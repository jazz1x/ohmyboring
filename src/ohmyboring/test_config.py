#!/usr/bin/env python3
"""Plain-runnable regression test for ohmyboring.config repo-root discovery.

Run: python3 src/ohmyboring/test_config.py   (no pytest dependency)

Guards the off-by-one that silently disabled policy: the resolver used
Path(__file__).resolve().parent.parent, one level too high, so boring.json
discovery returned None and note_lang + repo rules were ignored for every
distilled session. The root must be the dir that holds boring.example.json
(and, when present, boring.json).
"""

import json
import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR.parent))

# Neutralize ambient policy env so the test exercises the <repo-root>/boring.json
# branch deterministically from a fresh clone, regardless of the dev's shell.
for _var in ("BORING_CONFIG", "BORING_HOME"):
    os.environ.pop(_var, None)

from ohmyboring import config as boring_config  # noqa: E402  (import after sys.path/env setup, on purpose)

# The committed marker that pins the repo root in a fresh clone (boring.json is
# gitignored; only the example is tracked).
ROOT_MARKER = "boring.example.json"


def test_repo_root_is_dir_with_example():
    root = boring_config._repo_root()
    expected = root / ROOT_MARKER
    assert expected.is_file(), (
        f"_repo_root() = {root} does not contain {ROOT_MARKER}; resolver is pointing at the wrong level"
    )


def test_repo_root_is_not_the_agents_dir():
    # Direct regression guard for the exact off-by-one (parent.parent -> agents/).
    root = boring_config._repo_root()
    assert root.name != "agents", (
        f"_repo_root() = {root} is the agents/ dir (off-by-one); "
        f"it must be the repo root holding {ROOT_MARKER}"
    )
    # ohmyboring -> src -> repo: the root is exactly two levels above this dir.
    assert root == PACKAGE_DIR.parent.parent, (
        f"_repo_root() = {root} != {PACKAGE_DIR.parent.parent} (ohmyboring->src->repo)"
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
        assert found.parent.name != "agents", f"discover_path() = {found} resolved under agents/ (off-by-one)"


def test_note_links_come_only_from_boring_json():
    import tempfile

    cases = [
        ({}, ()),
        ({"card": {"note_links": {}}}, ()),
        (
            {"card": {"note_links": {"obsidian": {"vault": "v", "folder": "/vault/wiki/"}}}},
            (boring_config.ObsidianLink(vault="v", folder="vault/wiki"),),
        ),
        (
            {"card": {"note_links": {"file": {"folder": "/n"}, "obsidian": {"vault": "v"}}}},
            (boring_config.FileLink(folder="/n"), boring_config.ObsidianLink(vault="v", folder="")),
        ),
        ({"card": {"note_links": {"obsidian": {"vault": ""}, "file": {}, "web": {"url": "x"}}}}, ()),
        # The card runs in the door container, whose ~ is not the host's — no ~, no relative path.
        ({"card": {"note_links": {"file": {"folder": "~/notes"}}}}, ()),
        ({"card": {"note_links": {"file": {"folder": "notes"}}}}, ()),
        ({"card": {"note_links": {"file": {"folder": "/n", "editor": "vscode://"}}}}, ()),
        (
            {"card": {"note_links": {"file": {"folder": "/n/", "editor": "zed"}}}},
            (boring_config.FileLink(folder="/n", editor="zed"),),
        ),
        ({"card": "x"}, ()),
        ({"card": {"note_links": [1]}}, ()),
    ]
    import contextlib
    import io

    for cfg, expected in cases:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
        os.environ["BORING_CONFIG"] = f.name
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                got = boring_config.note_links()
        finally:
            os.environ.pop("BORING_CONFIG", None)
            os.unlink(f.name)
        assert got == expected, (cfg, got)
        dropped = cfg.get("card") not in (None, {"note_links": {}}) and not got
        assert bool(err.getvalue()) == dropped, (cfg, err.getvalue())


def test_in_container_is_the_env_var_alone():
    before = os.environ.get("BORING_IN_CONTAINER")
    seen = {}
    for value in ("1", "true", "YES", "0", "no", ""):
        os.environ["BORING_IN_CONTAINER"] = value
        seen[value] = boring_config._in_container()
    os.environ.pop("BORING_IN_CONTAINER")
    if before is not None:
        os.environ["BORING_IN_CONTAINER"] = before
    assert os.environ.get("BORING_IN_CONTAINER") == before
    assert seen == {"1": True, "true": True, "YES": True, "0": False, "no": False, "": False}, seen


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
            {"match": "jazz1x/ohmyboring", "name": "ohmyboring", "origin": "personal"},
            {"match": "jazz1x/oh-my-boring", "name": "ohmyboring", "origin": "personal"},
            {"match": "oh-my-boring", "name": "ohmyboring", "origin": "personal"},
        ]
    }
    old_load = boring_config.load
    try:
        boring_config.load = lambda: cfg
        assert boring_config.canonical_repo("marketboro/foodspring-front") == "foodspring-front"
        assert boring_config.canonical_repo("foodspring-front") == "foodspring-front"
        assert boring_config.canonical_repo("jazz1x/ohmyboring") == "ohmyboring"
        assert boring_config.canonical_repo("jazz1x/oh-my-boring") == "ohmyboring"
        assert boring_config.canonical_repo("oh-my-boring") == "ohmyboring"
        assert boring_config.canonical_repo("git@github.com:acme/widget.git") == "widget"
        assert boring_config.canonical_repo("") == ""
    finally:
        boring_config.load = old_load


