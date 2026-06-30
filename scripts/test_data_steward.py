#!/usr/bin/env python3
"""Network-free regression tests for scripts/data-steward.py.

Guardrail owned: `--fix` must NEVER produce unparseable frontmatter. The original
line-splice preserved an inline `tags: []` line and appended block entries after
it, yielding YAML that fails to parse (silent vault data loss on re-ingest).
"""
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

# data-steward.py has a hyphen → load it by path.
_HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location("data_steward", os.path.join(_HERE, "data-steward.py"))
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


def _roundtrip_fix(frontmatter: str, body: str = "body.\n", target: str = "omb") -> str:
    """Write a note, run _fix_note, return the rewritten frontmatter block."""
    d = tempfile.mkdtemp()
    wiki = Path(d) / "wiki"
    wiki.mkdir()
    p = wiki / "wiki-0042.md"
    p.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    note = ds._collect_notes(wiki)[0]
    ds._fix_note(note, target)
    out = p.read_text(encoding="utf-8")
    return out[4 : out.find("\n---\n")]


def _assert_parses(fm: str, label: str):
    try:
        yaml.safe_load(fm)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"{label}: rewritten frontmatter is unparseable YAML: {e}\n---\n{fm}\n---")


def test_inline_empty_tags_stays_parseable():
    # The exact red-team repro: empty inline tags + org-prefixed project.
    fm = _roundtrip_fix(
        "id: wiki-0042\ntitle: t\nkind: note\norigin: personal\n"
        "project: marketboro/omb\ntags: []\nsources: []"
    )
    _assert_parses(fm, "inline-empty-tags")
    loaded = yaml.safe_load(fm)
    assert loaded["project"] == "omb", loaded
    # repo/omb tag is added for the canonicalized project.
    assert "repo/omb" in (loaded.get("tags") or []), loaded


def test_inline_nonempty_tags_stays_parseable():
    fm = _roundtrip_fix(
        "id: wiki-0042\ntitle: t\nproject: marketboro/omb\n"
        "tags: [repo/marketboro/omb, effect]\nsources: []"
    )
    _assert_parses(fm, "inline-nonempty-tags")
    loaded = yaml.safe_load(fm)
    assert "effect" in loaded["tags"], loaded


def test_block_tags_stays_parseable():
    fm = _roundtrip_fix(
        "id: wiki-0042\ntitle: t\nproject: marketboro/omb\n"
        "tags:\n- repo/marketboro/omb\n- effect\nsources: []"
    )
    _assert_parses(fm, "block-tags")
    loaded = yaml.safe_load(fm)
    assert "effect" in loaded["tags"], loaded


def test_placeholder_tags_removed_cleanly():
    fm = _roundtrip_fix(
        "id: wiki-0042\ntitle: t\nproject: omb\ntags:\n- _\n- pr_\n- real\nsources: []"
    )
    _assert_parses(fm, "placeholder-tags")
    loaded = yaml.safe_load(fm)
    tags = loaded.get("tags") or []
    assert "_" not in tags and "pr_" not in tags, tags
    assert "real" in tags, tags


def test_no_case_duplicate_repo_tag_and_keeps_real_tags():
    # wiki-0001 shape: TitleCase project + lowercase repo tag + a placeholder to trigger the fix.
    fm = _roundtrip_fix(
        "id: wiki-0001\ntitle: t\nproject: Development\n"
        "tags:\n- repo/development\n- git\n- ssh\n- github\n- bitbucket\n- _\nsources: []",
        target="Development",
    )
    _assert_parses(fm, "case-dup-repo")
    loaded = yaml.safe_load(fm)
    tags = loaded.get("tags") or []
    repo_tags = [t for t in tags if t.lower() == "repo/development"]
    assert len(repo_tags) == 1, f"case-duplicate repo tag added: {tags}"
    assert "_" not in tags, f"placeholder not removed: {tags}"
    # all real tags survive — none silently dropped under the 6-cap to make room for a derived tag
    for real in ("git", "ssh", "github", "bitbucket"):
        assert real in tags, f"real tag {real!r} dropped: {tags}"


def _make_note(frontmatter: str, body: str = "body.\n"):
    d = tempfile.mkdtemp()
    wiki = Path(d) / "wiki"
    wiki.mkdir()
    p = wiki / "wiki-0042.md"
    p.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return ds._collect_notes(wiki)[0]


