"""그림자 대조 — 파이썬 읽어 들이기와 Rust 엔진 결과를 나란히 대조만 한다(운영 표에 쓰지 않는다).

네 축: ①문서(sha·title·kind·tags) ②조각(개수·내용) ③임베딩(표본 코사인) ④새 자르기 통계.
운영 표에는 손대는 문이 없다 — 읽기 문만 나가고, test_shadow.py 가 이 파일 소스를 훑어
쓰기 문 하나라도 들어오면 시험을 빨갛게 끝낸다. DSN 은 DOOR_PG_DSN(문 안에서는 이미 세팅).

문 컨테이너 안에서 돌릴 때는 BORING_IN_CONTAINER=1 을 주거나 BORING_LLM_BASE_URL 을 준다 —
주소는 config.llm_base_url() 이 정하고 코드에 박지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ohmyboring.adapters import embed as embed_adapter
from ohmyboring.ingest.chunk import chunk_stats, fixed_chunks
from ohmyboring.ingest.note import NoteDoc, Skipped, read_note
from ohmyboring.result import Err, Ok

SQL_DOCUMENTS = "SELECT source_path, title, kind, tags, sha FROM document ORDER BY source_path"
SQL_CHUNKS = "SELECT source_path, chunk_idx, content FROM chunk ORDER BY source_path, chunk_idx"
SQL_VECTOR = "SELECT embedding::text FROM chunk WHERE id = %s"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def parse_vector(text: str) -> list[float]:
    """pgvector 의 텍스트 모양([1,2,3])을 파이썬 벡터로 — 별도 드라이버 없이 끝낸다."""
    inner = text.strip().removeprefix("[").removesuffix("]")
    if not inner:
        return []
    return [float(v) for v in inner.split(",")]


def stride_sample(total: int, wanted: int) -> list[int]:
    """0..total-1 을 골고루 걸러낸 결정적 표본 자리(전체가 wanted 이하면 전부)."""
    if total <= 0 or wanted <= 0:
        return []
    if total <= wanted:
        return list(range(total))
    return sorted({round(i * (total - 1) / max(wanted - 1, 1)) for i in range(wanted)})


def chunk_diff(db_chunks: list[tuple[int, str]], pieces: list[str]) -> tuple[bool, bool]:
    """(개수 같음, 내용 같음) — db_chunks 는 chunk_idx 순."""
    count_equal = len(db_chunks) == len(pieces)
    content_equal = all(idx < len(pieces) and content == pieces[idx] for idx, content in db_chunks)
    return count_equal, content_equal


def _documents(cur: Any) -> list[tuple[str, str | None, str, list[str], str]]:
    cur.execute(SQL_DOCUMENTS)
    return [(row[0], row[1], row[2], list(row[3] or []), row[4]) for row in cur.fetchall()]


def _chunks_by_doc(cur: Any) -> dict[str, list[tuple[int, str]]]:
    cur.execute(SQL_CHUNKS)
    grouped: dict[str, list[tuple[int, str]]] = {}
    for source_path, chunk_idx, content in cur.fetchall():
        grouped.setdefault(source_path, []).append((chunk_idx, content))
    return grouped


DocRow = tuple[str, str | None, str, list[str], str]


def _axis_checks(note: NoteDoc, row: DocRow) -> dict[str, bool]:
    """① 문서 축 — 파이썬 재파싱 값과 표 값을 축마다 비교한 결과."""
    _, db_title, db_kind, db_tags, db_sha = row
    return {
        "sha": note.sha == db_sha,
        "title": note.title == db_title,
        "kind": note.kind == db_kind,
        "tags": list(note.tags) == db_tags,
    }


@dataclass
class _Ledger:
    """대조 한 바퀴의 누계 — run() 이 이것만 채우고 _report 가 JSON 모양으로 바꾼다."""

    axes: dict[str, list[int]]
    diffs: dict[str, list[Any]]
    files_missing: int = 0
    files_unparseable: int = 0
    chunk_docs: int = 0
    chunk_count_yes: int = 0
    chunk_content_yes: int = 0
    chunk_diff_paths: list[str] = field(default_factory=list)
    embed_pool: list[tuple[str, str]] = field(default_factory=list)
    titles_bodies: list[tuple[str, str]] = field(default_factory=list)


def _record_skip(ledger: _Ledger, skipped: Skipped) -> None:
    if skipped.reason.startswith("unreadable:"):
        ledger.files_missing += 1
        ledger.diffs["missing_file"].append(skipped.source_path)
    else:
        ledger.files_unparseable += 1
        ledger.diffs["unparseable"].append({"path": skipped.source_path, "reason": skipped.reason})


def _record_axes(ledger: _Ledger, checks: dict[str, bool], source_path: str) -> None:
    for axis, equal in checks.items():
        ledger.axes[axis][1] += 1
        if equal:
            ledger.axes[axis][0] += 1
        else:
            ledger.diffs[axis].append(source_path)


def _record_chunks(ledger: _Ledger, note: NoteDoc, db_chunks: list[tuple[int, str]]) -> None:
    """② 조각 축 — 본문 없는 노트는 분모에서 빼고, 일치하면 그 조각들을 임베딩 풀에 넣는다."""
    pieces = fixed_chunks(note.body.strip())
    if not pieces or all(not piece.strip() for piece in pieces):
        return
    ledger.titles_bodies.append((note.title or "", note.body))
    count_equal, content_equal = chunk_diff(db_chunks, pieces)
    ledger.chunk_docs += 1
    ledger.chunk_count_yes += 1 if count_equal else 0
    ledger.chunk_content_yes += 1 if content_equal else 0
    if not (count_equal and content_equal):
        ledger.chunk_diff_paths.append(note.source_path)
        return
    ledger.embed_pool.extend((f"{note.source_path}#{idx}", content) for idx, content in db_chunks)


def _ingest_document(ledger: _Ledger, row: DocRow, chunks_by_doc: dict[str, list[tuple[int, str]]]) -> None:
    """문서 한 줄의 ①② 대조 — read_note 부터 임베딩 풀 등록까지."""
    match read_note(row[0]):
        case Err(skipped):
            _record_skip(ledger, skipped)
        case Ok(note):
            _record_axes(ledger, _axis_checks(note, row), note.source_path)
            _record_chunks(ledger, note, chunks_by_doc.get(note.source_path, []))


def _embedding_sample(
    cur: Any, embed_pool: list[tuple[str, str]], sample: int
) -> tuple[list[float], list[dict[str, str]]]:
    """③ 임베딩 축 — 풀에서 골고루 뽑아 파이썬 어댑터 벡터와 표 벡터의 코사인을 낸다."""
    sims: list[float] = []
    failures: list[dict[str, str]] = []
    for at in stride_sample(len(embed_pool), sample):
        chunk_id, content = embed_pool[at]
        match embed_adapter.embed(content):
            case Ok(vec):
                cur.execute(SQL_VECTOR, (chunk_id,))
                row = cur.fetchone()
                stored = parse_vector(row[0]) if row else []
                sims.append(cosine(vec, stored))
            case Err(failure):
                failures.append({"id": chunk_id, "error": str(failure)})
    return sims, failures


def _report(
    documents_total: int,
    ledger: _Ledger,
    sims: list[float],
    failures: list[dict[str, str]],
    sample: int,
) -> dict[str, Any]:
    return {
        "schema": "omb-ingest-shadow/v1",
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "arguments": {"sample": sample},
        "population": {
            "documents_in_db": documents_total,
            "files_missing": ledger.files_missing,
            "files_unparseable": ledger.files_unparseable,
        },
        "documents": {axis: {"match": hit, "total": tot} for axis, (hit, tot) in ledger.axes.items()},
        "document_diffs": ledger.diffs,
        "chunks": {
            "documents_compared": ledger.chunk_docs,
            "count_equal": {"yes": ledger.chunk_count_yes, "total": ledger.chunk_docs},
            "content_equal": {"yes": ledger.chunk_content_yes, "total": ledger.chunk_docs},
            "diff_paths": ledger.chunk_diff_paths,
        },
        "embeddings": {
            "sampled": len(sims) + len(failures),
            "succeeded": len(sims),
            "cosine_min": min(sims) if sims else None,
            "cosine_avg": sum(sims) / len(sims) if sims else None,
            "failures": failures,
        },
        "heading_cut": chunk_stats(ledger.titles_bodies),
    }


def run(conn: Any, sample: int) -> dict[str, Any]:
    """대조 한 바퀴 — 연결은 밖에서 열리고 여기서는 읽기만 한다."""
    cur = conn.cursor()
    documents = _documents(cur)
    chunks_by_doc = _chunks_by_doc(cur)
    ledger = _Ledger(
        axes={"sha": [0, 0], "title": [0, 0], "kind": [0, 0], "tags": [0, 0]},
        diffs={
            "sha": [],
            "title": [],
            "kind": [],
            "tags": [],
            "unparseable": [],
            "missing_file": [],
        },
    )
    for row in documents:
        _ingest_document(ledger, row, chunks_by_doc)
    sims, failures = _embedding_sample(cur, ledger.embed_pool, sample)
    return _report(len(documents), ledger, sims, failures, sample)


def _connect(dsn: str) -> Any:
    import psycopg  # 문 컨테이너 안의 드라이버 — 호스트 시험은 이 경계를 안 탄다.

    conn = psycopg.connect(dsn)
    conn.autocommit = True
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ohmyboring.ingest.shadow")
    parser.add_argument("--sample", type=int, default=20, help="임베딩 대조 표본 수(기본 20)")
    parser.add_argument("--out", default=None, help="JSON 을 stdout 대신 적을 경로")
    parser.add_argument("--dsn", default=os.environ.get("DOOR_PG_DSN", ""))
    args = parser.parse_args(argv)
    if not args.dsn:
        print("[shadow] DOOR_PG_DSN 이 비어 있다 — 문 안에서 돌리거나 --dsn 을 준다.", file=sys.stderr)
        return 2
    conn = _connect(args.dsn)
    try:
        report = run(conn, args.sample)
    finally:
        conn.close()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"[shadow] JSON 을 {args.out} 에 적었다.", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
