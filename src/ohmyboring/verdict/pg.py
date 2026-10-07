"""판정 쓰기의 표 — store.rs record_consumption · record_supersedes · seal_superseded_claims · handed_paths.

커서를 받아 쓰고 커밋은 부르는 쪽(run.py)이 한 번 한다. 엔진은 문장마다 자동 커밋이라 중간
실패면 반쯤 쓰지만 여기는 한 요청 = 한 트랜잭션이다(의도한 차이). 은퇴 정의(RETIRED_NOTE)와 간선·
노드 upsert 는 remember 쓰기 길의 문자열을 그대로 쓴다 — 봉인만 다르다: remember 는 새 노트가
다시 말한 슬롯만 닫고(부분), 판정은 옛 노트의 current claim 전부를 닫고 칸별로 승격한다(통째).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ohmyboring.remember.writer import (
    _GET_DOC_SHA_SQL,
    _RETIRED_NOTE,
    _UPSERT_EDGE_SQL,
    _UPSERT_NODE_SQL,
    _owner_written_targets,
)
from ohmyboring.search.pg import doc_node_id

#: store.rs:2898-2911 — 옛 노트의 current claim 전부, 새 노트 최신 valid_from(없으면 now())으로.
_SEAL_SQL = (
    "UPDATE claim c SET superseded_at = COALESCE("
    "        (SELECT max(valid_from) FROM claim WHERE source_path = %(newer)s),"
    "        now())"
    " WHERE c.source_path = %(older)s AND c.superseded_at IS NULL"
    f"   AND {_RETIRED_NOTE};"
)

#: store.rs:2917-2937 — 옛 노트가 건드린 fact 슬롯마다 살아 있는 최신 행으로 넘긴다.
_PROMOTE_SQL = (
    "UPDATE claim c SET superseded_at = NULL"
    " FROM (SELECT c.subject, c.predicate, max(c.valid_from) AS mx"
    "       FROM claim c"
    "       WHERE (c.subject, c.predicate) IN (SELECT subject, predicate FROM claim"
    "                                          WHERE source_path = %(older)s AND kind = 'fact')"
    f"         AND NOT {_RETIRED_NOTE}"
    "       GROUP BY c.subject, c.predicate) m"
    " WHERE c.kind = 'fact' AND c.superseded_at IS NOT NULL"
    "   AND c.subject = m.subject AND c.predicate = m.predicate"
    "   AND c.valid_from = m.mx;"
)

#: store.rs:3165 handed_paths.
_HANDED_SQL = "SELECT DISTINCT dst FROM edge WHERE src = %(session)s AND kind = 'handed' ORDER BY dst;"


@dataclass(frozen=True)
class ConsumptionReport:
    """store.rs ConsumptionReport — 시도 수다(이미 있는 간선도 센다)."""

    used: int = 0
    contested: int = 0
    supersedes: int = 0
    unknown: int = 0


def _is_known(cur: Any, path: str) -> bool:
    cur.execute(_GET_DOC_SHA_SQL, {"path": path})
    return cur.fetchone() is not None


def _upsert_session(cur: Any, session_id: str, observed_at: str) -> str:
    node = f"session:{session_id}"
    cur.execute(_UPSERT_NODE_SQL, {"id": node, "kind": "session", "label": observed_at, "outcome": None})
    return node


def _link(cur: Any, src: str, path: str, kind: str, judge: str | None) -> None:
    cur.execute(_UPSERT_EDGE_SQL, {"src": src, "dst": doc_node_id(path), "kind": kind, "judge": judge})


def _record_verdicts(
    cur: Any, session_node: str, kind: str, paths: tuple[str, ...], judge: str | None
) -> tuple[int, int]:
    """경로마다 알려진 문서면 판정 간선, 아니면 unknown — (센 수, unknown)."""
    known = 0
    for path in paths:
        if _is_known(cur, path):
            _link(cur, session_node, path, kind, judge)
            known += 1
    return known, len(paths) - known


def seal_superseded_claims(cur: Any, newer: str, older: str) -> None:
    """옛 노트가 은퇴했으면(RETIRED_NOTE) current claim 전부를 닫고, 건드린 fact 슬롯을 승격한다."""
    cur.execute(_SEAL_SQL, {"newer": newer, "older": older})
    cur.execute(_PROMOTE_SQL, {"older": older})


def record_supersedes(cur: Any, pairs: tuple[tuple[str, str], ...], judge: str | None) -> tuple[int, int]:
    """(newer, older) 쌍마다 supersedes 간선 + 봉인 — (이은 수, unknown).

    같은 경로 두 번이거나 document 가 없는 쪽은 unknown 으로 세고 쓰지 않는다. 판정 순서는
    엔진 그대로: newer 확인 → older 확인."""
    linked = 0
    unknown = 0
    for newer, older in pairs:
        if newer == older or not _is_known(cur, newer) or not _is_known(cur, older):
            unknown += 1
            continue
        cur.execute(
            _UPSERT_EDGE_SQL,
            {"src": doc_node_id(newer), "dst": doc_node_id(older), "kind": "supersedes", "judge": judge},
        )
        linked += 1
        seal_superseded_claims(cur, newer, older)
    return linked, unknown


@dataclass(frozen=True)
class Verdicts:
    """쓸 판정 한 벌 — 경로 목록과 supersedes 쌍, 간선에 실을 judge(NULL 이면 아무도 안 이름 댐)."""

    used: tuple[str, ...]
    contested: tuple[str, ...]
    supersedes: tuple[tuple[str, str], ...]
    judge: str | None


def record_consumption(cur: Any, session_id: str, observed_at: str, verdicts: Verdicts) -> ConsumptionReport:
    """session 노드(label = observed_at, 마지막 것이 남는다) + used·contested 간선 + supersedes.

    간선은 ON CONFLICT DO NOTHING 이라 처음 이름 댄 judge 가 남는다."""
    session_node = _upsert_session(cur, session_id, observed_at)
    judge = verdicts.judge
    used_n, used_unknown = _record_verdicts(cur, session_node, "used", verdicts.used, judge)
    contested_n, contested_unknown = _record_verdicts(
        cur, session_node, "contested", verdicts.contested, judge
    )
    linked, superseded_unknown = record_supersedes(cur, verdicts.supersedes, judge)
    return ConsumptionReport(
        used=used_n,
        contested=contested_n,
        supersedes=linked,
        unknown=used_unknown + contested_unknown + superseded_unknown,
    )


def handed_paths(cur: Any, session_id: str) -> tuple[str, ...]:
    """그 세션에 건넨 문서 경로 — 중복 없이 경로 순. 모르는 세션은 빈 읽기."""
    cur.execute(_HANDED_SQL, {"session": f"session:{session_id}"})
    return tuple(row[0].removeprefix("doc:") for row in cur.fetchall())


def owner_authored(cur: Any, paths: tuple[str, ...]) -> list[str]:
    """대상 중 오너가 쓴 것 — owner.rs refused_supersedes 의 읽기."""
    return _owner_written_targets(cur, list(paths))
