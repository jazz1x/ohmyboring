#!/usr/bin/env python3
"""Regression tests for agent_wiring.py.

Run: python3 agents/shared/test_agent_wiring.py   (no pytest dependency)

Guards the installer surface that is otherwise only exercised at install time:
  - install() must report failures instead of swallowing them.
  - hermes-agent must not be reported as "unsupported".
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Import the module under test the same way the installed script does.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("BORING_CONFIG", None)
os.environ.pop("BORING_HOME", None)

import agent_wiring


def test_install_reports_failure():
    with mock.patch.object(
        agent_wiring, "wire_claude_code", side_effect=PermissionError("denied")
    ):
        results, failed = agent_wiring.install(["claude-code"], "ohmyboring", {})
    assert failed is True, "install() must return failed=True when a wire raises"
    assert results == [], "no successful result should be returned for a failed agent"


def test_install_returns_success_when_ok():
    with mock.patch.object(
        agent_wiring, "wire_claude_code", return_value={"agent": "claude-code", "changed": False}
    ):
        results, failed = agent_wiring.install(["claude-code"], "ohmyboring", {})
    assert failed is False
    assert len(results) == 1


def test_hermes_agent_calls_wire_hermes():
    """hermes-agent is wired via wire_hermes() (config.yaml + briefing template)."""
    with mock.patch.object(
        agent_wiring, "wire_hermes", return_value={"agent": "hermes-agent", "changed": False}
    ) as mock_wire:
        results, failed = agent_wiring.install(["hermes-agent"], "ohmyboring", {})
    assert failed is False
    assert len(results) == 1
    assert mock_wire.called is True


def test_unsupported_agent_is_skipped_without_failure():
    results, failed = agent_wiring.install(["nonexistent-agent"], "ohmyboring", {})
    assert failed is False
    assert results == []


def test_settings_path_override():
    """boring.json settings_path wins over the hardcoded default."""
    custom = Path(os.path.expanduser("~/custom-claude-settings.json"))
    cfg = {
        "agents": [
            {
                "id": "claude-code",
                "enabled": True,
                "settings_path": str(custom),
            }
        ]
    }
    with mock.patch.object(agent_wiring.boring_config, "load", return_value=cfg):
        assert agent_wiring._agent_path("claude-code") == custom


def test_default_path_when_no_override():
    """When settings_path is absent, the per-agent default is used."""
    with mock.patch.object(agent_wiring.boring_config, "load", return_value={}):
        assert agent_wiring._agent_path("claude-code") == Path(
            os.path.expanduser("~/.claude/settings.json")
        )


def test_wire_claude_code_adds_session_start():
    """Claude Code wiring adds a SessionStart recall hook alongside existing hooks."""
    with tempfile.TemporaryDirectory() as d:
        settings = Path(d) / "settings.json"
        result = agent_wiring.wire_claude_code(settings)
        assert result["changed"] is True
        data = json.loads(settings.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert "SessionStart" in hooks
        commands = [
            h.get("command")
            for group in hooks["SessionStart"]
            for h in group.get("hooks", [])
        ]
        assert any("session-start-recall.py" in c for c in commands)


def test_wire_hermes_adds_hint_and_weekly():
    """hermes wiring installs hint + weekly script and updates config.yaml."""
    with tempfile.TemporaryDirectory() as d, mock.patch.object(
        agent_wiring, "_ensure_hermes_cron_job", return_value=False
    ) as mock_cron:
        home = Path(d) / "omb"
        scripts = home / "agents" / "hermes"
        scripts.mkdir(parents=True)
        (scripts / "briefing.py").write_text("# stub", encoding="utf-8")
        (scripts / "weekly-briefing.py").write_text("# stub", encoding="utf-8")
        cfg = Path(d) / "config.yaml"
        result = agent_wiring.wire_hermes(cfg, boring_home=str(home))
        assert result["changed"] is True
        text = cfg.read_text(encoding="utf-8")
        assert "environment_hint:" in text
        assert "ohmyboring/context" in text
        assert (Path(os.path.expanduser("~/.hermes/scripts")) / "weekly-briefing.py").exists()
        assert mock_cron.called is True


if __name__ == "__main__":
    test_install_reports_failure()
    test_install_returns_success_when_ok()
    test_hermes_agent_calls_wire_hermes()
    test_unsupported_agent_is_skipped_without_failure()
    test_settings_path_override()
    test_default_path_when_no_override()
    test_wire_claude_code_adds_session_start()
    test_wire_hermes_adds_hint_and_weekly()
    print("ok - agent_wiring failure propagation + hermes wiring + settings_path")
