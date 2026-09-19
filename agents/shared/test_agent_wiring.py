#!/usr/bin/env python3
"""Regression tests for agent_wiring.py.

Run: python3 agents/shared/test_agent_wiring.py   (no pytest dependency)

Guards the installer surface that is otherwise only exercised at install time:
  - install() must report failures instead of swallowing them.
  - hermes-agent must not be reported as "unsupported".
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase, mock

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


def test_codex_calls_wire_codex():
    """codex wiring includes MCP config plus the host collector worker."""
    with mock.patch.object(
        agent_wiring,
        "wire_codex",
        return_value={
            "agent": "codex",
            "path": "~/.codex/mcp.json",
            "changed": False,
            "worker_kind": "launchd",
            "worker_path": "~/Library/LaunchAgents/com.ohmyboring.codex-ingest.plist",
            "worker_loaded": True,
        },
    ) as mock_wire:
        results, failed = agent_wiring.install(["codex"], "ohmyboring", {})
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


def test_the_same_script_under_a_different_path_spelling_is_not_wired_twice():
    """Recall ran twice on every prompt because two spellings looked like two hooks.

    `/opt/homebrew/bin/python3 ~/oh-my-boring/hooks/recall.py` and
    `python3 <repo>/hooks/recall.py` are the same file: one goes through a symlinked install
    directory and names the interpreter by absolute path. The old substring comparison saw two
    different strings and registered both, so every prompt wrote two identical ledger rows and
    `total_prompts` -- a pre-registered sample floor -- counted double.
    """
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "oh-my-boring"
        (repo / "hooks").mkdir(parents=True)
        (repo / "hooks" / "recall.py").write_text("# recall\n", encoding="utf-8")
        link = Path(d) / "linked"
        link.symlink_to(repo)

        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"/opt/homebrew/bin/python3 {link}/hooks/recall.py",
                            }
                        ],
                    }
                ]
            }
        }

        assert agent_wiring._already_wired(settings, f"python3 {repo}/hooks/recall.py")
        # A different script of ours must still be seen as missing.
        assert not agent_wiring._already_wired(settings, f"python3 {repo}/hooks/distill-session.py")


def test_existing_duplicate_registrations_are_collapsed():
    """Stopping new duplicates is not enough — the machines that already have them keep them."""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "oh-my-boring"
        (repo / "hooks").mkdir(parents=True)
        (repo / "hooks" / "recall.py").write_text("# recall\n", encoding="utf-8")
        link = Path(d) / "linked"
        link.symlink_to(repo)
        ours = f"python3 {repo}/hooks/recall.py"

        # The foreign hook is registered twice on purpose. Somebody else's duplicate is their
        # business; an installer that tidies the whole file would silently delete a hook it was
        # never asked about, and this is the user's live settings.json.
        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": f"python3 {link}/hooks/recall.py"},
                        {"type": "command", "command": "/other/unrelated-hook.sh"},
                    ]},
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": ours},
                        {"type": "command", "command": "/other/unrelated-hook.sh"},
                    ]},
                ]
            }
        }

        removed = agent_wiring._drop_duplicate_hooks(settings, (ours,))

        assert removed == 1, removed
        commands = [
            h["command"]
            for g in settings["hooks"]["UserPromptSubmit"]
            for h in g["hooks"]
        ]
        assert commands.count(f"python3 {link}/hooks/recall.py") == 1, commands
        assert ours not in commands, "the first registration wins; the later copy goes"
        assert commands.count("/other/unrelated-hook.sh") == 2, (
            "a hook we do not own keeps both registrations, duplicate or not"
        )


def test_wire_hermes_adds_hint_and_weekly():
    """Fresh Hermes wiring installs importable briefing scripts and config."""
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
        (scripts / "briefing.py").write_text(
            "import slack_briefing\nDEPENDENCY_PATH = slack_briefing.__file__\n",
            encoding="utf-8",
        )
        (scripts / "slack_briefing.py").write_text(
            'BRIEFING_DEPENDENCY = "installed"\n', encoding="utf-8"
        )
        (scripts / "weekly-briefing.py").write_text("# stub", encoding="utf-8")
        (scripts / "codex-collect-sessions.py").write_text("# stub", encoding="utf-8")
        (scripts / "ingest-worker.py").write_text("# stub", encoding="utf-8")
        installed_scripts = fake_home / ".hermes" / "scripts"
        installed_slack_briefing = installed_scripts / "slack_briefing.py"
        assert not installed_scripts.exists()
        cfg = Path(d) / "config.yaml"
        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser):
            result = agent_wiring.wire_hermes(cfg, boring_home=str(home))
        assert result["changed"] is True
        text = cfg.read_text(encoding="utf-8")
        assert "environment_hint:" in text
        assert "ohmyboring/context" in text
        assert installed_slack_briefing.exists()
        imported = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import briefing; "
                "print(briefing.DEPENDENCY_PATH)",
                str(installed_scripts),
            ],
            capture_output=True,
            text=True,
        )
        assert imported.returncode == 0, imported.stderr
        assert imported.stderr == ""
        assert Path(imported.stdout.strip()).resolve() == installed_slack_briefing.resolve()
        assert (fake_home / ".hermes" / "scripts" / "weekly-briefing.py").exists()
        assert (fake_home / ".hermes" / "scripts" / "codex-collect-sessions.py").exists()
        assert mock_cron.called is True


def test_install_hermes_briefing_backs_up_existing_scripts():
    """Briefing installation preserves the prior scripts as backups."""
    with tempfile.TemporaryDirectory() as d:
        fake_home = Path(d) / "home"
        source_dir = Path(d) / "omb" / "agents" / "hermes"
        source_dir.mkdir(parents=True)
        for name in agent_wiring._HERMES_ENTRY_SCRIPT_NAMES:
            (source_dir / name).write_text(f"# new {name}\n", encoding="utf-8")

        installed_scripts = fake_home / ".hermes" / "scripts"
        installed_scripts.mkdir(parents=True)
        for name in agent_wiring._HERMES_ENTRY_SCRIPT_NAMES:
            (installed_scripts / name).write_text(f"# old {name}\n", encoding="utf-8")

        def fake_expanduser(value):
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        sources = agent_wiring._hermes_briefing_sources(str(Path(d) / "omb"))
        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser):
            agent_wiring._install_hermes_briefing(sources)

        assert sources, "installer resolved nothing to copy"
        for src in sources:
            installed = installed_scripts / src.name
            assert installed.read_text(encoding="utf-8") == f"# new {src.name}\n"
            assert Path(str(installed) + ".omb-bak").read_text(
                encoding="utf-8"
            ) == f"# old {src.name}\n"


def test_real_hermes_entry_scripts_ship_every_module_they_import():
    """The shipped scripts must not import a sibling the installer leaves behind.

    Deliberately run against the repo's own files rather than stubs. The defect this guards
    against was invisible to the stub fixtures: `weekly-briefing.py` imported `weekly_trend`,
    the installer copied neither, and the fixture wrote `# stub` in place of the real file, so
    the import that would have failed in production was never in the test's reach.
    """
    repo = HERE.parent.parent
    src_dir = repo / "agents" / "hermes"
    shared_dir = repo / "agents" / "shared"
    installed = {src.name for src in agent_wiring._hermes_briefing_sources(str(repo))}
    installed |= set(agent_wiring._HERMES_SEPARATELY_INSTALLED)

    for name in agent_wiring._HERMES_ENTRY_SCRIPT_NAMES:
        for dep in agent_wiring._local_module_deps(src_dir / name, (shared_dir,)):
            assert dep.name in installed, (
                f"{name} imports {dep.name}, which the installer never copies to"
                " ~/.hermes/scripts"
            )


def test_local_deps_are_transitive_and_reach_the_shared_dir():
    """Deps of deps ship; a shared-dir module ships; stdlib does not; a vanished one raises."""
    with tempfile.TemporaryDirectory() as d:
        src_dir = Path(d) / "hermes"
        shared_dir = Path(d) / "shared"
        src_dir.mkdir()
        shared_dir.mkdir()
        (src_dir / "entry.py").write_text(
            "import json\nfrom middle import thing\nimport floors\n", encoding="utf-8"
        )
        (src_dir / "middle.py").write_text("import leaf\nthing = leaf\n", encoding="utf-8")
        (src_dir / "leaf.py").write_text("value = 1\n", encoding="utf-8")
        # Lives beside the host tooling, not the briefing -- a measurement floor must not be
        # copied into the renderer just because the two sit in different folders.
        (shared_dir / "floors.py").write_text("MIN_COMPARED = 20\n", encoding="utf-8")

        deps = agent_wiring._local_module_deps(src_dir / "entry.py", (shared_dir,))

        assert deps == {
            src_dir / "middle.py",
            src_dir / "leaf.py",
            shared_dir / "floors.py",
        }, deps

        (shared_dir / "floors.py").unlink()
        try:
            agent_wiring._local_module_deps(src_dir / "entry.py", (shared_dir,))
        except FileNotFoundError as exc:
            assert "floors.py" in str(exc), exc
        else:
            raise AssertionError("a missing module must abort the install")


def test_wire_hermes_missing_slack_briefing_has_no_side_effects():
    """Missing slack_briefing aborts before config backup or script installation."""
    with tempfile.TemporaryDirectory() as d:
        fake_home = Path(d) / "home"
        source_dir = Path(d) / "omb" / "agents" / "hermes"
        source_dir.mkdir(parents=True)
        # The entry points are all present; the module one of them imports is not.
        (source_dir / "briefing.py").write_text("import slack_briefing\n", encoding="utf-8")
        (source_dir / "weekly-briefing.py").write_text("# stub\n", encoding="utf-8")
        (source_dir / "codex-collect-sessions.py").write_text("# stub\n", encoding="utf-8")
        (source_dir / "ingest-worker.py").write_text("# stub\n", encoding="utf-8")
        cfg = Path(d) / "config.yaml"
        original = b"agent:\n  environment_hint: 'keep exactly'\n"
        cfg.write_bytes(original)

        def fake_expanduser(value):
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        with mock.patch.object(
            agent_wiring.os.path, "expanduser", side_effect=fake_expanduser
        ), TestCase().assertRaisesRegex(FileNotFoundError, "slack_briefing.py"):
            agent_wiring.wire_hermes(cfg, boring_home=str(Path(d) / "omb"))

        assert cfg.read_bytes() == original
        assert not Path(str(cfg) + ".omb-bak").exists()
        assert not (fake_home / ".hermes" / "scripts").exists()


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


def test_install_codex_host_worker_macos_writes_launch_agent():
    """The default codex adapter creates a visible host-side collector schedule."""
    with tempfile.TemporaryDirectory() as d:
        fake_home = Path(d) / "home"
        omb = Path(d) / "omb"
        collector = omb / "agents" / "codex" / "collect-sessions.py"
        collector.parent.mkdir(parents=True)
        collector.write_text("# stub", encoding="utf-8")

        def fake_expanduser(value):
            if value == "~":
                return str(fake_home)
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        completed = mock.Mock(returncode=0)
        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser), mock.patch.object(
            agent_wiring.subprocess, "run", return_value=completed
        ):
            result = agent_wiring._install_codex_host_worker_macos(str(omb))

        plist = fake_home / "Library" / "LaunchAgents" / "com.ohmyboring.codex-ingest.plist"
        text = plist.read_text(encoding="utf-8")
        assert result["kind"] == "launchd"
        assert result["loaded"] is True
        assert str(collector) in text
        assert "<integer>1200</integer>" in text
        assert "CODEX_INCLUDE_ROLLOUTS=1" in text
        assert "COLLECT_STABLE_AGE_SECONDS=1800" in text


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
        # weekly-briefing (managed from config) + morning-briefing (preserved) + memory-ingest-worker.
        # No codex job: the host worker (launchd/crontab) owns that schedule — see below.
        assert len(saved["jobs"]) == 3
        weekly = next(j for j in saved["jobs"] if j["name"] == "weekly-briefing")
        assert weekly["script"] == "weekly-briefing.py"
        assert weekly["enabled"] is True
        assert weekly["deliver"] == "slack:test"
        worker = next(j for j in saved["jobs"] if j["name"] == "memory-ingest-worker")
        assert worker["script"] == "ingest-worker.py"
        assert worker["schedule"] == {"kind": "interval", "minutes": 20, "display": "every 20m"}
        assert worker["skill"] == "memory-ingest"
        assert [j["name"] for j in saved["jobs"] if j["name"] == "codex-memory-ingest-worker"] == [], (
            "the host worker already runs the collector every 20m; a hermes job is a second owner"
        )


def test_install_removes_a_hermes_codex_job_left_by_an_older_install():
    """An install that ran before this change left the duplicate behind, and disabling it by hand
    did not survive the next install. The removal has to happen on the way through, or the two
    schedulers stay and `doctor` keeps reporting `dual codex scheduler active`."""
    jobs = [
        {"name": "morning-briefing", "deliver": "slack:test"},
        {"name": "codex-memory-ingest-worker", "script": "codex-collect-sessions.py", "enabled": True},
    ]
    assert agent_wiring._retire_codex_memory_ingest_worker(jobs) is True
    assert [j["name"] for j in jobs] == ["morning-briefing"]
    assert agent_wiring._retire_codex_memory_ingest_worker(jobs) is False, (
        "nothing to remove is not a change — an install that changes nothing must not rewrite the file"
    )


def test_install_places_ingest_worker_and_job_uses_relative_script():
    """--install must place ingest-worker.py in ~/.hermes/scripts AND point the job at the
    relative name — either alone leaves the job blocked with 'script path resolves outside
    the scripts directory' on every tick."""
    repo = HERE.parent.parent
    with tempfile.TemporaryDirectory() as d, mock.patch.object(
        agent_wiring.boring_config, "hermes_cron_jobs", return_value={}
    ):
        fake_home = Path(d) / "home"

        def fake_expanduser(value):
            if value == "~":
                return str(fake_home)
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        cfg = Path(d) / "config.yaml"
        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser):
            agent_wiring.wire_hermes(cfg, boring_home=str(repo))

        installed = fake_home / ".hermes" / "scripts" / "ingest-worker.py"
        assert installed.exists(), "the worker must be installed where hermes resolves scripts"

        data = json.loads(
            (fake_home / ".hermes" / "cron" / "jobs.json").read_text(encoding="utf-8")
        )
        worker = next(j for j in data["jobs"] if j["name"] == "memory-ingest-worker")
        assert worker["script"] == "ingest-worker.py"
        assert not os.path.isabs(worker["script"])


