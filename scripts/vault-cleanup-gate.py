#!/usr/bin/env python3
"""Backup-first gate for safe vault/wiki cleanup.

This wraps data-steward's safe repairs with a verification contract:
- create a tar.gz backup before any rewrite
- keep the wiki note set stable
- keep every note frontmatter parseable
- clear only automatically fixable steward issues
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DATA_STEWARD = ROOT / "scripts" / "data-steward.py"


def _load_data_steward():
    spec = importlib.util.spec_from_file_location("data_steward", str(DATA_STEWARD))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _default_vault() -> Path:
    return Path(
        os.environ.get("BORING_VAULT_DIR")
        or os.path.join(os.environ.get("BORING_HOME") or str(ROOT), "vault")
    ).expanduser()


def _wiki_dir(vault: Path) -> Path:
    return vault.expanduser() / "wiki"


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_report_path(mode: str) -> Path:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    return ROOT / "docs" / "reports" / f"{today}-vault-cleanup-gate-{mode}.md"


def _note_paths(wiki_dir: Path) -> set[str]:
    return {p.name for p in wiki_dir.glob("wiki-*.md")}


def _frontmatter_errors(wiki_dir: Path) -> list[str]:
    errors = []
    for path in sorted(wiki_dir.glob("wiki-*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            errors.append(f"{path.name}: missing frontmatter fence")
            continue
        end = text.find("\n---\n")
        if end < 0:
            errors.append(f"{path.name}: missing closing frontmatter fence")
            continue
        try:
            yaml.safe_load(text[4:end])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{path.name}: frontmatter parse error: {e}")
    return errors


def _issue_counts(report: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issues in report.get("note_issues", {}).values():
        for issue in issues:
            kind = issue.get("kind", "unknown")
            counts[kind] = counts.get(kind, 0) + 1
    counts["claim_issues"] = len(report.get("claim_issues", []))
    return counts


def _manual_issue_count(report: dict, data_steward) -> int:
    count = 0
    for issues in report.get("note_issues", {}).values():
        for issue in issues:
            if issue.get("kind") not in data_steward.FIXABLE_ISSUE_KINDS:
                count += 1
    return count + len(report.get("claim_issues", []))


def _create_backup(wiki_dir: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"vault-wiki-{_stamp()}.tar.gz"
    with tarfile.open(backup, "w:gz") as tar:
        for path in sorted(wiki_dir.iterdir()):
            if path.is_file():
                tar.add(path, arcname=f"wiki/{path.name}")
        manifest = {
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "wiki_dir": str(wiki_dir),
            "file_count": len([p for p in wiki_dir.iterdir() if p.is_file()]),
        }
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    if backup.stat().st_size <= 0:
        raise RuntimeError(f"backup archive is empty: {backup}")
    return backup


def _apply_safe_fixes(notes: list[dict], data_steward) -> list[str]:
    fixed = []
    for note in notes:
        fm = note["fm"]
        project = fm.get("project") or ""
        tags = list(fm.get("tags") or [])
        target = data_steward._issue_target_project(project)
        needs_fix = target != project or any(t in data_steward.PLACEHOLDER_TAGS for t in tags)
        if needs_fix:
            data_steward._fix_note(note, target)
            fixed.append(note["path"].name)
    return sorted(fixed)


def _write_report(
    path: Path,
    *,
    mode: str,
    status: str,
    backup: Path | None,
    before: dict,
    after: dict,
    fixed: list[str],
    issues: list[str],
    data_steward,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    before_fixable = data_steward.fixable_note_names(before)
    after_fixable = data_steward.fixable_note_names(after)
    lines = [
        f"# Vault Cleanup Gate - {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- mode: `{mode}`",
        f"- status: `{status}`",
        f"- wiki: `{after['wiki_dir']}`",
        f"- backup: `{backup or 'not-created'}`",
        f"- notes: `{before['note_count']} -> {after['note_count']}`",
        f"- fixed notes: `{len(fixed)}`",
        f"- fixable before/after: `{len(before_fixable)} -> {len(after_fixable)}`",
        f"- manual remaining: `{_manual_issue_count(after, data_steward)}`",
        "",
        "## Contract",
        "",
        "- backup archive exists before rewrite",
        "- wiki note set is stable",
        "- all frontmatter parses after cleanup",
        "- automatically fixable steward issues are zero after cleanup",
        "",
        "## Issue Counts",
        "",
        "| kind | before | after |",
        "| --- | ---: | ---: |",
    ]
    before_counts = _issue_counts(before)
    after_counts = _issue_counts(after)
    for kind in sorted(set(before_counts) | set(after_counts)):
        lines.append(f"| `{kind}` | {before_counts.get(kind, 0)} | {after_counts.get(kind, 0)} |")
    lines.extend(["", "## Fixed Notes", ""])
    if fixed:
        lines.extend(f"- `{name}`" for name in fixed)
    else:
        lines.append("- none")
    lines.extend(["", "## Gate Issues", ""])
    if issues:
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("- none")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    data_steward = _load_data_steward()
    vault = Path(args.vault).expanduser() if args.vault else _default_vault()
    wiki_dir = _wiki_dir(vault)
    if not wiki_dir.is_dir():
        print(f"[vault-cleanup] wiki directory not found: {wiki_dir}", file=sys.stderr)
        return 1

    before_notes, before = data_steward.analyze_vault(wiki_dir)
    before_paths = _note_paths(wiki_dir)
    before_errors = _frontmatter_errors(wiki_dir)
    backup = None
    fixed: list[str] = []

    if args.fix:
        backup = _create_backup(wiki_dir, Path(args.backup_dir).expanduser())
        fixed = _apply_safe_fixes(before_notes, data_steward)

    after_notes, after = data_steward.analyze_vault(wiki_dir)
    after_paths = _note_paths(wiki_dir)
    after_errors = _frontmatter_errors(wiki_dir)

    issues = []
    if before_errors:
        issues.extend(f"pre-existing parse error: {e}" for e in before_errors)
    if after_errors:
        issues.extend(f"post-cleanup parse error: {e}" for e in after_errors)
    if before["note_count"] != after["note_count"]:
        issues.append(f"note_count changed: {before['note_count']} -> {after['note_count']}")
    if before_paths != after_paths:
        missing = sorted(before_paths - after_paths)
        added = sorted(after_paths - before_paths)
        issues.append(f"wiki note set changed: missing={missing} added={added}")
    if args.fix and (backup is None or not backup.exists() or backup.stat().st_size <= 0):
        issues.append("backup archive missing or empty")
    remaining_fixable = data_steward.fixable_note_names(after)
    if remaining_fixable:
        issues.append(f"fixable steward issues remain: {remaining_fixable}")

    status = "ok" if not issues else "failed"
    mode = "fix" if args.fix else "check"
    report = Path(args.report).expanduser() if args.report else _default_report_path(mode)
    _write_report(
        report,
        mode=mode,
        status=status,
        backup=backup,
        before=before,
        after=after,
        fixed=fixed,
        issues=issues,
        data_steward=data_steward,
    )

    print(
        "vault_cleanup_gate "
        f"status={status} mode={mode} "
        f"notes={after['note_count']} fixed={len(fixed)} "
        f"manual_remaining={_manual_issue_count(after, data_steward)} "
        f"report={report}"
    )
    if backup:
        print(f"vault_cleanup_backup path={backup}")
    return 0 if status == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup-first verification gate for vault cleanup")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify cleanup contract without rewriting")
    mode.add_argument("--fix", action="store_true", help="backup, apply safe fixes, then verify")
    parser.add_argument("--vault", help="vault root directory")
    parser.add_argument(
        "--backup-dir",
        default=str(ROOT / "data" / "backups" / "vault-cleanup"),
        help="directory for pre-cleanup tar.gz backups",
    )
    parser.add_argument("--report", help="markdown report path")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