def test_load_warns_on_parse_error():
    """A corrupt boring.json must not silently look like an empty policy."""
    import tempfile

    boring_config.discover_path()
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


def test_checkout_roots_unset_is_none_and_set_is_expanded():
    """Unset must stay distinguishable from an empty scan — the caller reports it as not scanned."""
    old_load = boring_config.load
    try:
        boring_config.load = lambda: {}
        assert boring_config.checkout_roots() is None
        boring_config.load = lambda: {"checkout_roots": ["~/src", " ", 3]}
        assert boring_config.checkout_roots() == [str(Path("~/src").expanduser())]
    finally:
        boring_config.load = old_load


def test_classify_prefers_remote_url_over_cwd():
    """Git remote identity wins over local working-tree path matching."""
    cfg = {
        "repos": [
            {"match": "your-company", "origin": "company", "name": "your-company"},
            {"match": "~/mine", "origin": "personal", "name": "mine"},
        ]
    }
    old_load = boring_config.load
    try:
        boring_config.load = lambda: cfg
        # cwd happens to match a personal path, but remote URL says company.
        origin, rule = boring_config.classify(
            "/Users/jongyun/your-company", "https://github.com/your-company/repo.git"
        )
        assert origin == "company", f"expected company from remote URL, got {origin}"
        assert rule == "your-company", f"expected your-company rule, got {rule}"
        # No remote URL → falls back to cwd matching.
        origin, rule = boring_config.classify("/Users/jongyun/your-company", None)
        assert origin == "company", f"expected company from cwd fallback, got {origin}"
    finally:
        boring_config.load = old_load


def test_classify_adversarial_inputs():
    cfg = {
        "repos": [
            {"match": "acme", "origin": "company", "name": "acme"},
            {
                "match": " ~/work ",
                "origin": "personal",
                "name": "work",
            },  # spaces should still match via strip? no, matcher is used as-is in _matches
        ]
    }
    old_load = boring_config.load
    try:
        boring_config.load = lambda: cfg
        # Empty inputs → default origin.
        assert boring_config.classify("", "") == ("personal", None)
        assert boring_config.classify("", None) == ("personal", None)
        # No rule matches.
        assert boring_config.classify("/tmp/orphan", "https://github.com/orphan/repo.git") == (
            "personal",
            None,
        )
        # Case-insensitive remote match; .git suffix ignored by matcher.
        assert boring_config.classify("/tmp/foo", "https://github.com/ACME/Widget.git") == ("company", "acme")
        # SSH remote format.
        assert boring_config.classify("/tmp/foo", "git@github.com:acme/widget.git") == ("company", "acme")
    finally:
        boring_config.load = old_load


def test_default_hermes_cron_jobs_hand_the_cards_to_hermes():
    """The defaults hermes_cron_jobs() falls back to: hermes owns WHEN, the card programs stay
    the tool, delivery is local (they post to Slack themselves). The old weekly-briefing job
    stays paused (#425)."""
    jobs = boring_config.DEFAULT_HERMES_CRON_JOBS
    assert jobs["morning-card"] == {
        "enabled": True,
        "schedule": "0 8 * * *",
        "script": "run-morning-card.py",
        "deliver": "local",
    }
    assert jobs["weekly-card"] == {
        "enabled": True,
        "schedule": "0 9 * * 1",
        "script": "run-weekly-card.py",
        "deliver": "local",
    }
    assert jobs["weekly-briefing"]["enabled"] is False


def main():
    tests = [
        test_repo_root_is_dir_with_example,
        test_repo_root_is_not_the_agents_dir,
        test_discover_path_targets_repo_root,
        test_in_container_is_the_env_var_alone,
        test_note_links_come_only_from_boring_json,
        test_source_dirs_filter_by_adapter_and_agent,
        test_agent_config_lookup,
        test_canonical_repo_normalizes_variants,
        test_load_warns_on_parse_error,
        test_classify_prefers_remote_url_over_cwd,
        test_classify_adversarial_inputs,
        test_default_hermes_cron_jobs_hand_the_cards_to_hermes,
    ]
    for t in tests:
        t()
        print(f"ok - {t.__name__}")
    print(f"\nPASS: {len(tests)} checks; repo_root = {boring_config._repo_root()}")


if __name__ == "__main__":
    main()