def test_installed_ingest_worker_imports_resolve_from_the_scripts_dir():
    """Whatever the worker imports at runtime must be present beside the installed copy —
    asserted by importing, the way the briefing install's test does."""
    repo = HERE.parent.parent
    with tempfile.TemporaryDirectory() as d, mock.patch.object(
        agent_wiring.boring_config, "hermes_cron_jobs", return_value={}
    ):
        fake_home = Path(d) / "home"

        def fake_expanduser(value):
            if value == "~":
                return str(fake_home)
            if value.startswith("~/"):
                return str(fake_home / value[2:])
            return value

        cfg = Path(d) / "config.yaml"
        with mock.patch.object(agent_wiring.os.path, "expanduser", side_effect=fake_expanduser):
            agent_wiring.wire_hermes(cfg, boring_home=str(repo))

        installed_scripts = (fake_home / ".hermes" / "scripts").resolve()
        imported = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import pathlib, runpy, sys; "
                "ns = runpy.run_path(sys.argv[1], run_name='ingest_worker'); "
                "print(pathlib.Path(ns['distill_core'].__file__).resolve())",
                str(installed_scripts / "ingest-worker.py"),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "BORING_HOME": str(repo), "BORING_IN_CONTAINER": "0"},
        )
        assert imported.returncode == 0, imported.stderr
        assert Path(imported.stdout.strip()) == installed_scripts / "distill_core.py"


