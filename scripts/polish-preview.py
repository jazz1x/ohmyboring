#!/usr/bin/env python3
"""Preview the distill polish step on existing notes — read-only.

Runs ohmyboring.distill.polish on the named notes (or the N worst by data-steward's readability
signals) with the configured local model, and writes each note's before/after and the verdict to
a markdown file. No note is changed.

    python3 scripts/polish-preview.py --notes wiki-2216 wiki-2278 --out /tmp/polish.md
    python3 scripts/polish-preview.py --worst 3 --out /tmp/polish.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "src"))

from vault_note import split_frontmatter  # noqa: E402

from ohmyboring import config as boring_config  # noqa: E402
from ohmyboring.adapters import llm  # noqa: E402
from ohmyboring.distill import polish, readability  # noqa: E402


def _wiki_dir(vault: str | None) -> Path:
    root = vault or os.environ.get("BORING_VAULT_DIR") or "~/oh-my-boring/vault"
    return Path(root).expanduser() / "wiki"


def _title(frontmatter: str) -> str:
    line = next((line for line in frontmatter.splitlines() if line.startswith("title:")), "title:")
    return line.removeprefix("title:").strip().strip("'\"")


def _worst(wiki: Path, n: int) -> list[str]:
    scored = []
    for path in sorted(wiki.glob("wiki-*.md")):
        split = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if split is not None:
            scored.append((len(readability.body_signals(split[1])), path.stem))
    return [name for score, name in sorted(scored, key=lambda s: (-s[0], s[1])) if score][:n]


def _section(name: str, title: str, body: str, result: polish.PolishResult) -> str:
    before = readability.body_signals(body)
    match result:
        case polish.Polished(body=new):
            head = f"## {name} — 바꿈 ({', '.join(before)} → {', '.join(readability.body_signals(new)) or '깨끗'})"
            return f"{head}\n\n제목: {title}\n\n### 전\n\n{body}\n\n### 후\n\n{new}\n"
        case polish.Kept(reason=reason):
            return f"## {name} — 그대로 ({reason})\n\n제목: {title}\n\n### 전\n\n{body}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault")
    parser.add_argument("--notes", nargs="*", default=[])
    parser.add_argument("--worst", type=int, default=0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    wiki = _wiki_dir(args.vault)
    names = args.notes or _worst(wiki, args.worst)
    lang = boring_config.note_lang()
    sections = []
    for name in names:
        split = split_frontmatter((wiki / f"{name}.md").read_text(encoding="utf-8", errors="replace"))
        if split is None:
            sections.append(f"## {name} — 머리말 없음, 건너뜀\n")
            continue
        frontmatter, body = split
        result = polish.polish(body, lang, llm.call_llm)
        sections.append(_section(name, _title(frontmatter), body, result))
        print(f"[polish-preview] {name}: {type(result).__name__}", file=sys.stderr)
    Path(args.out).write_text("\n---\n\n".join(sections), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
