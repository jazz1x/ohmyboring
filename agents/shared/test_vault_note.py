#!/usr/bin/env python3
"""Tests for the one frontmatter splitter.

Run: python3 agents/shared/test_vault_note.py   (no pytest dependency)

Each case here is a note shape the four hand-rolled copies got wrong in the same direction:
they answered "this note has no frontmatter", and a note with no frontmatter loses its id and
its claims on the next pass.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from vault_note import frontmatter_text, split_frontmatter  # noqa: E402


def test_a_plain_note_splits_into_frontmatter_and_body():
    fm, body = split_frontmatter("---\nid: wiki-0001\ntitle: t\n---\nbody line\n")
    assert fm == "id: wiki-0001\ntitle: t"
    assert body == "body line\n"


def test_a_note_with_no_fence_has_no_frontmatter():
    assert split_frontmatter("just a body\n") is None
    assert split_frontmatter("") is None
    assert frontmatter_text("just a body\n") == ""


def test_an_unclosed_fence_is_not_frontmatter():
    """The opening fence alone is not a frontmatter block — treating the whole file as YAML
    would hand the body to a parser that then reports an error on the note's prose."""
    assert split_frontmatter("---\nid: wiki-0001\nbody with no closing fence\n") is None


def test_a_bom_does_not_hide_the_frontmatter():
    fm, body = split_frontmatter("﻿---\nid: wiki-0002\n---\nbody\n")
    assert fm == "id: wiki-0002"
    assert body == "body\n"


def test_crlf_endings_do_not_hide_the_frontmatter():
    fm, body = split_frontmatter("---\r\nid: wiki-0003\r\n---\r\nbody\r\n")
    assert fm == "id: wiki-0003"
    assert body == "body\n"


def test_a_trailing_space_on_the_fence_does_not_hide_the_frontmatter():
    fm, _ = split_frontmatter("--- \nid: wiki-0004\n--- \nbody\n")
    assert fm == "id: wiki-0004"


def test_a_horizontal_rule_in_the_body_does_not_end_the_frontmatter_early():
    """The closing fence is the first one AFTER the opening fence; a `---` inside the body is
    past it and must not be mistaken for the close of a second block."""
    fm, body = split_frontmatter("---\nid: wiki-0005\n---\nintro\n\n---\n\nmore body\n")
    assert fm == "id: wiki-0005"
    assert body.startswith("intro")
    assert "more body" in body


def test_the_body_keeps_its_own_blank_lines():
    _, body = split_frontmatter("---\nid: wiki-0006\n---\n\nleading blank stays\n")
    assert body == "\nleading blank stays\n"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - vault_note: one splitter, tolerant of BOM/CRLF/fence whitespace")
