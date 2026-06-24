#!/usr/bin/env python3
"""Data steward — inspect and optionally repair the oh-my-boring vault.

Focuses on data-management hygiene that automated sync cannot fix by itself:
  - project/repo slug variants (`org/repo` vs `repo`)
  - placeholder tags (`_`, `pr_`, `slack_`)
  - missing lineage (`sources: []` on session-distilled notes)
  - generic or likely-typo project names

Run dry-run (safe):
    python3 scripts/data-steward.py

Apply fixes (rewrites vault/wiki/*.md — backs up each touched note to <note>.md.bak;
vault/wiki is gitignored so `git diff` shows nothing — review the .bak files):
    python3 scripts/data-steward.py --fix
"""
import argparse
import difflib
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# shared policy library lives next to the hooks
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared")
)
import boring_config  # noqa: E402

PLACEHOLDER_TAGS = {"_", "pr_", "slack_", ""}
GENERIC_PROJECTS = {"Development", "wiki", ""}
TYPO_THRESHOLD = 0.85
# The shipped sample note is allowed to be generic/empty; do not flag it as data rot.
SEED_NOTE = "wiki-0000.md"


def _wiki_dir(args) -> Path:
    vault = (
        args.vault
        or os.environ.get("BORING_VAULT_DIR")
        or os.path.join(os.environ.get("BORING_HOME") or os.path.expanduser("~/oh-my-boring"), "vault")
    )
    return Path(vault).expanduser() / "wiki"


def _collect_notes(wiki_dir: Path):
    notes = []
    for p in sorted(wiki_dir.glob("wiki-*.md")):
        if p.name == SEED_NOTE:
            continue
        text = p.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---\n")
        if end < 0:
            continue
        yaml_text = text[4:end]
        body = text[end + 5 :]
        try:
            import yaml

            fm = yaml.safe_load(yaml_text)
        except Exception as e:
            print(f"[warn] skipping {p.name}: frontmatter parse error: {e}", file=sys.stderr)
            continue
        notes.append({"path": p, "yaml_text": yaml_text, "body": body, "fm": fm or {}})
    return notes


def _project_variants(notes):
    """Group projects by their canonical slug and return groups with >1 variant."""
    canonical = defaultdict(set)
    for n in notes:
        proj = n["fm"].get("project") or ""
        canonical[boring_config.canonical_repo(proj)].add(proj)
    return {c: sorted(v) for c, v in canonical.items() if len(v) > 1}


def _likely_typos(projects):
    """Find project names that look like typos of another canonical project."""
    names = sorted({p for p in projects if p})
    typos = []
    for a in names:
        for b in names:
            if a == b:
                continue
            ratio = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if ratio >= TYPO_THRESHOLD:
                typos.append((a, b, ratio))
    # dedupe symmetric pairs
    seen = set()
    out = []
    for a, b, r in typos:
        key = tuple(sorted((a, b)))
        if key not in seen:
            seen.add(key)
            out.append((a, b, r))
    return out


def _issue_target_project(project: str) -> str:
    """Decide what the project should be after fixing.

    Only canonicalization (org-prefix stripping / rule name) is applied
    automatically. Likely typos and generic project names are reported for
    manual review to avoid false-positive rewrites.
    """
    return boring_config.canonical_repo(project)


def _rewrite_yaml_block(yaml_text: str, project: str | None, tags: list | None) -> str:
    """Minimal in-place rewrite of project line and tags list."""
    lines = yaml_text.splitlines()
    if project is not None:
        for i, line in enumerate(lines):
            if re.match(r"^project:\s*", line):
                lines[i] = f"project: {project}"
                break
    if tags is not None:
        # find the tags: line (block- or flow-style)
        start = None
        for i, line in enumerate(lines):
            if line.startswith("tags:"):
                start = i
                break
        if start is not None:
            # Consume any block-style entries that follow (none for inline `tags: []`/`tags: [a]`).
            end = start + 1
            while end < len(lines) and lines[end].strip().startswith("-"):
                end += 1
            # REPLACE the tags line itself, not just its children — preserving an inline
            # `tags: []` and appending block entries after it yields unparseable YAML.
            if tags:
                new_block = ["tags:"] + [f"- {t}" for t in tags]
            else:
                new_block = ["tags: []"]
            lines = lines[:start] + new_block + lines[end:]
    return "\n".join(lines)


