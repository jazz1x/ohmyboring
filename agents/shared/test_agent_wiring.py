import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared")
)
import agent_wiring


class WireMcpAgentTest(unittest.TestCase):
    def _patch_cursor(self, tmp_home: Path):
        self._orig = copy.deepcopy(agent_wiring.AGENTS["cursor"])
        agent_wiring.AGENTS["cursor"]["path"] = str(tmp_home / ".cursor/mcp.json")

    def _restore_cursor(self):
        agent_wiring.AGENTS["cursor"] = self._orig

    def test_creates_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._patch_cursor(home)
            try:
                result = agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", {"type": "http", "url": "http://x/mcp"}
                )
                self.assertTrue(result["changed"])
                data = json.loads((home / ".cursor/mcp.json").read_text())
                self.assertEqual(
                    data["mcpServers"]["ohmyboring"]["url"], "http://x/mcp"
                )
            finally:
                self._restore_cursor()

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._patch_cursor(home)
            try:
                server = {"type": "http", "url": "http://x/mcp"}
                agent_wiring.wire_mcp_agent("cursor", "ohmyboring", server)
                result = agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", server
                )
                self.assertFalse(result["changed"])
            finally:
                self._restore_cursor()

    def test_updates_changed_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._patch_cursor(home)
            try:
                agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", {"type": "http", "url": "http://old/mcp"}
                )
                result = agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", {"type": "http", "url": "http://new/mcp"}
                )
                self.assertTrue(result["changed"])
                data = json.loads((home / ".cursor/mcp.json").read_text())
                self.assertEqual(
                    data["mcpServers"]["ohmyboring"]["url"], "http://new/mcp"
                )
            finally:
                self._restore_cursor()

    def test_backup_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / ".cursor/mcp.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"existing": true}')
            self._patch_cursor(home)
            try:
                agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", {"type": "http", "url": "http://x/mcp"}
                )
                self.assertTrue((home / ".cursor/mcp.json.omb-bak").exists())
                backup = json.loads((home / ".cursor/mcp.json.omb-bak").read_text())
                self.assertTrue(backup["existing"])
            finally:
                self._restore_cursor()

    def test_backup_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / ".cursor/mcp.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"existing": true}')
            self._patch_cursor(home)
            try:
                agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", {"type": "http", "url": "http://a/mcp"}
                )
                (home / ".cursor/mcp.json.omb-bak").write_text('{"stale": true}')
                agent_wiring.wire_mcp_agent(
                    "cursor", "ohmyboring", {"type": "http", "url": "http://b/mcp"}
                )
                backup = json.loads((home / ".cursor/mcp.json.omb-bak").read_text())
                self.assertTrue(backup["stale"])
            finally:
                self._restore_cursor()


if __name__ == "__main__":
    unittest.main()
