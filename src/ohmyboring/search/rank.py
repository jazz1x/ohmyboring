"""순위 파이프라인 — 융합·판정 넛지·집합 안 순서·예산. 순수 함수만 (DB·HTTP import 0).

drudge/src/retrieve.rs 의 모양을 그대로 옮긴다. 가중치는 소유자가 미리 정해 둔 고정값
(wiki-2505 계약 — 시험이 숫자를 못 박는다): RRF_K=60, 한 표 = 1/61-1/62 (한 목록에서
1위와 2위 사이 점수 차), net 상한 ±3, 풀 = max(k*4, 20).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

RRF_K = 60.0
FEEDBACK_STEP = 1.0 / (RRF_K + 1.0) - 1.0 / (RRF_K + 2.0)
FEEDBACK_NET_MAX = 3


@dataclass(frozen=True)
class Hit:
    """한 조각 — store.rs Hit 의 부분 집합 (dist 는 항상 vector_cosine 경로의 값)."""

    id: str
    content: str
    origin: str
    project: str
    source_path: str
    dist: float
    dist_kind: str  # "vector_cosine" | "text_rank"


@dataclass(frozen=True)
class Scored:
    hit: Hit
    score: float


@dataclass(frozen=True)
class RankFacts:
    """order_within_set 이 읽는 문서 사실 둘 — rank_facts 질의 한 행."""

    superseded: bool
    owner: bool
    updated_at: datetime | None


@dataclass(frozen=True)
class Counts:
    used: int = 0
    contested: int = 0


def pool_size(max_results: int) -> int:
    """각 목록의 후보 풀 — retrieve.rs:285 (retrieve_budget, /search 경로)."""
    return max(max_results * 4, 20)


def rrf_term(rank: int) -> float:
    """rank 는 1부터 — 0 이면 호출자 버그."""
    if rank < 1:
        raise ValueError("rrf rank is 1-based")
    return 1.0 / (RRF_K + rank)


def net_feedback(used: int, contested: int) -> int:
    """used − contested, 상한 ±FEEDBACK_NET_MAX — 반응 더미가 순위를 통째로 뒤집지 못하게."""
    return max(-FEEDBACK_NET_MAX, min(FEEDBACK_NET_MAX, used - contested))


def merge_hits(
    vec_hits: list[Hit],
    txt_hits: list[Hit],
    counts: dict[str, Counts],
    exclude_origins: tuple[str, ...] = (),
) -> list[Scored]:
    """벡터·어휘 목록을 RRF 로 융합하고 판정 넛지를 얹어 점수 내림차순으로 돌려준다.

    판정 카운트 읽기 실패는 여기 오기 전에 Err 로 끝난다 — Rust 의 「로그만 남기고 판정 없이
    순위」 폭은 파이썬이 일부러 닫는다 (조용한 폭락 금지, 계약 divergence 목록).
    """
    fused: dict[str, float] = {}
    byid: dict[str, Hit] = {}
    for rank, hit in enumerate(vec_hits, start=1):
        fused[hit.id] = fused.get(hit.id, 0.0) + rrf_term(rank)
        byid.setdefault(hit.id, hit)
    for rank, hit in enumerate(txt_hits, start=1):
        fused[hit.id] = fused.get(hit.id, 0.0) + rrf_term(rank)
        byid.setdefault(hit.id, hit)
    nets = {path: net_feedback(c.used, c.contested) for path, c in counts.items()}
    for path, net in nets.items():
        if net == 0:
            continue
        for chunk_id, hit in byid.items():
            if hit.source_path == path and chunk_id in fused:
                fused[chunk_id] += net * FEEDBACK_STEP
    merged = [
        Scored(hit, score)
        for chunk_id, score in fused.items()
        if (hit := byid[chunk_id]).origin not in exclude_origins
    ]
    # 한 표는 정확히 한 계단이라 점수 동점은 곧 판정이 만든 동점 — 판정 많은 쪽이 먼저, 그다음 id 오름차순.
    merged.sort(
        key=lambda scored: (
            -scored.score,
            -nets.get(scored.hit.source_path, 0),
            scored.hit.id,
        )
    )
    return merged


def tally_feedback(
    rows: list[tuple[str, str, str | None, str | None]], owner_only: bool
) -> dict[str, Counts]:
    """edge 행 (dst, kind, judge, author) 를 경로별 Counts 로 센다.

    owner_only (ranking 피드백) 일 때만: owner 가 쓴 노트의 contested 는 owner 판정만 깎는다 —
    다른 judge 가 owner 글을 깎지 못한다 (store.rs:3061-3068). used 는 누가 판정했든 센다.
    """
    counts: dict[str, Counts] = {}
    for dst, kind, judge, author in rows:
        if owner_only and kind == "contested" and author == "owner" and judge != "owner":
            continue
        path = dst.removeprefix("doc:")
        entry = counts.setdefault(path, Counts())
        if kind == "used":
            counts[path] = replace(entry, used=entry.used + 1)
        elif kind == "contested":
            counts[path] = replace(entry, contested=entry.contested + 1)
    return counts


def rank_key(facts: RankFacts | None) -> tuple[bool, bool, float]:
    """집합 안 순서 한 벌 — (대체되님, owner 아님, owner 는 갱신 최신 먼저) 다음 점수.

    세 번째 칸: owner 의 updated_at 은 클수록(최신) 앞에 와야 하니 부호를 뒤집고,
    owner 가 아니면 같은 칸의 어떤 시각보다 뒤(+inf)로 본다 — Rust 의 Reverse(Option) 규칙.
    """
    match facts:
        case None:
            return (False, True, float("inf"))
        case RankFacts(superseded=superseded, owner=True, updated_at=updated_at):
            key = -updated_at.timestamp() if updated_at is not None else 0.0
            return (superseded, False, key)
        case RankFacts(superseded=superseded, owner=False):
            return (superseded, True, float("inf"))


def order_within_set(scored: list[Scored], facts: dict[str, RankFacts]) -> list[Scored]:
    """멤버십은 점수로 이미 정해졌고, 돌려주는 모양만 rank_key 순으로 다시 정렬한다 —
    대체된 노트는 밀리는(demote) 거지 짤리는(cut) 게 아니다."""
    return sorted(
        scored,
        key=lambda scored_item: (
            rank_key(facts.get(scored_item.hit.source_path)),
            -scored_item.score,
        ),
    )


def within_budget(merged: list[Scored], max_results: int, max_chars: int) -> list[Scored]:
    """per_hit_cap = max_chars/max_results, 남은 예산 안에서 자른다 — within_budget 그대로.

    content 는 새 Hit (자른 조각) 으로 — 입력은 그대로 둔다.
    """
    per_hit_cap = max_chars // max_results
    budget = max_chars
    out: list[Scored] = []
    for item in merged:
        if len(out) >= max_results:
            break
        take = min(per_hit_cap, budget)
        if take == 0:
            break
        cut = item.hit.content[:take]
        if not cut:
            continue
        budget -= len(cut)
        out.append(Scored(replace(item.hit, content=cut), item.score))
    return out


def attach_related(
    heads: list[str],
    pool_paths: list[str],
    related_lists: list[list[tuple[str, str]]],
    snippet_chars: int,
) -> dict[str, list[dict[str, str]]]:
    """related 붙이기 — http.rs:543-561 의 순수 부분. seen 은 전체 hit 경로로 시작해, related 는
    머리 hit(heads)의 것만 받고 한 노트는 전 응답에서 한 번만 실린다 (seen 에 없는 것만 — 먼저 나온
    머리가 가져간다). snippet 은 문자 단위로 snippet_chars 까지 — Rust 의
    content.chars().take(RELATED_SNIPPET_CHARS) 와 같다."""
    seen = set(pool_paths)
    out: dict[str, list[dict[str, str]]] = {}
    for head, docs in zip(heads, related_lists, strict=True):
        notes = out.setdefault(head, [])
        for source_path, content in docs:
            if source_path in seen:
                continue
            seen.add(source_path)
            notes.append({"source_path": source_path, "snippet": content[:snippet_chars]})
    return out
