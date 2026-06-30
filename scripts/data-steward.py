#!/usr/bin/env python3
"""Data steward — inspect and optionally repair the ohmyboring vault.

Focuses on data-management hygiene that automated sync cannot fix by itself:
  - project/repo slug variants (`org/repo` vs `repo`)
  - placeholder tags (`_`, `pr_`, `slack_`)
  - weak session claims
  - generic or likely-typo project names

Run dry-run (safe):
    python3 scripts/data-steward.py

Apply fixes (rewrites vault/wiki/*.md — backs up each touched note to <note>.md.bak;
vault/wiki is gitignored so `git diff` shows nothing — review the .bak files):
    python3 scripts/data-steward.py --fix
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

# shared policy library lives next to the hooks
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared")
)
import boring_config  # noqa: E402

PLACEHOLDER_TAGS = {"_", "pr_", "slack_", ""}
GENERIC_PROJECTS = {"Development", "wiki", ""}
TYPO_THRESHOLD = 0.85
# Session-distilled notes should carry enough claims to be recallable as decisions.
MIN_CLAIMS_PER_SESSION = 2
# A claim value shorter than this is too vague to be authoritative.
MIN_CLAIM_VALUE_LEN = 4
# Markers that make a fact/decision claim sound like a next-step rather than completed work.
WEAK_CLAIM_KO_PATTERNS = {"검토 필요", "검토 예정", "확인 필요", "확인 예정", "확인해야", "고민", "예정", "계획"}
WEAK_CLAIM_EN_PATTERN = re.compile(r"\b(review|consider|plan|todo)\b", re.IGNORECASE)
PLAN_LIKE_CLAIM_KINDS = {"fact", "decision", "assumption", "term"}
SHORT_VALUE_PREDICATE_HINTS = {
    "actual_count",
    "count",
    "groups",
    "bugs",
    "merge",
    "issue-type",
    "file-type",
    "team",
    "ownership",
    "존재 여부",
}
ALLOWED_CLAIM_KINDS = {"fact", "decision", "assumption", "risk", "blocked", "goal", "term", "next"}
ALLOWED_CLAIM_CONFIDENCES = {"certain", "likely", "assumption", "outdated"}
FIXABLE_ISSUE_KINDS = {"project-variant", "placeholder-tags"}
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


def _configured_project_names() -> set[str]:
    """Project names explicitly declared in boring.json are trusted as real axes."""
    names = set()
    for rule in boring_config.load().get("repos") or []:
        name = str(rule.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _likely_typos(projects):
    """Find project names that look like typos of another canonical project."""
    counts = Counter(p for p in projects if p)
    names = sorted(counts)
    configured = _configured_project_names()
    typos = []
    for a in names:
        for b in names:
            if a == b:
                continue
            if boring_config.canonical_repo(a) == boring_config.canonical_repo(b):
                continue
            if a in configured and b in configured:
                continue
            ratio = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if ratio >= TYPO_THRESHOLD:
                if counts[a] == counts[b]:
                    continue
                bad, good = (a, b) if counts[a] < counts[b] else (b, a)
                typos.append((bad, good, ratio, counts[bad], counts[good]))
    # dedupe symmetric pairs
    seen = set()
    out = []
    for a, b, r, bad_count, good_count in typos:
        key = tuple(sorted((a, b)))
        if key not in seen:
            seen.add(key)
            out.append((a, b, r, bad_count, good_count))
    return out


def _short_claim_value_is_specific(claim: dict) -> bool:
    val = str(claim.get("value", "")).strip()
    pred = str(claim.get("predicate", "")).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", val):
        return True
    if re.fullmatch(r"\.[A-Za-z0-9]+", val):
        return True
    if any(hint in pred for hint in SHORT_VALUE_PREDICATE_HINTS) and len(val) >= 2:
        return True
    return False


def _claim_value_sounds_like_plan(claim: dict) -> bool:
    value = str(claim.get("value", "")).strip()
    predicate = str(claim.get("predicate", "")).strip().lower()
    if "status_mark" in predicate and value.startswith("(") and value.endswith(")"):
        return False
    lowered = value.lower()
    return any(w in value for w in WEAK_CLAIM_KO_PATTERNS) or bool(
        WEAK_CLAIM_EN_PATTERN.search(lowered)
    )


def _claim_issues(notes):
    """Find session-distilled notes with missing or weak claims.

    Claims are agent-curated facts (subject, predicate, value). They are the
    authoritative signal for later recall. We only flag session-distilled notes
    (omb_session_id present) because freeform notes may legitimately have no claims.
    """
    issues = []
    for n in notes:
        fm = n["fm"]
        if not fm.get("omb_session_id"):
            continue
        claims = fm.get("claims") or []
        if len(claims) < MIN_CLAIMS_PER_SESSION:
            issues.append(
                {
                    "path": n["path"].name,
                    "kind": "missing-claims",
                    "count": len(claims),
                    "min": MIN_CLAIMS_PER_SESSION,
                }
            )
            continue
        weak = []
        for c in claims:
            if not isinstance(c, dict):
                continue
            val = str(c.get("value", "")).strip()
            kind = str(c.get("kind", "") or "fact").strip().lower()
            conf = str(c.get("confidence", "") or "certain").strip().lower()
            if len(val) < MIN_CLAIM_VALUE_LEN and not _short_claim_value_is_specific(c):
                weak.append({"claim": c, "reason": "value too short"})
            elif kind in PLAN_LIKE_CLAIM_KINDS and _claim_value_sounds_like_plan(c):
                weak.append({"claim": c, "reason": "value sounds like a plan, not a fact"})
            if kind not in ALLOWED_CLAIM_KINDS:
                weak.append({"claim": c, "reason": f"unknown claim kind '{kind}'"})
            if conf not in ALLOWED_CLAIM_CONFIDENCES:
                weak.append({"claim": c, "reason": f"unknown claim confidence '{conf}'"})
        if weak:
            issues.append({"path": n["path"].name, "kind": "weak-claims", "claims": weak})
    return issues


def _session_lineage_issues(note):
    """Find invalid session provenance without requiring duplicated source artifacts."""
    return []


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


def _build_report(wiki_dir: Path, notes: list[dict]) -> dict:
    projects = [n["fm"].get("project") or "" for n in notes]
    variants = _project_variants(notes)
    typos = _likely_typos(projects)
    claim_issues = _claim_issues(notes)

    note_issues = defaultdict(list)
    for n in notes:
        proj = n["fm"].get("project") or ""
        tags = n["fm"].get("tags") or []

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
        lineage_issues = _session_lineage_issues(n)
        if lineage_issues:
            note_issues[n["path"].name].extend(lineage_issues)

    return {
        "wiki_dir": str(wiki_dir),
        "note_count": len(notes),
        "project_variants": variants,
        "likely_typos": [
            {
                "bad": a,
                "good": b,
                "similarity": round(r, 3),
                "bad_count": bad_count,
                "good_count": good_count,
            }
            for a, b, r, bad_count, good_count in typos
        ],
        "note_issues": dict(note_issues),
        "claim_issues": claim_issues,
    }


def analyze_vault(wiki_dir: Path) -> tuple[list[dict], dict]:
    notes = _collect_notes(wiki_dir)
    return notes, _build_report(wiki_dir, notes)


def fixable_note_names(report: dict) -> list[str]:
    names = []
    for name, issues in report.get("note_issues", {}).items():
        if any(issue.get("kind") in FIXABLE_ISSUE_KINDS for issue in issues):
            names.append(name)
    return sorted(names)


def main():
    parser = argparse.ArgumentParser(description="Inspect/repair ohmyboring vault data hygiene")
    parser.add_argument("--vault", help="vault root directory (default: BORING_VAULT_DIR or ~/oh-my-boring/vault)")
    parser.add_argument("--fix", action="store_true", help="rewrite notes in place (review with git diff)")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    parser.add_argument("--json", action="store_true", help="output structured JSON report")
    args = parser.parse_args()

    wiki_dir = _wiki_dir(args)
    if not wiki_dir.exists():
        print(f"[error] wiki directory not found: {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    notes, report = analyze_vault(wiki_dir)
    variants = report["project_variants"]
    typos = [
        (t["bad"], t["good"], t["similarity"], t["bad_count"], t["good_count"])
        for t in report["likely_typos"]
    ]
    claim_issues = report["claim_issues"]
    note_issues = report["note_issues"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
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
        for bad, good, r, bad_count, good_count in typos:
            print(
                f"   {bad!r} ({bad_count}) → {good!r} ({good_count}) "
                f"(similarity {r:.2f})"
            )
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
                elif issue["kind"] == "zombie-rollout":
                    print(f"    - zombie rollout session {issue['omb_session_id']}")
    else:
        print("✅ No structural hygiene issues found.")

    if claim_issues:
        print(f"\n🧭 Session notes with weak claims: {len(claim_issues)}")
        for issue in sorted(claim_issues, key=lambda x: x["path"]):
            if issue["kind"] == "missing-claims":
                print(
                    f"  {issue['path']}: only {issue['count']} claim(s) "
                    f"(aim for ≥{issue['min']})"
                )
            elif issue["kind"] == "weak-claims":
                print(f"  {issue['path']}:")
                for w in issue["claims"]:
                    c = w["claim"]
                    print(f"    - weak claim ({w['reason']}): {c}")
    elif not note_issues:
        print("✅ No data hygiene issues found.")
        return

    if not args.fix:
        fixable = fixable_note_names(report)
        if fixable:
            print("\n💡 Run with --fix to apply project canonicalization (org-prefix stripping) and")
            print("   remove placeholder tags. Typos and generic project names are left for manual review.")
        else:
            print("\n💡 No automatic fixes remain. Typos, generic project names, and weak claims")
            print("   require explicit mapping or note-level review.")
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
