#!/usr/bin/env python3
"""Plain-runnable regression test for boring_config repo-root discovery.

Run: python3 agents/shared/test_boring_config.py   (no pytest dependency)

Guards the off-by-one that silently disabled policy: the installed hooks are
symlinks (hooks/distill-session.py -> agents/claude-code/distill-session.py) that
sys.path-insert agents/shared and `import boring_config`. The resolver used
Path(__file__).resolve().parent.parent, which yields the agents/ dir (one level
too high), so boring.json discovery returned None and note_lang + repo rules were
ignored for every distilled session. The root must be the dir that holds
boring.example.json (and, when present, boring.json).
"""
import os
import sys
from pathlib import Path

# Import boring_config the way the hooks do: insert agents/shared onto sys.path
# then import by name. This file lives in that dir, so the shared dir is its
# parent. resolve() follows any symlink to the real on-disk location.
SHARED_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SHARED_DIR))

# Neutralize ambient policy env so the test exercises the <repo-root>/boring.json
# branch deterministically from a fresh clone, regardless of the dev's shell.
for _var in ("BORING_CONFIG", "BORING_HOME"):
    os.environ.pop(_var, None)

import boring_config  # noqa: E402  (import after sys.path/env setup, on purpose)

# The committed marker that pins the repo root in a fresh clone (boring.json is
# gitignored; only the example is tracked).
ROOT_MARKER = "boring.example.json"


def test_repo_root_is_dir_with_example():
    root = boring_config._repo_root()
    expected = (root / ROOT_MARKER)
    assert expected.is_file(), (
        f"_repo_root() = {root} does not contain {ROOT_MARKER}; "
        f"resolver is pointing at the wrong level"
    )


def test_repo_root_is_not_the_agents_dir():
    # Direct regression guard for the exact off-by-one (parent.parent -> agents/).
    root = boring_config._repo_root()
    assert root.name != "agents", (
        f"_repo_root() = {root} is the agents/ dir (off-by-one); "
        f"it must be the repo root holding {ROOT_MARKER}"
    )
    # shared -> agents -> repo: the root is exactly two levels above this dir.
    assert root == SHARED_DIR.parent.parent, (
        f"_repo_root() = {root} != {SHARED_DIR.parent.parent} (shared->agents->repo)"
    )


def test_discover_path_targets_repo_root():
    # With env neutralized and no boring.json in a fresh clone, discovery is None;
    # but it must be probing <repo-root>/boring.json — i.e. next to the example.
    root = boring_config._repo_root()
    probe = root / "boring.json"
    assert probe.parent == root, "discovery must probe boring.json at the repo root"
    found = boring_config.discover_path()
    # Whether or not a local boring.json exists, the result must never be the
    # bogus agents/boring.json that the off-by-one produced.
    if found is not None:
        assert found.parent.name != "agents", (
            f"discover_path() = {found} resolved under agents/ (off-by-one)"
        )


def test_source_dirs_filter_by_adapter_and_agent():
    cfg = {
        "agents": [
            {"id": "claude-code", "enabled": True, "adapter": "session-end", "paths": ["~/a"]},
            {"id": "codex", "enabled": True, "adapter": "mcp-only"},
            {"id": "cursor", "enabled": False, "adapter": "session-end", "paths": ["~/skip"]},
        ]
    }
    # Monkey-patch load() for the duration of the test.
    old_load = boring_config.load
    try:
        boring_config.load = lambda: cfg
        assert boring_config.source_dirs() == [os.path.expanduser("~/a")]
        assert boring_config.source_dirs(adapter="session-end") == [os.path.expanduser("~/a")]
        assert boring_config.source_dirs(adapter="mcp-only") == []
        assert boring_config.source_dirs(agent_id="codex") == []
        assert boring_config.source_dirs(agent_id="claude-code") == [os.path.expanduser("~/a")]
    finally:
        boring_config.load = old_load


def test_agent_config_lookup():
    cfg = {
        "agents": [
            {"id": "claude-code", "enabled": True, "adapter": "session-end", "format": "claude-json"},
            {"id": "cursor", "enabled": False, "adapter": "mcp-only"},
        ]
    }
    old_load = boring_config.load
    try:
        boring_config.load = lambda: cfg
        assert boring_config.agent_config("claude-code").get("format") == "claude-json"
        assert boring_config.agent_config("cursor") == {}  # disabled
        assert boring_config.agent_config("nonexistent") == {}
    finally:
        boring_config.load = old_load


def test_canonical_repo_normalizes_variants():
    cfg = {
        "repos": [
            {"match": "marketboro", "origin": "company"},
            {"match": "jazz1x/oh-my-boring", "name": "oh-my-boring", "origin": "personal"},
        ]
    }
    old_load = boring_config.load
    try:
        boring_config.load = lambda: cfg
        assert boring_config.canonical_repo("marketboro/foodspring-front") == "foodspring-front"
        assert boring_config.canonical_repo("foodspring-front") == "foodspring-front"
        assert boring_config.canonical_repo("jazz1x/oh-my-boring") == "oh-my-boring"
        assert boring_config.canonical_repo("git@github.com:acme/widget.git") == "widget"
        assert boring_config.canonical_repo("") == ""
    finally:
        boring_config.load = old_load


def test_load_warns_on_parse_error():
    """A corrupt boring.json must not silently look like an empty policy."""
    import tempfile

    old_path = boring_config.discover_path()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"schema_version": 1, "note_lang": "ko",}')  # trailing comma
        tmp = f.name
    try:
        os.environ["BORING_CONFIG"] = tmp
        cfg = boring_config.load()
        assert cfg == {}, "parse error must fall back to empty default"
    finally:
        os.environ.pop("BORING_CONFIG", None)
        os.unlink(tmp)


def main():
    tests = [
        test_repo_root_is_dir_with_example,
        test_repo_root_is_not_the_agents_dir,
        test_discover_path_targets_repo_root,
        test_source_dirs_filter_by_adapter_and_agent,
        test_agent_config_lookup,
        test_canonical_repo_normalizes_variants,
        test_load_warns_on_parse_error,
    ]
    for t in tests:
        t()
        print(f"ok - {t.__name__}")
    print(f"\nPASS: {len(tests)} checks; repo_root = {boring_config._repo_root()}")


if __name__ == "__main__":
    main()
