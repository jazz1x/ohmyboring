#!/usr/bin/env python3
"""The gate: a branch that changes behaviour also says so in CHANGELOG.md.

Run: python3 scripts/test_changelog.py   (no pytest dependency)

Why this exists, measured 2026-09-21. `CHANGELOG.md` was last touched on 2026-08-20 by a commit
titled *"record the 33 commits Unreleased was missing"* — a catch-up, not a habit. Since that
commit, 157 pull requests merged and none of them touched the file. Across the repo's whole
history, 24 of 337 commits changed it, several of those being the same kind of catch-up.

Writing it down afterwards does not work: by then nobody remembers which of 157 changes a reader
would have wanted to know about, so the entries that do get written are the ones still visible in
someone's memory — the recent and the large. The gate has to fire while the change is in hand.

What it checks, and only this: a branch whose commits include a `feat(...)` or `fix(...)` subject
touching shipped source must also touch `CHANGELOG.md`. Not the wording, not the section, not
whether the entry is any good — a person judges that. A gate that claimed to judge it would be
selling a guarantee it does not have.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = "CHANGELOG.md"

#: Where a change the reader cares about lives. Tests, gates and docs move without a CHANGELOG
#: line; these directories are the product itself.
SHIPPED_PREFIXES = ("drudge/src/", "agents/", "hooks/", "scripts/", "install.sh", "Makefile")

#: A commit subject that announces behaviour. `chore`, `docs`, `test`, `refactor` and `ci` do not.
_ANNOUNCES = re.compile(r"^(feat|fix)(\([^)]*\))?!?:")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _base() -> str:
    """The commit this branch grew from — `origin/main`, or `main` when there is no remote.

    CI checks out a shallow clone with no `origin/main` ref at all, and the first run of this
    gate failed there with "neither origin/main nor main is reachable". So when neither ref
    exists the gate fetches `main` from origin before giving up — a network call, but one CI
    already made to get here. It still fails loudly when the base cannot be found: a gate that
    silently skips in CI is a gate that only runs on laptops.
    """
    for ref in ("origin/main", "main"):
        try:
            return _git("merge-base", "HEAD", ref)
        except subprocess.CalledProcessError:
            continue
    # `--unshallow`, not a plain fetch: a depth-1 clone shares no history with main, so fetching
    # the ref alone still leaves merge-base with nothing in common. Rehearsed on a depth-1 clone
    # of this branch — plain fetch failed, unshallow found the base.
    subprocess.run(
        ["git", "fetch", "--no-tags", "--unshallow", "origin", "main:refs/remotes/origin/main"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    try:
        return _git("merge-base", "HEAD", "origin/main")
    except subprocess.CalledProcessError as e:
        raise AssertionError(
            "neither origin/main nor main is reachable, and fetching origin/main did not help — "
            "check out with full history (fetch-depth: 0) so the branch can be scoped"
        ) from e


def _branch_commits(base: str) -> list[tuple[str, str]]:
    out = _git("log", "--format=%H%x00%s", f"{base}..HEAD")
    rows = []
    for line in out.splitlines():
        if "\0" in line:
            sha, subject = line.split("\0", 1)
            rows.append((sha, subject))
    return rows


def _files(sha: str) -> list[str]:
    return _git("show", "--name-only", "--format=", sha).splitlines()


def test_a_branch_that_changes_behaviour_says_so_in_the_changelog():
    base = _base()
    commits = _branch_commits(base)
    if not commits:
        return  # nothing on this branch yet; the gate has nothing to judge

    touched_changelog = any(CHANGELOG in _files(sha) for sha, _ in commits)
    announcing = [
        (sha[:8], subject)
        for sha, subject in commits
        if _ANNOUNCES.match(subject)
        and any(f.startswith(SHIPPED_PREFIXES) for f in _files(sha))
        and not all("test" in f for f in _files(sha) if f)
    ]
    assert not announcing or touched_changelog, (
        "these commits change what the product does and the branch does not touch "
        f"{CHANGELOG}: {announcing}. Add the line now — the last time this was left for later, "
        "157 merges went unrecorded."
    )


def test_the_unreleased_section_exists_and_is_not_empty():
    """A catch-up commit once found `Unreleased` missing 33 commits. It can only be missing
    entries if it is there at all."""
    text = (ROOT / CHANGELOG).read_text(encoding="utf-8")
    assert "## [Unreleased]" in text, "CHANGELOG.md has no Unreleased section to add to"
    body = text.split("## [Unreleased]", 1)[1].split("\n## ", 1)[0]
    assert [ln for ln in body.splitlines() if ln.startswith("- ")], (
        "the Unreleased section holds no entries — a released-looking changelog with unreleased work"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                print(f"FAIL {name}: {e}", file=sys.stderr)
                failures += 1
    if failures:
        sys.exit(1)
    print("ok - changelog: a branch that changes behaviour records it")