def test_missing_claims_flags_session_notes():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\nclaims: []\nsources: []"
    )
    issues = ds._claim_issues([note])
    assert len(issues) == 1, issues
    assert issues[0]["kind"] == "missing-claims", issues[0]


def test_non_session_note_without_claims_is_ok():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: note\norigin: personal\n"
        "claims: []\nsources: []"
    )
    issues = ds._claim_issues([note])
    assert len(issues) == 0, issues


def test_weak_claims_detected():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: omb, predicate: status, value: ok}\n"
        "- {subject: omb, predicate: plan, value: 검토 예정}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    weak_kinds = [i["kind"] for i in issues]
    assert "weak-claims" in weak_kinds, issues


def test_short_specific_claim_values_are_allowed():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: ds-logic-separation, predicate: actual_count, value: '1', kind: fact}\n"
        "- {subject: legacyCategoryRedirectMap.ts, predicate: file-type, value: .ts, kind: fact}\n"
        "- {subject: FEDEV-139, predicate: issue-type, value: 작업, kind: fact}\n"
        "- {subject: maintainability, predicate: team-ownership, value: jvm, kind: fact}\n"
        "- {subject: adversarial-hunt.md, predicate: 존재 여부, value: 없음, kind: fact}\n"
        "sources: []"
    )
    assert ds._claim_issues([note]) == []


def test_literal_status_mark_is_not_plan_like():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: FEDEV-40 하위 티켓, predicate: has_status_mark, value: (재활용예정)}\n"
        "- {subject: FEDEV-52~55, predicate: is_out_of_scope, value: Wave 0 범위 밖}\n"
        "sources: []"
    )
    assert ds._claim_issues([note]) == []


def test_plan_like_fact_claim_is_still_flagged():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: slack badge, predicate: technical-check, value: 검토 필요, kind: fact}\n"
        "- {subject: slack badge, predicate: current-state, value: markdown block fallback, kind: fact}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    assert len(issues) == 1, issues
    assert issues[0]["kind"] == "weak-claims", issues


def test_slug_with_plan_substring_is_not_weak_claim():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: vigil-command, predicate: repo-role, value: control-plane, kind: fact}\n"
        "- {subject: vigil-command, predicate: state, value: pipeline green, kind: fact}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    assert not issues, issues


def test_next_claim_allows_review_word():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: 'PR #198', predicate: next-step, value: review/approval, kind: next}\n"
        "- {subject: 'PR #198', predicate: ci, value: pipeline green, kind: fact}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    assert not issues, issues


def test_configured_project_alias_is_variant_not_typo():
    cfg = {
        "repos": [
            {"match": "oh-my-boring", "name": "ohmyboring", "origin": "personal"},
        ]
    }
    old_load = ds.boring_config.load
    try:
        ds.boring_config.load = lambda: cfg
        notes = [
            {"path": Path("wiki-0001.md"), "fm": {"project": "oh-my-boring", "tags": []}},
            {"path": Path("wiki-0002.md"), "fm": {"project": "ohmyboring", "tags": []}},
        ]
        report = ds._build_report(Path("/tmp/wiki"), notes)
        first = report["note_issues"]["wiki-0001.md"]
        assert {"kind": "project-variant", "old": "oh-my-boring", "suggested": "ohmyboring"} in first
        assert not any(i["kind"] == "project-typo" for i in first), first
    finally:
        ds.boring_config.load = old_load


def test_likely_typo_direction_uses_project_frequency():
    old_load = ds.boring_config.load
    try:
        ds.boring_config.load = lambda: {"repos": []}
        typos = ds._likely_typos(["kb-rag-bot", "kb-rag-bot", "kb-rag-bot", "kb-rag_bot"])
        assert len(typos) == 1, typos
        bad, good, _similarity, bad_count, good_count = typos[0]
        assert (bad, good, bad_count, good_count) == ("kb-rag_bot", "kb-rag-bot", 1, 3), typos
    finally:
        ds.boring_config.load = old_load


def test_likely_typo_tie_is_not_reported_without_evidence():
    old_load = ds.boring_config.load
    try:
        ds.boring_config.load = lambda: {"repos": []}
        assert ds._likely_typos(["alpha-service", "alpha_service"]) == []
    finally:
        ds.boring_config.load = old_load