def _fix_note(n, target_project: str):
    fm = n["fm"]
    old_project = fm.get("project") or ""
    old_tags = list(fm.get("tags") or [])
    target_repo = f"repo/{target_project}" if target_project else None

    new_tags = []
    seen_lower = set()
    for t in old_tags:
        if t in PLACEHOLDER_TAGS:
            continue
        # rewrite the old repo/<old_project> tag to the canonical target (in place, not as an addition)
        if target_repo and t.startswith("repo/"):
            suffix = t[len("repo/") :]
            if suffix.lower() in (old_project.lower(), old_project.split("/")[-1].lower()):
                t = target_repo
        # dedup case-insensitively so e.g. repo/Development and repo/development don't both survive
        if t.lower() in seen_lower:
            continue
        seen_lower.add(t.lower())
        new_tags.append(t)

    # ensure the canonical repo tag exists (case-insensitive check — avoid a duplicate case-variant)
    if target_repo and target_repo.lower() not in seen_lower:
        new_tags.insert(0, target_repo)

    # cap to 6 (remember boundary) AFTER dedup — the case-insensitive dedup above means a rewritten
    # repo tag no longer inflates the count, so a real tag is no longer silently dropped to make room.
    new_tags = new_tags[:6]

    # .bak recovery: vault/wiki is gitignored, so `git diff` shows nothing — back up before rewriting.
    bak = n["path"].with_name(n["path"].name + ".bak")
    shutil.copy2(n["path"], bak)
    new_yaml = _rewrite_yaml_block(n["yaml_text"], target_project, new_tags)
    n["path"].write_text("---\n" + new_yaml + "\n---\n" + n["body"], encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Inspect/repair oh-my-boring vault data hygiene")
    parser.add_argument("--vault", help="vault root directory (default: BORING_VAULT_DIR or ~/oh-my-boring/vault)")
    parser.add_argument("--fix", action="store_true", help="rewrite notes in place (review with git diff)")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    parser.add_argument("--json", action="store_true", help="output structured JSON report")
    args = parser.parse_args()

    wiki_dir = _wiki_dir(args)
    if not wiki_dir.exists():
        print(f"[error] wiki directory not found: {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    notes = _collect_notes(wiki_dir)
    projects = [n["fm"].get("project") or "" for n in notes]
    variants = _project_variants(notes)
    typos = _likely_typos(projects)

    # build per-note issues
    note_issues = defaultdict(list)
    for n in notes:
        proj = n["fm"].get("project") or ""
        tags = n["fm"].get("tags") or []
        sources = n["fm"].get("sources") or []
        sid = n["fm"].get("omb_session_id")

        canonical = boring_config.canonical_repo(proj)
        if proj and canonical != proj:
            note_issues[n["path"].name].append(
                {"kind": "project-variant", "old": proj, "suggested": canonical}
            )
        for bad, good, _ in typos:
            if proj == bad:
                note_issues[n["path"].name].append(
                    {"kind": "project-typo", "old": proj, "suggested": good}
                )
        if proj in GENERIC_PROJECTS:
            note_issues[n["path"].name].append(
                {"kind": "generic-project", "value": proj or "(empty)"}
            )
        bad_tags = [t for t in tags if t in PLACEHOLDER_TAGS]
        if bad_tags:
            note_issues[n["path"].name].append({"kind": "placeholder-tags", "tags": bad_tags})
        if sid and not sources:
            note_issues[n["path"].name].append({"kind": "missing-source", "omb_session_id": sid})

    if args.json:
        print(
            json.dumps(
                {
                    "wiki_dir": str(wiki_dir),
                    "note_count": len(notes),
                    "project_variants": variants,
                    "likely_typos": [
                        {"bad": a, "good": b, "similarity": round(r, 3)} for a, b, r in typos
                    ],
                    "note_issues": dict(note_issues),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(f"📂 vault/wiki: {wiki_dir}")
    print(f"📝 notes scanned: {len(notes)}\n")

    if variants:
        print("⚠️  Project variants detected (same repo under multiple names):")
        for canonical, vs in sorted(variants.items()):
            print(f"   canonical={canonical!r}: {vs}")
        print()

    if typos:
        print("🔤 Likely project typos:")
        for bad, good, r in typos:
            print(f"   {bad!r} → {good!r} (similarity {r:.2f})")
        print()

    if note_issues:
        print(f"🧹 Notes with issues: {len(note_issues)}")
        for name, issues in sorted(note_issues.items()):
            print(f"  {name}:")
            for issue in issues:
                if issue["kind"] == "project-variant":
                    print(f"    - project variant: {issue['old']!r} → {issue['suggested']!r}")
                elif issue["kind"] == "project-typo":
                    print(f"    - typo: {issue['old']!r} → {issue['suggested']!r}")
                elif issue["kind"] == "generic-project":
                    print(f"    - generic project: {issue['value']!r}")
                elif issue["kind"] == "placeholder-tags":
                    print(f"    - placeholder tags: {issue['tags']}")
                elif issue["kind"] == "missing-source":
                    print(f"    - missing sources for session {issue['omb_session_id']}")
    else:
        print("✅ No data hygiene issues found.")
        return

    if not args.fix:
        print("\n💡 Run with --fix to apply project canonicalization (org-prefix stripping) and")
        print("   remove placeholder tags. Typos and generic project names are left for manual review.")
        return

    if not args.yes:
        ans = input("\nApply fixes? [y/N] ")
        if ans.lower() not in ("y", "yes"):
            print("aborted.")
            return

    fixed = 0
    for n in notes:
        proj = n["fm"].get("project") or ""
        tags = list(n["fm"].get("tags") or [])
        target = _issue_target_project(proj)
        needs_fix = (
            target != proj
            or any(t in PLACEHOLDER_TAGS for t in tags)
        )
        if needs_fix:
            _fix_note(n, target)
            fixed += 1

    print(
        f"\n✅ Fixed {fixed} note(s). vault/wiki is gitignored, so `git diff` won't show changes — "
        "review the `*.md.bak` files written next to each fixed note, then delete them once satisfied."
    )


if __name__ == "__main__":
    main()
