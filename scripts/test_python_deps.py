#!/usr/bin/env python3
"""The gate: every non-stdlib module this repo's Python imports is declared, and nothing else is.

Run: python3 scripts/test_python_deps.py   (no pytest dependency)

Why this exists. Until 2026-09-19 the tracked Python imported `yaml` in five files with no
requirements file anywhere, and `scripts/guard.sh` ran two of those files. The gate was green
because the GitHub runner image ships PyYAML — not because anything had asked for it. A green
that depends on someone else's base image is not a green, and the failure it hides arrives on a
stranger's machine at `clone → guard.sh`, which is exactly the first five minutes this product
claims to work in.

It runs in both directions on purpose: an undeclared import fails, and a declaration nothing
imports fails too. A requirements file that lists what the code stopped using teaches the next
reader that the dependency is load-bearing when it is not.
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"

#: Import name → distribution name, for the few where they differ.
IMPORT_TO_DISTRIBUTION = {"yaml": "pyyaml", "slack_sdk": "slack-sdk"}

#: The three ways this repo used to open a note by hand, each found in the source when the
#: splitter was written. They are spelled precisely on purpose: `startswith("---")` without the
#: newline is how a Markdown horizontal rule is detected (`scripts/peek.py`), which is a
#: different thing that happens to start with the same three characters.
_SPLIT_IDIOMS = ("[4:end]", 'startswith("---\\n")', 'split("---", 2)')


def _tracked_python() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [ROOT / p for p in out]


def _local_module_names(files: list[Path]) -> set[str]:
    return {p.stem for p in files} | {p.name.replace("-", "_") for p in files}


def _imported_top_level(files: list[Path]) -> dict[str, list[str]]:
    """Top-level module name → the files that import it."""
    found: dict[str, list[str]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:  # a file that does not parse is a different gate's problem
            raise AssertionError(f"{path.relative_to(ROOT)} does not parse: {e}") from e
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                found.setdefault(name, []).append(str(path.relative_to(ROOT)))
    return found


def _executable_lines(source: str) -> list[str]:
    """The file's lines with every docstring and comment blanked out.

    Uses the tokenizer rather than a regex: a string that merely mentions the old idiom is not
    the old idiom, and the difference is the whole reason the explanation can stay in the file.
    """
    import io
    import tokenize

    lines = source.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return lines
    prev_type = tokenize.NEWLINE
    for tok in tokens:
        is_docstring = tok.type == tokenize.STRING and prev_type in (
            tokenize.INDENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.ENCODING,
        )
        if tok.type == tokenize.COMMENT or is_docstring:
            for row in range(tok.start[0] - 1, tok.end[0]):
                blanked[row] = ""
        # A comment does not end a logical line, so it must not make the string that follows it
        # look like an expression — a `#!` shebang sits in front of every module docstring here.
        if tok.type not in (tokenize.NL, tokenize.COMMENT):
            prev_type = tok.type
    return blanked


def _declared() -> set[str]:
    assert REQUIREMENTS.exists(), "requirements.txt is missing — the one dependency is undeclared again"
    declared = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        declared.add(line.split("==")[0].split(">=")[0].split("[")[0].strip().lower())
    return declared


def test_every_third_party_import_is_declared():
    files = _tracked_python()
    assert files, "git ls-files found no Python — the gate would pass vacuously"
    local = _local_module_names(files)
    declared = _declared()
    undeclared = {}
    for name, importers in _imported_top_level(files).items():
        if name in sys.stdlib_module_names or name in local:
            continue
        distribution = IMPORT_TO_DISTRIBUTION.get(name, name).lower()
        if distribution not in declared:
            undeclared[name] = sorted(set(importers))[:3]
    assert not undeclared, (
        f"imported but not in requirements.txt: {undeclared}. Declare it, or drop the import — "
        "a gate that passes only on a runner that happens to ship it is not passing."
    )


def test_nothing_is_declared_that_nothing_imports():
    files = _tracked_python()
    imported = {IMPORT_TO_DISTRIBUTION.get(name, name).lower() for name in _imported_top_level(files)}
    unused = _declared() - imported
    assert not unused, (
        f"declared in requirements.txt but imported nowhere: {sorted(unused)}. "
        "A stale declaration reads as load-bearing to whoever installs it next."
    )


def test_the_frontmatter_split_has_exactly_one_implementation():
    """`agents/shared/vault_note.py` is that implementation. The hand-rolled `text[4:end]` idiom
    is what it replaced, and a fifth copy of it would fail the same way the four did: all at once,
    on the same notes, silently."""
    offenders = []
    for path in _tracked_python():
        if path.resolve() == Path(__file__).resolve():
            continue  # this file holds the idioms as data; that is what makes it the gate
        source = path.read_text(encoding="utf-8")
        # Judge code, not prose: this file and vault_note.py both have to name the old idiom in
        # order to explain it, and a gate that cannot tell a docstring from a statement teaches
        # people to stop writing the explanation.
        code = "\n".join(line for line in _executable_lines(source) if line)
        if any(idiom in code for idiom in _SPLIT_IDIOMS):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"hand-rolled frontmatter split in {offenders} — call vault_note.split_frontmatter instead"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - python deps: declared both ways, one frontmatter splitter")