def _load_ingest_worker():
    """Import agents/hermes/ingest-worker.py (hyphenated name) for direct unit tests."""
    src = HERE.parent / "hermes" / "ingest-worker.py"
    spec = importlib.util.spec_from_file_location("omb_ingest_worker_test", src)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_ingest_worker_skips_session_inside_stability_window():
    """A transcript modified within COLLECT_STABLE_AGE_SECONDS is still open — offering it
    would pay for a note the SessionEnd hook is about to write itself."""
    worker = _load_ingest_worker()
    original_mark_dir = worker.markers.MARK_DIR
    try:
        with tempfile.TemporaryDirectory() as d:
            worker.markers.set_mark_dir(str(Path(d) / "markers"))
            fresh = Path(d) / "s-fresh.jsonl"
            fresh.write_text("{}\n", encoding="utf-8")
            settled = Path(d) / "s-settled.jsonl"
            settled.write_text("{}\n", encoding="utf-8")
            old = time.time() - 2 * worker.STABLE_AGE_S
            os.utime(settled, (old, old))
            assert worker._eligible(str(fresh)) is False
            assert worker._eligible(str(settled)) is True
    finally:
        worker.markers.set_mark_dir(original_mark_dir)


def test_sync_hermes_cron_jobs_repairs_blocked_absolute_worker_path():
    """The job as installed until now points at a /host absolute path hermes refuses; the next
    --install must rewrite it to the relative name rather than re-assert the blocked path."""
    existing = {
        "jobs": [
            {
                "name": "memory-ingest-worker",
                "script": "/host/oh-my-boring/agents/hermes/ingest-worker.py",
                "schedule": {"kind": "interval", "minutes": 20, "display": "every 20m"},
                "schedule_display": "every 20m",
                "enabled": True,
                "state": "scheduled",
                "skill": "memory-ingest",
                "skills": ["memory-ingest"],
                "no_agent": False,
            }
        ]
    }
    with tempfile.TemporaryDirectory() as d, mock.patch.object(
        agent_wiring.boring_config, "hermes_cron_jobs", return_value={}
    ), mock.patch.object(
        agent_wiring, "_load_json", return_value=existing
    ), mock.patch.object(agent_wiring, "_save_json") as mock_save:
        jobs_path = Path(d) / "jobs.json"
        with mock.patch.object(Path, "expanduser", return_value=jobs_path):
            result = agent_wiring._sync_hermes_cron_jobs()
        assert result["changed"] is True
        saved = mock_save.call_args[0][1]
        worker = next(j for j in saved["jobs"] if j["name"] == "memory-ingest-worker")
        assert worker["script"] == "ingest-worker.py"
        assert worker["schedule"] == {"kind": "interval", "minutes": 20, "display": "every 20m"}
        assert worker["skill"] == "memory-ingest"
        assert worker["enabled"] is True




