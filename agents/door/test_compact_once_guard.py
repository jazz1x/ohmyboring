"""compact_once: 사본 DB(--dsn != DOOR_PG_DSN)는 사건이 격리돼 있지 않으면 일하기 전에 거부된다."""

from __future__ import annotations

import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "src")]

compact_once = importlib.import_module("agents.door.compact_once")

COPY_DSN = "postgresql://scratch/copy"
LIVE_DSN = "postgresql://live/boring"


class ForeignDsnRefusedBeforeWork(unittest.TestCase):
    def _run(self, env: dict[str, str]) -> tuple[int, str, mock.Mock]:
        err = io.StringIO()
        connect = mock.Mock(side_effect=RuntimeError("stop after the guard"))
        # 거부 분기를 지운 변이에서도 사건이 문 /events(라이브)로 가지 않게 싱크를 막는다 — 10-23 에 두 번 샜다.
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(compact_once.psycopg, "connect", connect),
            mock.patch.object(compact_once.event_log, "try_append_event", mock.Mock(return_value=True)),
            contextlib.redirect_stderr(err),
        ):
            code = compact_once.main(["--dsn", COPY_DSN])
        return code, err.getvalue(), connect

    def test_copy_dsn_without_isolated_events_exits_2_before_connecting(self) -> None:
        code, err, connect = self._run({"DOOR_PG_DSN": LIVE_DSN})
        self.assertEqual(code, 2)
        self.assertIn("BORING_EVENT_LOG", err)
        connect.assert_not_called()

    def test_copy_dsn_with_temp_spool_gets_past_the_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "DOOR_PG_DSN": LIVE_DSN,
                "BORING_EVENT_SINK": "spool",
                "BORING_EVENT_LOG": os.path.join(tmp, "events.ndjson"),
            }
            code, _, connect = self._run(env)
        self.assertEqual(code, 1)
        connect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
