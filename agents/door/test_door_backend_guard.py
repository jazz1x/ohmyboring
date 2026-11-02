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

_PYTHON_WRITERS = ("DOOR_REMEMBER_WRITER", "DOOR_SYNC_OWNER", "DOOR_LINK_WRITER")


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


if __name__ == "__main__":
    unittest.main()