def test_kimi_hooks_are_deduped_across_path_spellings():
    """One registration per script, whichever way the path was spelled when it was written.

    Two spellings of the same file — through the `~/oh-my-boring` symlink and through its target —
    both passed the old substring test, so a second `[[hooks]]` block was appended and the recall
    hook fired twice on every prompt. That does not move the uptake rate (numerator and
    denominator both double) but it halves the evidence behind a pre-registered sample floor
    (docs/PRD.md §8 D1). Measured on the real config 2026-09-02: four registrations of two hooks.

    Also asserts the blocks the installer does not own are left alone. A deduper that tidies away
    somebody else's hook is worse than the duplicate.
    """
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "oh-my-boring"
        (home / "hooks").mkdir(parents=True)
        for name in ("kimi-recall.py", "kimi-distill-session.py"):
            (home / "hooks" / name).write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        link = Path(d) / "linked"
        link.symlink_to(home)

        config = Path(d) / "config.toml"
        config.write_text(
            "[model]\nname = \"kimi\"\n"
            "\n[[hooks]]\nevent = \"UserPromptSubmit\"\n"
            f'command = "python3 {home}/hooks/kimi-recall.py"\ntimeout = 10\n'
            "\n[[hooks]]\nevent = \"UserPromptSubmit\"\n"
            f'command = "python3 {link}/hooks/kimi-recall.py"\ntimeout = 10\n'
            "\n[[hooks]]\nevent = \"SessionEnd\"\n"
            f'command = "python3 {home}/hooks/kimi-distill-session.py"\ntimeout = 130\n'
            # Twice on purpose. With one copy, "a foreign hook survives" is true even for a
            # deduper that removes every duplicate it finds regardless of owner — the assertion
            # passes because there was nothing to delete. Duplicated, it fails.
            '\n[[hooks]]\nevent = "SessionEnd"\n'
            'command = "python3 /somebody/elses/hook.py"\ntimeout = 5\n'
            '\n[[hooks]]\nevent = "SessionEnd"\n'
            'command = "python3 /somebody/elses/hook.py"\ntimeout = 5\n',
            encoding="utf-8",
        )

        with mock.patch.object(agent_wiring, "BORING_HOME", str(home)):
            agent_wiring.wire_kimi(config)

        text = config.read_text(encoding="utf-8")
        assert text.count("kimi-recall.py") == 1, text
        assert text.count("kimi-distill-session.py") == 1, text
        assert text.count("/somebody/elses/hook.py") == 2, (
            "a hook we do not own must survive untouched — including its own duplicates, which"
            " are not ours to tidy away"
        )
        assert 'name = "kimi"' in text, "the rest of the config must survive"

