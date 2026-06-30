#!/usr/bin/env python3
"""Network-free regression tests for vault-cleanup-gate.py."""
from __future__ import annotations

import argparse
import importlib.util
import tarfile
import tempfile
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("vault_cleanup_gate", str(HERE / "vault-cleanup-gate.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def _write_note(wiki: Path, name: str, frontmatter: str, body: str = "body.\n") -> Path:
    path = wiki / name
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _args(root: Path, fix: bool) -> argparse.Namespace:
    return argparse.Namespace(
        check=not fix,
        fix=fix,
        vault=str(root / "vault"),
        backup_dir=str(root / "backups"),
        report=str(root / "report.md"),
    )


def test_fix_creates_backup_and_clears_fixable_issues():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        _write_note(
            wiki,
            "wiki-0001.md",
            "id: wiki-0001\ntitle: t\nkind: session\norigin: personal\n"
            "project: marketboro/omb\n"
            "tags:\n- repo/marketboro/omb\n- _\n"
            "omb_session_id: s-1\n"
            "claims:\n"
            "- {subject: omb, predicate: status, value: remembered, kind: fact, confidence: certain}\n"
            "- {subject: omb, predicate: decision, value: cleanup gate, kind: decision, confidence: certain}\n",
        )

        rc = gate.run(_args(root, fix=True))

        assert rc == 0
        backups = sorted((root / "backups").glob("vault-wiki-*.tar.gz"))
        assert len(backups) == 1
        with tarfile.open(backups[0], "r:gz") as tar:
            assert "wiki/wiki-0001.md" in tar.getnames()
            assert "manifest.json" in tar.getnames()
        text = (wiki / "wiki-0001.md").read_text(encoding="utf-8")
        fm = yaml.safe_load(text[4 : text.find("\n---\n")])
        assert fm["project"] == "omb"
        assert "_" not in fm["tags"]
        assert "repo/omb" in fm["tags"]
        assert (wiki / "wiki-0001.md.bak").exists()
        report = (root / "report.md").read_text(encoding="utf-8")
        assert "status: `ok`" in report


def test_check_fails_when_fixable_issues_remain():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        wiki = root / "vault" / "wiki"
        wiki.mkdir(parents=True)
        _write_note(
            wiki,
            "wiki-0001.md",
            "id: wiki-0001\ntitle: t\nkind: note\norigin: personal\n"
            "project: marketboro/omb\ntags: [_]\n",
        )

        rc = gate.run(_args(root, fix=False))

        assert rc == 1
        assert not (root / "backups").exists()
        report = (root / "report.md").read_text(encoding="utf-8")
        assert "fixable steward issues remain" in report


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok - {fn.__name__}")
    print(f"\nOK: {len(fns)} vault-cleanup gate tests passed.")


if __name__ == "__main__":
    main()
