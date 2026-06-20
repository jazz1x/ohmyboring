#!/usr/bin/env python3
"""Regression tests for agent_wiring.py.

Run: python3 agents/shared/test_agent_wiring.py   (no pytest dependency)

Guards the installer surface that is otherwise only exercised at install time:
  - install() must report failures instead of swallowing them.
  - hermes-agent must not be reported as "unsupported".
"""
import os
import sys
from pathlib import Path
from unittest import mock

# Import the module under test the same way the installed script does.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("BORING_CONFIG", None)
os.environ.pop("OMB_HOME", None)

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


def test_hermes_agent_is_not_unsupported():
    """hermes-agent lives in a container and does not need host-side wiring."""
    with mock.patch.object(agent_wiring, "wire_claude_code") as mock_wire:
        results, failed = agent_wiring.install(["hermes-agent"], "ohmyboring", {})
    assert failed is False
    assert results == []
    assert mock_wire.called is False


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


if __name__ == "__main__":
    test_install_reports_failure()
    test_install_returns_success_when_ok()
    test_hermes_agent_is_not_unsupported()
    test_unsupported_agent_is_skipped_without_failure()
    test_settings_path_override()
    test_default_path_when_no_override()
    print("ok - agent_wiring failure propagation + hermes skip + settings_path")