if __name__ == "__main__":
    test_install_reports_failure()
    test_install_returns_success_when_ok()
    test_hermes_agent_calls_wire_hermes()
    test_codex_calls_wire_codex()
    test_unsupported_agent_is_skipped_without_failure()
    test_settings_path_override()
    test_default_path_when_no_override()
    test_wire_claude_code_adds_session_start()
    test_the_same_script_under_a_different_path_spelling_is_not_wired_twice()
    test_existing_duplicate_registrations_are_collapsed()
    test_wire_hermes_adds_hint_and_weekly()
    test_install_hermes_briefing_backs_up_existing_scripts()
    test_wire_hermes_missing_slack_briefing_has_no_side_effects()
    test_real_hermes_entry_scripts_ship_every_module_they_import()
    test_kimi_hooks_are_deduped_across_path_spellings()
    test_local_deps_are_transitive_and_reach_the_shared_dir()
    test_install_hermes_skills_removes_legacy_nested_duplicate()
    test_install_codex_host_worker_macos_writes_launch_agent()
    test_next_cron_run_finds_next_monday()
    test_sync_hermes_cron_jobs_adds_managed_job()
    test_install_places_ingest_worker_and_job_uses_relative_script()
    test_installed_ingest_worker_imports_resolve_from_the_scripts_dir()
    test_ingest_worker_skips_session_inside_stability_window()
    test_sync_hermes_cron_jobs_repairs_blocked_absolute_worker_path()
    test_install_removes_a_hermes_codex_job_left_by_an_older_install()
    print("ok - agent_wiring failure propagation + hermes wiring + settings_path")
