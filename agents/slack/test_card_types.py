#!/usr/bin/env python3
"""card_types — the value layer every other card_* module builds on.

Run: python3 agents/slack/test_card_types.py
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


class DependencyDirectionTests(unittest.TestCase):
    """AC2: card_types imports no card_* module — it is the dependency-free base layer every
    other split module (card_registers, card_advice, card_verdicts, card_view, card_live,
    card) builds on. A backward import here is exactly the "잘못 자른 것" the contract warns
    about, so this reads the source rather than trusting import success (a card_* import
    inside a function body would still be a cycle risk, even if the module happens to import
    cleanly at collection time)."""

    def test_card_types_imports_no_other_card_module(self):
        here = os.path.dirname(os.path.realpath(__file__))
        with open(os.path.join(here, "card_types.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="card_types.py")
        named = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                named.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                named.add(node.module)
        card_modules = {n for n in named if n.startswith("card") and n != "card_types"}
        self.assertEqual(card_modules, set())


if __name__ == "__main__":
    unittest.main()
