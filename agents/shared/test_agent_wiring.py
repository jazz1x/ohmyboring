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
        agent_wiring, "_sync_hermes_cron_jobs", return_value={"changed": False, "jobs_count": 3}
    ) as mock_cron:
        fake_home = Path(d) / "home"

        def fake_expanduser(value):
            if value == "~":
                return str(fake_home)
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        home = Path(d) / "omb"
        scripts = home / "agents" / "hermes"
        scripts.mkdir(parents=True)
        (scripts / "briefing.py").write_text("# stub", encoding="utf-8")
        (scripts / "weekly-briefing.py").write_text("# stub", encoding="utf-8")
        (scripts / "codex-collect-sessions.py").write_text("# stub", encoding="utf-8")
        cfg = Path(d) / "config.yaml"
        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser):
            result = agent_wiring.wire_hermes(cfg, boring_home=str(home))
        assert result["changed"] is True
        text = cfg.read_text(encoding="utf-8")
        assert "environment_hint:" in text
        assert "ohmyboring/context" in text
        assert (fake_home / ".hermes" / "scripts" / "weekly-briefing.py").exists()
        assert (fake_home / ".hermes" / "scripts" / "codex-collect-sessions.py").exists()
        assert mock_cron.called is True


def test_install_hermes_skills_removes_legacy_nested_duplicate():
    """Old installs could leave memory-ingest/memory-ingest/SKILL.md and confuse Hermes."""
    with tempfile.TemporaryDirectory() as d:
        fake_home = Path(d) / "home"
        omb = Path(d) / "omb"
        src = omb / "agents" / "hermes" / "skills" / "memory-ingest"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("name: memory-ingest\n", encoding="utf-8")

        dst = fake_home / ".hermes" / "skills" / "memory-ingest"
        nested = dst / "memory-ingest"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("stale duplicate\n", encoding="utf-8")

        def fake_expanduser(value):
            if value == "~":
                return str(fake_home)
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser):
            agent_wiring._install_hermes_skills(str(omb))

        assert (dst / "SKILL.md").exists()
        assert not nested.exists()


def test_next_cron_run_finds_next_monday():
    tz = agent_wiring.datetime.timezone(agent_wiring.datetime.timedelta(hours=9))
    now = agent_wiring.datetime.datetime(2026, 6, 29, 10, 0, 0, tzinfo=tz)  # Monday 10:00
    nxt = agent_wiring._next_cron_run("0 9 * * 1", tz, now)
    assert nxt.weekday() == 0  # Monday
    assert nxt.hour == 9
    assert nxt > now


def test_sync_hermes_cron_jobs_adds_managed_job():
    """_sync_hermes_cron_jobs creates missing managed jobs without touching others."""
    with tempfile.TemporaryDirectory() as d, mock.patch.object(
        agent_wiring.boring_config, "hermes_cron_jobs", return_value={
            "weekly-briefing": {"enabled": True, "schedule": "0 9 * * 1", "script": "weekly-briefing.py"}
        }
    ), mock.patch.object(
        agent_wiring, "_load_json", return_value={
            "jobs": [{"name": "morning-briefing", "deliver": "slack:test"}]
        }
    ), mock.patch.object(agent_wiring, "_save_json") as mock_save:
        jobs_path = Path(d) / "jobs.json"
        with mock.patch.object(Path, "expanduser", return_value=jobs_path):
            result = agent_wiring._sync_hermes_cron_jobs()
        assert result["changed"] is True
        saved = mock_save.call_args[0][1]
        # weekly-briefing (managed from config) + morning-briefing (preserved) + memory-ingest-worker + codex-memory-ingest-worker
        assert len(saved["jobs"]) == 4
        weekly = next(j for j in saved["jobs"] if j["name"] == "weekly-briefing")
        assert weekly["script"] == "weekly-briefing.py"
        assert weekly["enabled"] is True
        assert weekly["deliver"] == "slack:test"
        worker = next(j for j in saved["jobs"] if j["name"] == "memory-ingest-worker")
        assert worker["script"] == "/host/oh-my-boring/agents/hermes/ingest-worker.py"
        assert worker["schedule"] == {"kind": "interval", "minutes": 20, "display": "every 20m"}
        assert worker["skill"] == "memory-ingest"
        codex_worker = next(j for j in saved["jobs"] if j["name"] == "codex-memory-ingest-worker")
        assert codex_worker["script"] == "codex-collect-sessions.py"
        assert codex_worker["schedule"] == {"kind": "interval", "minutes": 20, "display": "every 20m"}
        assert codex_worker["skill"] is None
        assert codex_worker["skills"] == []
        assert codex_worker["no_agent"] is True


if __name__ == "__main__":
    test_install_reports_failure()
    test_install_returns_success_when_ok()
    test_hermes_agent_calls_wire_hermes()
    test_unsupported_agent_is_skipped_without_failure()
    test_settings_path_override()
    test_default_path_when_no_override()
    test_wire_claude_code_adds_session_start()
    test_wire_hermes_adds_hint_and_weekly()
    test_install_hermes_skills_removes_legacy_nested_duplicate()
    test_next_cron_run_finds_next_monday()
    test_sync_hermes_cron_jobs_adds_managed_job()
    test_wire_hermes_adds_hint_and_weekly()
    print("ok - agent_wiring failure propagation + hermes wiring + settings_path")
