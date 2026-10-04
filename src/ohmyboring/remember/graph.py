"""그래프 투영 — 한 노트가 엔진 그래프에 남길 간선 집합을 「같은 결정」으로 정한다 (E3b).

포트 출처 — drudge/src/ingest.rs FrontmatterGraphExtractor::extract(도구·개념·클레임),
store.rs upsert_document(태그·프로젝트), upsert_claim_node(claim 노드·is_a·claim_of_project),
relate_said(said), record_supersedes(supersedes). 순수 계산: (src, kind, dst) 집합을
낼 뿐 그래프·DB 에 쓰는 일이 없다 — 그림자는 「정하고」 대조만 하고, 쓰기는 언제나
엔진이 한다.

대체 봉인만 엔진과 일부러 다르다. 엔진(store.rs:2898 seal_superseded_claims)은 옛 노트의
현재 claim 을 통째로 봉인하지만, 여기서 정하는 목표는 부분 닫기다 — 새 노트가 다시 말한
(subject, predicate) 슬롯만 닫고, 새 노트가 말하지 않은 옛 사실은 산다(판정 wiki-2855,
의도한 차이). 그 어긋남은 그림자 사건의 사유 (나) 「의도한 차이 — 부분 닫기」로 남는다.

claim 두 부류(store.rs:1384-1388) — fact 는 (subject,predicate) 가 전역 단일값 슬롯이고,
그 외 kind 는 노트 안의 목록 항목이다. 슬롯 다툼(다른 노트가 같은 슬롯의 최신값을 쥠)은
여기서 모델에 넣지 않는다 — 대조는 그림자가 읽어낸 실제 봉인 상태와 목표 상태를 놓고 본다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 이 노트의 기억 투영이 책임지는 간선 부류 — 엔진이 remember 로 쓰는 것들.
#: 그 밖의 부류(handed·used·contested…)는 소비 문지방이라 투영과 무관하다.
PROJECTION_KINDS = frozenset(
    {"uses", "about", "claims", "is_a", "said", "tagged", "in_project", "claim_of_project", "supersedes"}
)

#: 한 노트의 tool/concept 투영 상한 — ingest.rs:150 (FrontmatterGraphExtractor::new 의 cap 6).
PROJECT_CAP = 6

#: said_by 가 낼 수 있는 화자 — SaidBy enum 이 Owner 하나뿐이라(frontmatter.rs:88-92)
#: 화자 노드도 이 하나다(store.rs:344 OWNER_SPEAKER_NODE).
OWNER_SPEAKER_NODE = "person:owner"

#: claim 의 value 가 「할 일이 없음」을 뜻할 때 next/blocked 를 fact 로 강등하는 어휘
#: (frontmatter.rs:152-172 WORK_DENIALS).
WORK_DENIALS = frozenset(
    (
        "none",
        "nothing",
        "n/a",
        "na",
        "not applicable",
        "no",
        "-",
        "--",
        "없음",
        "없다",
        "없습니다",
        "없어요",
        "해당 없음",
        "해당없음",
        "남은 작업 없음",
        "남은 작업이 없음",
        "なし",
        "無し",
        "該当なし",
    )
)


@dataclass(frozen=True)
class ClaimRow:
    """claim 테이블 한 행의 대조용 뷰 — 슬롯은 이미 canon 형태(DB 가 그렇게 쓴다)."""

    source_path: str
    subject: str
    predicate: str
    kind: str
    sealed: bool  # superseded_at IS NOT NULL


@dataclass(frozen=True)
class GraphSnapshot:
    """그림자의 읽기 전용 그래프 조회 한 번 — 문이 남긴 간선·행의 실제 끝 상태."""

    edges: frozenset[tuple[str, str, str]]  # (src, kind, dst)
    documents: frozenset[str]  # document 행이 있는 source_path (대체 대상의 존재 판정용)
    claims: tuple[ClaimRow, ...]


def has_han(text: str) -> bool:
    """한자(U+4E00..U+9FFF) 하나라도 있으면 참 — 모형이 중국어로 새어 나온 흔적을 걸러낸다
    (ingest.rs:319-321). 한글은 한자가 아니라 통과한다."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def slugify(text: str) -> str:
    """소문자로 + 별칭(c++→cpp, c#→csharp, .net→dotnet) + [a-z0-9] 만 남긴다
    (ingest.rs:325-335). 구분자를 다 지워 철자 변형이 노드를 갈라놓는 일이 없게 한다."""
    normalized = text.lower().replace("c++", "cpp").replace("c#", "csharp").replace(".net", "dotnet")
    return "".join(ch for ch in normalized if ch.isascii() and ch.isalnum())


