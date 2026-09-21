#!/usr/bin/env python3
"""Tests for anchor-shadow.py — the three ways this number comes out wrong.

Run: python3 scripts/test_anchor_shadow.py
"""

import importlib.util
import json
import os
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["BORING_EVENT_SINK"] = "spool"
_spec = importlib.util.spec_from_file_location("anchor_shadow", HERE / "anchor-shadow.py")
shadow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shadow)


class WhatCounts(unittest.TestCase):
    def test_docs_are_not_code(self):
        """`LOG.md` and `PRD.md` are the most re-edited files in this corpus by a wide margin.

        They are ledgers being appended to, not problems being re-solved — counting them would
        inflate the revisit rate with the one kind of file the north star is not about.
        """
        self.assertTrue(shadow.CODE.search("a/b/nodes.py"))
        self.assertTrue(shadow.CODE.search("Main.kt"))
        self.assertFalse(shadow.CODE.search("docs/LOG.md"))
        self.assertFalse(shadow.CODE.search("PRD.md"))

    def test_a_file_edited_twice_in_one_session_is_not_a_revisit(self):
        """A revisit needs an EARLIER session. Repeats inside one session are the same problem
        still being worked, and counting them turns ordinary iteration into evidence."""
        with self._tmp() as root:
            self._session(root, "s1", ["/r/a.py", "/r/a.py", "/r/a.py"])
            out = self._run(root)
            self.assertEqual(out["revisits"], 0, out)

            self._session(root, "s2", ["/r/a.py"])
            out = self._run(root)
            self.assertEqual(out["revisits"], 1, out)

    def _run(self, root):
        paths = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
        seen, total, revisits = {}, 0, 0
        current, touched = None, set()
        for project, target, session in shadow.edits(paths):
            if current != session:
                for k in touched:
                    seen.setdefault(k, set()).add(current)
                current, touched = session, set()
            total += 1
            key = (project, target)
            touched.add(key)
            if seen.get(key):
                revisits += 1
        return {"total": total, "revisits": revisits}

    def _tmp(self):
        import tempfile

        class Ctx:
            def __enter__(s):
                s.d = tempfile.TemporaryDirectory()
                return Path(s.d.name)

            def __exit__(s, *a):
                s.d.cleanup()

        return Ctx()

    def _session(self, root, name, files):
        import time as _t

        p = root / "proj" / f"{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": f}}]}}
            for f in files
        ]
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        os.utime(p, (_t.time(), _t.time()))


class VaultSearch(unittest.TestCase):
    def test_a_vault_it_cannot_read_raises_instead_of_answering_zero(self):
        """The failure that this whole script measures, sitting in the script itself.

        The first version shelled out to `rg` and swallowed `OSError` into a count of 0 — so on a
        machine without ripgrep (the CI runner, as it turns out) every file reported "no note
        mentions this" and the coverage read as a clean 0%. An unreadable vault has to be
        distinguishable from an empty one.
        """
        import tempfile

        saved = shadow.VAULT
        try:
            shadow.VAULT = Path(tempfile.gettempdir()) / "definitely-not-a-vault-xyzzy"
            with self.assertRaises(FileNotFoundError):
                shadow.vault_mentions(["anything"])
        finally:
            shadow.VAULT = saved

    def test_it_reads_notes_the_repo_gitignores(self):
        """`.gitignore` carries `vault/wiki/*`; a tool that honours it returns nothing here.

        Hermetic on purpose. An earlier version asserted against the repo's own vault and passed
        locally while failing in CI, where the checkout carries only the committed notes — a test
        whose subject depends on which files happen to exist is testing the environment.
        """
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("notes/*\n", encoding="utf-8")
            notes = root / "notes"
            notes.mkdir()
            (notes / "n1.md").write_text("touches markers.py:63 here\n", encoding="utf-8")

            hidden = subprocess.run(["git", "check-ignore", "-q", str(notes / "n1.md")], cwd=str(root))
            self.assertEqual(hidden.returncode, 0, "fixture must actually be ignored")

            saved = shadow.VAULT
            try:
                shadow.VAULT = notes
                counts = shadow.vault_mentions(["markers.py", "absent-xyzzy"])
            finally:
                shadow.VAULT = saved

            self.assertGreaterEqual(counts["markers.py"], 1, "the ignored note must still be read")
            self.assertEqual(counts["absent-xyzzy"], 0, "absence must read as 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
