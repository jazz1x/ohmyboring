#!/usr/bin/env python3
"""How often an answer was already in the vault and the agent wrote it again anyway.

The uptake ledger answers "was what we injected used". It cannot answer the question the product
is actually judged on — *the agent does not re-solve a problem the owner already solved* — because
it only ever looks at the three notes the hook happened to inject. A turn that rebuilt an answer
the vault held in some other note is invisible to it, and that turn is the failure.

This measures the same corpus the window measures, offline:

    re-invention turn = an assistant turn whose text carries >= MIN_PHRASES distinctive phrases
    from one vault note, while that note is named nowhere in the session.

Named anywhere in the session means the agent reached it — through injection, a citation, a
direct read. Not naming it while reproducing several of its phrases is the shape of re-derivation.

**No hook and no engine call.** `scripts/anchor-shadow.py` already established why: a shadow
lookup through `/search` writes `query_log` rows, and `label-recall.py` samples that table, so
the measurement would quietly enter M1's sample as a query nobody was ever shown. Transcripts and
the vault are both files already on disk, so the number comes out of them directly.

Read-only: transcripts and `vault/wiki`. Writes nothing anywhere, calls nothing.
"""
import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents" / "shared"))
import boring_config  # noqa: E402
import transcript  # noqa: E402
import uptake_core  # noqa: E402

#: Phrases from one note a turn must carry before it counts as having reproduced that note. One
#: is a coincidence between two documents about the same subject; the scorer's own control arm
#: exists because that coincidence is real and common.
MIN_PHRASES = 2

#: Phrases fingerprinted per note. The scorer takes four from a 280-char snippet; a whole note is
#: longer, so it gets more windows — but not so many that a long note matches everything.
PHRASES_PER_NOTE = 12

#: Notes shorter than this have nothing distinctive to reproduce.
MIN_NOTE_CHARS = 400


def transcripts(days):
    cut = time.time() - days * 86400
    dirs = boring_config.source_dirs(adapter="session-end") or [
        os.path.expanduser("~/.claude/projects")
    ]
    out = []
    for directory in dirs:
        base = Path(directory)
        if base.is_dir():
            out.extend(p for p in base.glob("*/*.jsonl") if p.stat().st_mtime > cut)
    return sorted(out, key=lambda p: p.stat().st_mtime)


def vault_phrases(vault):
    """`{phrase: note}` over the wiki, and the set of note names, both read straight off disk."""
    by_phrase, names = {}, set()
    for path in sorted(Path(vault).glob("*.md")):
        body = path.read_text(encoding="utf-8", errors="replace")
        if len(body) < MIN_NOTE_CHARS:
            continue
        names.add(path.name)
        for phrase in uptake_core.phrases(body, limit=PHRASES_PER_NOTE):
            by_phrase.setdefault(phrase, path.name)
    return by_phrase, names


def windows(words, size=uptake_core.PHRASE_WORDS):
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def turns(text):
    """Assistant turns of one transcript, as word lists."""
    for body in uptake_core.assistant_text(text).split("\n"):
        words = uptake_core._words(body)
        if len(words) >= uptake_core.PHRASE_WORDS:
            yield words


def confirmed(hits):
    """Notes carried by enough distinct phrases to be a rebuild rather than a shared subject."""
    return {note: phrases for note, phrases in hits.items() if len(phrases) >= MIN_PHRASES}


def matches(text, by_phrase):
    """`{note: phrases}` this text reproduced, before the coincidence bar is applied."""
    hits = collections.defaultdict(set)
    for words in turns(text):
        for window in windows(words) & by_phrase.keys():
            hits[by_phrase[window]].add(window)
    return hits


def reinventions(path, by_phrase, names):
    """`{note: phrases matched}` for turns in this session that reproduced a note it never named."""
    try:
        text = transcript.extract(str(path), uptake_core._transcript_format(str(path)))
    except (OSError, ValueError):
        return {}, 0
    session_words = set(uptake_core._words(text))
    named = {n for n in names if n in session_words or n.removesuffix(".md") in session_words}
    hits = matches(text, by_phrase)
    unnamed = {note: phrases for note, phrases in hits.items() if note not in named}
    return confirmed(unnamed), sum(1 for _ in turns(text))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-invention rate, measured from transcripts.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=12, help="notes to list")
    ap.add_argument("--vault", default=str(Path(__file__).resolve().parent.parent / "vault" / "wiki"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    by_phrase, names = vault_phrases(args.vault)
    if not by_phrase:
        print("rework_shadow=unknown reason=no_vault_notes_long_enough", file=sys.stderr)
        return 1

    sessions = transcripts(args.days)
    per_note = collections.Counter()
    sessions_with, turns_scanned = 0, 0
    for path in sessions:
        found, scanned = reinventions(path, by_phrase, names)
        turns_scanned += scanned
        if found:
            sessions_with += 1
        for note in found:
            per_note[note] += 1

    share = (100.0 * sessions_with / len(sessions)) if sessions else 0.0
    if args.json:
        print(json.dumps({
            "days": args.days,
            "sessions": len(sessions),
            "sessions_with_reinvention": sessions_with,
            "share_pct": round(share, 1),
            "turns_scanned": turns_scanned,
            "notes": per_note.most_common(args.limit),
            "vault_notes_indexed": len(names),
        }, ensure_ascii=False))
        return 0

    print(
        f"rework_shadow sessions={len(sessions)} with_reinvention={sessions_with}"
        f" ({share:.1f}%) turns={turns_scanned} vault_notes={len(names)} days={args.days}"
    )
    for note, count in per_note.most_common(args.limit):
        print(f"  {count:3d} sessions rebuilt  {note}")
    if not per_note:
        print("  no turn reproduced a note it never named — at this corpus and these thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
