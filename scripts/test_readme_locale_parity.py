#!/usr/bin/env python3
"""README.ko.md and README.ja.md are translations, not forks.

They drift silently: nobody reads a language they don't speak, so a section
added to the English README simply never appears in the other two, and no
test notices. That is this repo's recurring defect — one value in two places,
one of them going stale — wearing a different hat.

This does not check the prose. It checks the two things a translation cannot
legitimately change: how many sections there are, and which commands the
reader is told to run. If English grows a section or a `make` target, the
translations have to grow it too or this fails and names what is missing.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGLISH = "README.md"
TRANSLATIONS = ("README.ko.md", "README.ja.md")

HEADING = re.compile(r"^#{2,4} ", re.M)
# Only the invocation, not the arguments: `make bench-llm-tier TIER=32gb` and
# `make bench-llm-tier TIER=16gb` are the same command to a reader.
MAKE_TARGET = re.compile(r"\bmake ([a-z][a-z0-9-]*)")
# A fenced block in one language may legitimately differ in comments, so the
# targets are collected from the whole file rather than from code fences only.


def read(name):
    path = ROOT / name
    if not path.is_file():
        sys.exit(f"FAIL: {name} is missing")
    return path.read_text(encoding="utf-8")


def main():
    english = read(ENGLISH)
    want_headings = len(HEADING.findall(english))
    want_targets = set(MAKE_TARGET.findall(english))
    if not want_targets or want_headings < 5:
        sys.exit(f"FAIL: {ENGLISH} parsed as {want_headings} headings and "
                 f"{len(want_targets)} make targets — the parser is broken, "
                 "not the translations")

    failures = []
    for name in TRANSLATIONS:
        text = read(name)
        got_headings = len(HEADING.findall(text))
        if got_headings != want_headings:
            failures.append(
                f"{name}: {got_headings} sections, {ENGLISH} has {want_headings}. "
                "A section was added or removed on one side only."
            )
        missing = sorted(want_targets - set(MAKE_TARGET.findall(text)))
        if missing:
            failures.append(
                f"{name}: never mentions " + ", ".join(f"`make {t}`" for t in missing)
            )

    if failures:
        print("FAIL: the translated READMEs have drifted from " + ENGLISH)
        for line in failures:
            print("  - " + line)
        print("\nPort the missing section rather than deleting the translation —"
              " measure the gap before you decide it is unmaintainable.")
        sys.exit(1)

    print(f"ok - README locale parity ({want_headings} sections, "
          f"{len(want_targets)} make targets across "
          f"{len(TRANSLATIONS) + 1} languages)")


if __name__ == "__main__":
    main()
