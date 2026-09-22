#!/usr/bin/env python3
"""One place that knows how a vault note is shaped.

The vault is the source of truth (`docs/PRD.md` §1), and until this module existed its file
format was split open in four Python files and two Rust ones, each with its own copy of
`text[4:end]`. Four copies do not fail independently: they all assume the file begins with
exactly `---\\n`, so a note written with a BOM, with `--- ` (trailing space), or with CRLF
line endings is read as "no frontmatter" by every one of them at once, and the note silently
loses its id, project and claims on the next pass.

This module takes the assumptions out of the callers and into one function with tests. It
deliberately does NOT parse YAML — callers that need a mapping still bring their own parser,
because writing a YAML parser here would be a second, worse one.
"""

from __future__ import annotations

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split `---\\nyaml\\n---\\nbody` into (raw frontmatter, body), or None if there is none.

    Tolerates what the hand-rolled copies did not: a UTF-8 BOM, trailing whitespace on either
    fence line, and CRLF endings. Returns the frontmatter without its fences and the body with
    its leading newline consumed, so `split` + `join` is not needed to get at either half.
    """
    if not text:
        return None
    if text.startswith("﻿"):
        text = text[1:]
    normalised = text.replace("\r\n", "\n")
    first, sep, rest = normalised.partition("\n")
    if not sep or first.strip() != _FENCE:
        return None
    lines = rest.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == _FENCE:
            return "\n".join(lines[:i]), "\n".join(lines[i + 1 :])
    return None


def frontmatter_text(text: str) -> str:
    """The raw frontmatter alone, or `""` when the note has none.

    For callers that scan the frontmatter with a regex rather than parsing it — an empty string
    matches nothing, which is the same answer they reached by returning early.
    """
    split = split_frontmatter(text)
    return split[0] if split else ""
