#!/usr/bin/env python3
"""4시간 sync 시험 — 해시가 정하는 두 약속을 가짜 연결(실행된 SQL 기록)·임시 볼트·가짜 임베딩으로 본다.

Run: python3 src/ohmyboring/sync/test_sync.py   (no pytest dependency)

1. relates_to 만 바뀐 노트는 다시 임베딩되지 않는다 (wiki-4029) — 변이: hash.strip_derived 가
   글자를 그대로 돌려주면(= 해시에 relates_to 가 들어가면) 빨강.
2. 해시가 같은 노트는 간선 DELETE 를 실행하지 않는다 — 변이: classify.judge_sha 에서
   「새 해시와 같다」 갈래를 지우면 빨강. 바뀐 노트의 DELETE 가 보이는 것을 같이 단언해
   가짜 커서가 DELETE 를 못 기록해서 초록인 경우를 막는다.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring.result import Ok  # noqa: E402
from ohmyboring.sync import run  # noqa: E402
from ohmyboring.sync.hash import content_hash  # noqa: E402

_DIM = 3
_DOC_SHAS = "SELECT source_path, sha FROM document;"
_CLEAR_EDGES = "DELETE FROM edge WHERE src = %(doc)s AND kind = ANY(%(kinds)s);"


def _note(relates_to: str, body: str = "본문 하나") -> str:
    return f'---\ntitle: t\norigin: personal\nkind: note\nrelates_to:\n- "[[{relates_to}]]"\n---\n{body}\n'


class _Cursor:
    def __init__(self, db_shas: dict[str, str], log: list) -> None:
        self._db_shas = db_shas
        self._log = log
        self._rows: list = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._log.append((sql, params))
        self._rows = list(self._db_shas.items()) if sql == _DOC_SHAS else []
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Conn:
    def __init__(self, db_shas: dict[str, str], log: list) -> None:
        self._db_shas = db_shas
        self._log = log

    def cursor(self):
        return _Cursor(self._db_shas, self._log)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _sync(wiki: Path, db_shas: dict[str, str]):
    log: list = []
    embedded: list[str] = []

    def embed(text):
        embedded.append(text)
        return Ok([0.0] * _DIM)

    deps = run.SyncDeps(
        wiki_dir=str(wiki),
        connect=lambda: _Conn(db_shas, log),
        embed=embed,
        embed_dim=_DIM,
        split_frontmatter=split_frontmatter,
        classify_origin=lambda _path: "personal",
        append_event=lambda *args, **fields: True,
    )
    return run.run_sync(deps), log, embedded


class SyncHashTests(unittest.TestCase):
    def test_relates_to_only_change_does_not_re_embed(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            note = wiki / "wiki-0001.md"
            db_sha = content_hash(_note("wiki-0002"))
            note.write_text(_note("wiki-0777"), encoding="utf-8")
            report, log, embedded = _sync(wiki, {str(note): db_sha})
        self.assertEqual(embedded, [])
        self.assertEqual(report.counts.unchanged, 1)
        self.assertFalse([sql for sql, _ in log if sql.startswith("INSERT INTO document")])

    def test_unchanged_hash_runs_no_edge_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp)
            same, edited = wiki / "wiki-0001.md", wiki / "wiki-0002.md"
            same.write_text(_note("wiki-0009"), encoding="utf-8")
            edited.write_text(_note("wiki-0009", body="새 본문"), encoding="utf-8")
            db_shas = {str(same): content_hash(_note("wiki-0009")), str(edited): "stale"}
            _report, log, _embedded = _sync(wiki, db_shas)
        cleared = {params["doc"] for sql, params in log if sql == _CLEAR_EDGES}
        self.assertEqual(cleared, {f"doc:{edited}"})


if __name__ == "__main__":
    unittest.main()
