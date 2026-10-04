"""wiki 노트 렌더 — drudge 의 render_wiki_note 를 파이썬으로 미러한다 (순수).

정본: drudge/src/vault/remember.rs:174-222. 칸 순서는 그대로 — id, title, kind, origin,
project, date, tags, tools, concepts, claims, relates_to, sources, [omb_session_id], author —
relates_to 는 늘 `[]` 로 시작해(관계는 그래프 투영이 나중에 채운다), omb_session_id 는
있을 때만 실린다. 본문은 끝 공백을 다듬은 뒤 한 줄 바꿈 하나로 닫는다. 시험은
remember.rs 의 Rust 사례(세션 id 실림/생략·YAML 로 되돌아감)로 고정한다 — 바이트
동일성이 아니라 되돌림 동일성이 목표(날짜 따옴표처럼 방출기마다 다른 몸짓은 파싱에서
사라진다).
"""

from __future__ import annotations

import yaml

from ohmyboring.remember.parse import Claim, FrontMatter


def _claim_map(claim: Claim) -> dict[str, str]:
    """Claim 하나의 YAML 맵 — Rust 구조체 필드 순서 그대로."""
    out = {
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value": claim.value,
        "kind": claim.kind,
        "confidence": claim.confidence,
    }
    if claim.said_by is not None:
        out["said_by"] = claim.said_by
    return out


def render_wiki_note(wiki_id: str, front: FrontMatter, body: str) -> str:
    """remember 노트를 wiki `.md` 텍스트로 — 필드 순서는 Rust Fm 구조체 그대로."""
    fm: dict[str, object] = {
        "id": wiki_id,
        "title": front.title or wiki_id,
        "kind": front.kind or "note",
        "origin": front.origin,
        "project": front.project,
        "date": front.date,
        "tags": list(front.tags),
        "tools": list(front.tools),
        "concepts": list(front.concepts),
        "claims": [_claim_map(claim) for claim in front.claims],
        "relates_to": [],
        "sources": list(front.sources),
    }
    if front.omb_session_id is not None:
        fm["omb_session_id"] = front.omb_session_id
    fm["author"] = front.author
    yaml_text = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=float("inf"))
    return f"---\n{yaml_text}---\n{body.rstrip()}\n"