def test_configured_distinct_projects_are_not_typos():
    cfg = {
        "repos": [
            {"match": "foodspring-admin-front", "name": "foodspring-admin-front", "origin": "company"},
            {"match": "foodspring-harmony-front", "name": "foodspring-harmony-front", "origin": "company"},
        ]
    }
    old_load = ds.boring_config.load
    try:
        ds.boring_config.load = lambda: cfg
        projects = ["foodspring-admin-front"] * 7 + ["foodspring-harmony-front"]
        assert ds._likely_typos(projects) == []
    finally:
        ds.boring_config.load = old_load


def test_rollout_session_without_sources_is_not_lineage_issue():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: codex-rollout-2026-06-21T06-59-02-demo\n"
        "claims:\n"
        "- {subject: omb, predicate: status, value: remembered, kind: fact, confidence: certain}\n"
        "- {subject: omb, predicate: mode, value: rollout, kind: fact, confidence: certain}\n"
        "sources: []"
    )
    assert ds._session_lineage_issues(note) == []


def test_normal_session_without_sources_is_not_lineage_issue():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: codex-real-session\nclaims: []\nsources: []"
    )
    assert ds._session_lineage_issues(note) == []


def test_unknown_claim_kind_is_flagged():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: omb, predicate: status, value: ok, kind: myth}\n"
        "- {subject: omb, predicate: version, value: 0.2.0}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    weak = [i for i in issues if i["kind"] == "weak-claims"]
    assert len(weak) == 1, issues
    reasons = [w["reason"] for w in weak[0]["claims"]]
    assert any("unknown claim kind" in r for r in reasons), reasons


def test_term_claim_kind_is_allowed():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: claim, predicate: is, value: a temporal fact, kind: term}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    weak = [i for i in issues if i["kind"] == "weak-claims"]
    reasons = [w["reason"] for w in weak[0]["claims"]] if weak else []
    assert not any("unknown claim kind" in r for r in reasons), reasons


def test_next_claim_kind_is_allowed():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: omb, predicate: follow-up, value: add next_actions endpoint, kind: next}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    weak = [i for i in issues if i["kind"] == "weak-claims"]
    reasons = [w["reason"] for w in weak[0]["claims"]] if weak else []
    assert not any("unknown claim kind" in r for r in reasons), reasons


def test_unknown_claim_confidence_is_flagged():
    note = _make_note(
        "id: wiki-0042\ntitle: t\nkind: session\norigin: personal\n"
        "omb_session_id: s-123\n"
        "claims:\n"
        "- {subject: omb, predicate: status, value: ok, confidence: definitely}\n"
        "- {subject: omb, predicate: version, value: 0.2.0}\n"
        "sources: []"
    )
    issues = ds._claim_issues([note])
    weak = [i for i in issues if i["kind"] == "weak-claims"]
    assert len(weak) == 1, issues
    reasons = [w["reason"] for w in weak[0]["claims"]]
    assert any("unknown claim confidence" in r for r in reasons), reasons


def test_allowed_claim_kinds_match_frontmatter_documentation():
    """The machine-parsing SSOT (.rules/schema.yaml) has a human-facing sibling
    (.rules/frontmatter.md). data-steward's ALLOWED_CLAIM_KINDS must stay in sync
    with the documented claim kinds so linting does not drift from docs.
    """
    repo_root = Path(_HERE).parent
    fm_path = repo_root / "vault" / ".rules" / "frontmatter.md"
    text = fm_path.read_text(encoding="utf-8")
    # Find the claim kind bullet, e.g.:
    # - `kind`: one of `fact` (default), `decision`, `assumption`, `risk`, `blocked`, `goal`, `term`, `next`
    match = re.search(r"^\s*- `kind`: one of (.+)$", text, re.MULTILINE)
    assert match, "claim kind bullet not found in frontmatter.md"
    documented = set(re.findall(r"`([a-z]+)`", match.group(1)))
    assert documented == ds.ALLOWED_CLAIM_KINDS, (
        f"ALLOWED_CLAIM_KINDS mismatch:\n"
        f"  frontmatter.md: {sorted(documented)}\n"
        f"  data-steward.py: {sorted(ds.ALLOWED_CLAIM_KINDS)}"
    )


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"\n✅ data-steward: {len(fns)} tests passed.")


if __name__ == "__main__":
    main()