def canon(text: str) -> str:
    """claim 슬롯의 표기 정규화 — 소문자, 공백/_/- 뭉치를 하나의 - 로 접는다
    (ingest.rs:340-356). 「foo bar」와 「foo-bar」가 다른 슬롯이 되는 일이 없게 한다."""
    out: list[str] = []
    prev_sep = False
    for ch in text.lower():
        if ch.isspace() or ch in "_-":
            prev_sep = True
        else:
            if prev_sep and out:
                out.append("-")
            prev_sep = False
            out.append(ch)
    return "".join(out)


def denies_work(value: str) -> bool:
    """value 가 「할 일 없음」 계열인지 — frontmatter.rs:174-181."""
    v = value.strip().rstrip(".。").strip().lower()
    return v in WORK_DENIALS


def claim_kind(raw_kind: str, value: str) -> str:
    """Claim::kind() 그대로 (frontmatter.rs:135-144) — 빈 kind 는 fact, next/blocked 가
    작업 부정의 값을 쥐면 fact 로 강등."""
    kind = raw_kind.strip()
    if not kind:
        return "fact"
    if kind in ("next", "blocked") and denies_work(value):
        return "fact"
    return kind


def _capped_slugs(items: list[str]) -> list[str]:
    """도구·개념 목록의 슬러그 — 앞 6개만 보고, 빈칸·한자를 빼고, 슬러그가 빈 것과
    중복을 빤다 (ingest.rs:173-205 의 take(cap)→필터→dedup 순서 그대로)."""
    out: list[str] = []
    for item in items[:PROJECT_CAP]:
        item = item.strip()
        if not item or has_han(item):
            continue
        slug = slugify(item)
        if slug and slug not in out:
            out.append(slug)
    return out


def _claim_written(claim: Mapping[str, Any]) -> tuple[str, str] | None:
    """claim 하나가 실제로 쓰이는 슬롯 — canon 슬롯을 낳거나, 쓰이지 않으면 None.
    subject·predicate·value 가 비거나, subject·value 에 한자가 있으면 엔진이 걸러낸다
    (ingest.rs:220-227 — predicate 만 한자를 안 본다)."""
    subject = canon(str(claim.get("subject") or ""))
    predicate = canon(str(claim.get("predicate") or ""))
    value = str(claim.get("value") or "").strip()
    if not subject or not predicate or not value or has_han(subject) or has_han(value):
        return None
    return (subject, predicate)


