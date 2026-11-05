"""문 기동 가드(C-SURALL2 C0b1): backend=surreal 에서 document·chunk 를 pg 에 직접 쓰는 파이썬 길
(DOOR_REMEMBER_WRITER · DOOR_SYNC_OWNER · DOOR_LINK_WRITER = python) 이 켜져 있으면 문은 뜨지 않는다.
기동 경로(`_lifespan`)를 그대로 돌린다 — 가드가 다른 곳에 있으면 이 시험이 못 본다."""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

door = importlib.import_module("agents.door.door")
brief_once = importlib.import_module("agents.door.brief_once")

_PYTHON_WRITERS = ("DOOR_REMEMBER_WRITER", "DOOR_SYNC_OWNER", "DOOR_LINK_WRITER")
# 묶음2: node·edge 를 pg 에 쓰는 길 — verdict/run.py:151, door.py:1205·2259 (search/pg.py write_handover), compact → sync/run.py gc_apply
_GRAPH_WRITERS = ("DOOR_VERDICT_WRITER", "DOOR_RECALL_READER", "DOOR_SEARCH_READER", "DOOR_COMPACT_OWNER")


def _start(backend: str, env: dict[str, str]) -> None:
    """문을 기동 경로로 한 번 띄웠다 내린다. 가드가 막으면 SystemExit 이 난다."""

    async def run() -> None:
        async with door._lifespan(door.app):
            pass

    with tempfile.TemporaryDirectory() as vault:
        full = {"BORING_VAULT_DIR": vault, **env}
        with (
            mock.patch.dict("os.environ", full, clear=False),
            mock.patch.object(door.boring_config, "load", return_value={"vector": {"backend": backend}}),
        ):
            asyncio.run(run())


class BackendGuard(unittest.TestCase):
    def test_surreal_with_a_python_writer_refuses_to_start(self) -> None:
        for name in _PYTHON_WRITERS:
            with self.subTest(switch=name), self.assertRaises(SystemExit) as stopped:
                _start("surreal", {name: "python"})
            self.assertIn(name, str(stopped.exception))

    def test_surreal_with_every_writer_on_engine_starts(self) -> None:
        _start("surreal", {name: "engine" for name in _PYTHON_WRITERS})

    def test_pgvector_with_python_writers_starts(self) -> None:
        _start("pgvector", {name: "python" for name in _PYTHON_WRITERS})

    def test_surreal_with_a_python_graph_writer_refuses_to_start(self) -> None:
        for name in _GRAPH_WRITERS:
            with self.subTest(switch=name), self.assertRaises(SystemExit) as stopped:
                _start("surreal", {name: "python"})
            self.assertIn(name, str(stopped.exception))

    def test_surreal_with_every_graph_writer_on_engine_starts(self) -> None:
        _start("surreal", {name: "engine" for name in _GRAPH_WRITERS})

    def test_pgvector_with_python_graph_writers_starts(self) -> None:
        _start("pgvector", {name: "python" for name in _GRAPH_WRITERS})

    def test_surreal_refuses_the_python_paths_that_read_pg(self) -> None:
        """C-PGREAD1: pg 를 읽는 파이썬 길(MEMORY_READER: _fetch_edges·_fetch_active_projects, BRIEF_OWNER: generate.py)
        과 brief_once 진입은 surreal 에서 낡은 pg 값을 조용히 읽으니 거부한다."""
        for name in ("DOOR_MEMORY_READER", "DOOR_BRIEF_OWNER"):
            with self.subTest(switch=name), self.assertRaises(SystemExit) as stopped:
                _start("surreal", {name: "python"})
            self.assertIn(name, str(stopped.exception))
        with (
            self.subTest(entry="brief_once"),
            mock.patch.object(door.boring_config, "load", return_value={"vector": {"backend": "surreal"}}),
        ):
            self.assertEqual(brief_once.main(["--dsn", "postgres://unused"]), 2)

    def test_pgvector_with_python_pg_readers_starts(self) -> None:
        _start("pgvector", {"DOOR_MEMORY_READER": "python", "DOOR_BRIEF_OWNER": "python"})


if __name__ == "__main__":
    unittest.main()
