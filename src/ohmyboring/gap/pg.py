"""빈자리 쓰기 — 기존 node/edge 칸만 쓴다 (새 표·새 칸 없음). 실패는 Err 값으로."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import psycopg

from ohmyboring.gap.parse import GapArgs
from ohmyboring.result import Either
from ohmyboring.search.pg import PgError, _attempt, doc_node_id


@dataclass(frozen=True)
class GapReport:
    gap: str
    kind: str
    handed: int
    unknown: int

    def payload(self) -> dict:
        return {"gap": self.gap, "kind": self.kind, "handed": self.handed, "unknown": self.unknown}


def gap_node_id(session_id: str, query: str) -> str:
    digest = hashlib.sha256(f"{session_id}\n{query}".encode()).hexdigest()
    return f"gap:{digest[:16]}"


def record_gap(conn: psycopg.Connection, args: GapArgs, observed_at: str) -> Either[GapReport, PgError]:
    """gap 노드 upsert + session→gap 간선 + 알려진 문서로 가는 gap_handed 간선. 쓰기는 여기서 커밋."""

    def run() -> GapReport:
        gap = gap_node_id(args.session_id, args.query)
        session_node = f"session:{args.session_id}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES (%s, 'gap', %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                (gap, args.query, args.kind.value),
            )
            cur.execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES (%s, 'session', %s, NULL)"
                " ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                (session_node, observed_at),
            )
            cur.execute(
                "INSERT INTO edge (src, dst, kind, judge) VALUES (%s, %s, 'gap', NULL)"
                " ON CONFLICT DO NOTHING;",
                (session_node, gap),
            )
            cur.execute("SELECT source_path FROM document WHERE source_path = ANY(%s);", (list(args.handed),))
            known = {row[0] for row in cur.fetchall()}
            handed = [path for path in args.handed if path in known]
            for path in handed:
                cur.execute(
                    "INSERT INTO edge (src, dst, kind, judge) VALUES (%s, %s, 'gap_handed', NULL)"
                    " ON CONFLICT DO NOTHING;",
                    (gap, doc_node_id(path)),
                )
        conn.commit()
        return GapReport(gap, args.kind.value, len(handed), len(args.handed) - len(handed))

    return _attempt(run)