def restated_slots(front: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """이 노트가 다시 말하는 (subject, predicate) 슬롯 집합 — 부분 닫기의 「닫을 것」."""
    slots: set[tuple[str, str]] = set()
    for claim in front.get("claims") or []:
        if isinstance(claim, Mapping):
            if slot := _claim_written(claim):
                slots.add(slot)
    return frozenset(slots)


def _claim_edges(claim: Mapping[str, Any], doc: str, project: str) -> set[tuple[str, str, str]]:
    """claim 하나가 남기는 간선 — 슬롯 노드·주장·(비-fact 면) is_a·프로젝트·화자."""
    slot = _claim_written(claim)
    if slot is None:
        return set()
    subject, predicate = slot
    claim_id = f"claim:{subject}:{predicate}"
    out = {(doc, "claims", claim_id)}
    kind = claim_kind(str(claim.get("kind") or ""), str(claim.get("value") or ""))
    if kind != "fact":
        out.add((claim_id, "is_a", f"{kind}:{subject}:{predicate}"))
    if project:
        out.add((claim_id, "claim_of_project", f"project:{project}"))
    if (claim.get("said_by") or None) == "owner":
        out.add((OWNER_SPEAKER_NODE, "said", doc))
        out.add((OWNER_SPEAKER_NODE, "said", claim_id))
    return out


def expected_edges(
    front: Mapping[str, Any],
    source_path: str,
    supersedes: tuple[str, ...] = (),
) -> frozenset[tuple[str, str, str]]:
    """엔진이 이 노트를 그래프에 옮길 때 남길 (src, kind, dst) 집합 — 엔진 투영과 같은 결정.

    front 는 그림자의 대조용 뷰(_front_view) — tags·tools·concepts·claims·project 만 본다.
    supersedes 는 문서 행이 확인된 대상 경로(존재하지 않는 대상은 호출 쪽에서 뺀다 —
    record_supersedes 가 unknown 으로 세고 간선을 안 쓴다, store.rs:2866-2876).
    """
    doc = f"doc:{source_path}"
    project = str(front.get("project") or "")
    edges: set[tuple[str, str, str]] = set()
    if project:
        edges.add((doc, "in_project", f"project:{project}"))
    edges.update((doc, "tagged", f"topic:{tag}") for tag in front.get("tags") or [])
    edges.update((doc, "uses", f"tool:{slug}") for slug in _capped_slugs(list(front.get("tools") or [])))
    edges.update(
        (doc, "about", f"concept:{slug}") for slug in _capped_slugs(list(front.get("concepts") or []))
    )
    for claim in front.get("claims") or []:
        if isinstance(claim, Mapping):
            edges.update(_claim_edges(claim, doc, project))
    edges.update((doc, "supersedes", f"doc:{older}") for older in supersedes)
    return frozenset(edges)


def expected_seal_states(
    claims: tuple[ClaimRow, ...],
    new_path: str,
    new_slots: frozenset[tuple[str, str]],
    superseded_paths: frozenset[str],
) -> list[tuple[ClaimRow, bool]]:
    """각 claim 행의 목표 봉인 상태 — 부분 닫기 의도.

    새 노트가 쓴 행은 산다. 대첻된 옛 노트의 행은 새 노트가 다시 말한 슬롯만 닫힌다 —
    엔진의 통째 봉인(store.rs:2898)과의 어긋남이 여기서 나오고, 그림자는 그걸 사유 (나)로
    남긴다. 두 노트 모두 아닌 행(함께 조회된 다른 대상 등)은 대상이 아니라 비교에 안 낸다.
    """
    out: list[tuple[ClaimRow, bool]] = []
    for row in claims:
        if row.source_path == new_path:
            out.append((row, False))
        elif row.source_path in superseded_paths:
            out.append((row, (row.subject, row.predicate) in new_slots))
    return out


@dataclass(frozen=True)
class SealVerdict:
    """봉인 대조 한 번의 결과 — engine_only 는 실제로 닫혔는데 목표는 산 행(나家族),
    python_only 는 목표는 닫았는데 실제로 산 행(가)."""

    checked: int
    engine_only: int
    python_only: int
    engine_only_on_new_note: int  # 새 노트 자신의 행이 닫힌 어긋남 — (나)이긴 한데 이상한 쪽


def compare_seals(expected: list[tuple[ClaimRow, bool]], new_path: str) -> SealVerdict:
    """목표 상태와 실제 봉인 상태를 놓고 어긋남을 샌다.

    engine_only 가 새 노트 자신의 행에서 나오면 그건 통째 봉인이 아니라 이상 현상 —
    갈래를 달리해 (가)로 본다."""
    engine_only = 0
    python_only = 0
    engine_only_on_new_note = 0
    for row, want_sealed in expected:
        if row.sealed and not want_sealed:
            engine_only += 1
            if row.source_path == new_path:
                engine_only_on_new_note += 1
        elif want_sealed and not row.sealed:
            python_only += 1
    return SealVerdict(
        checked=len(expected),
        engine_only=engine_only,
        python_only=python_only,
        engine_only_on_new_note=engine_only_on_new_note,
    )
