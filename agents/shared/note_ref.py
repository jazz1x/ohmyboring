"""Note references in the engine's spelling (C-NOTEID1) — standard library only.

Ids were renumbered to 11 digits; distill still writes old widths. One fold turns a note value
into the `note` + `note_reach` event attributes. Shipped as a standalone script beside
distill_core.py, so it imports nothing from the repo.
"""

import re
from typing import NamedTuple


class ReachableNote(NamedTuple):
    """A wiki note path in the engine's current 11-digit spelling."""

    path: str


class UnreachableNote(NamedTuple):
    """A note value no wiki id zero-fills to — kept as given, never dropped."""

    value: str


_WIKI_NOTE = re.compile(r"^(?P<dir>.*/)?wiki-(?P<digits>\d{1,11})\.md$")


def classify_note(value):
    """ReachableNote when `value` is a wiki path whose id zero-fills to 11 digits, else
    UnreachableNote(value)."""
    m = _WIKI_NOTE.match(value or "")
    if m is None:
        return UnreachableNote(value)
    return ReachableNote(f"{m['dir'] or ''}wiki-{m['digits'].zfill(11)}.md")


def note_fields(value):
    """The `note` + `note_reach` event attributes for one note value — the single fold over
    classify_note, shared by the writer and the card readers."""
    match classify_note(value):
        case ReachableNote(path):
            return {"note": path, "note_reach": "reachable"}
        case UnreachableNote(raw):
            return {"note": raw, "note_reach": "unreachable"}
