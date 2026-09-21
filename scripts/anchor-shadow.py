#!/usr/bin/env python3
"""What a file-scoped anchor lookup would have returned, measured without building one.

PRD §8 Q8 adopted the file as the unit of task scope and needs an anchor hit rate before the
09-15 slice: of the edits that land on a file some earlier session already touched, how many have
a note — and a claim — behind them.

**This does not install a hook.** A PostToolUse surface would be a second delivery path to get
wrong (seven merges have already failed to reach production), and calling `/search` from it would
write rows into `query_log`, which is where `label-recall.py` samples from — the shadow lookup
would quietly enter M1's sample as a query that was never injected. Transcripts already record
every Edit and Write with its path, so the same number comes out of data that is already sitting
on disk, with no runtime surface and nothing to contaminate.

Read-only: transcripts, the vault, and the claims table. Writes nothing anywhere.
"""

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents" / "shared"))
import boring_config  # noqa: E402

#: Extensions that count as code. Docs are excluded deliberately: `LOG.md` and `PRD.md` are the
#: most re-edited files in the corpus by a wide margin, and they are project ledgers being appended
#: to, not problems being re-solved. Counting them would inflate the revisit rate with the one kind
#: of file the north star is definitely not about.
CODE = re.compile(r"\.(py|rs|ts|tsx|js|jsx|go|java|sh|sql|rb|kt|swift|c|cc|cpp|h|hpp)$")

VAULT = Path(__file__).resolve().parent.parent / "vault" / "wiki"


def transcripts(days):
    cut = time.time() - days * 86400
    dirs = boring_config.source_dirs(adapter="session-end") or [os.path.expanduser("~/.claude/projects")]
    out = []
    for d in dirs:
        base = Path(d)
        if base.is_dir():
            out.extend(p for p in base.glob("*/*.jsonl") if p.stat().st_mtime > cut)
    return sorted(out, key=lambda p: p.stat().st_mtime)


def edits(paths):
    """(project, path, session) for every Edit/Write of a code file, oldest session first."""
    for f in paths:
        project, session = f.parent.name, f.stem
        with open(f, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in ("Edit", "Write"):
                        continue
                    target = (block.get("input") or {}).get("file_path") or ""
                    if CODE.search(target):
                        yield project, target, session


def vault_mentions(basenames):
    """Notes naming each file, counted by reading the vault directly.

    Not `rg`. Two reasons, and the second is the one that matters:

    - It is not installed on the CI runner, so the check silently reported zero everywhere.
    - **A missing tool returned 0, which reads exactly like "no note mentions this file."** That is
      the defect this whole measurement exists to avoid — absence rendered as a measurement — and
      it was sitting in the code that measures it. Reading the files has no such failure: a
      directory that cannot be read raises rather than answers.

    (`rg` would also need `--no-ignore` here, since `.gitignore` carries `vault/wiki/*` and
    ripgrep honours it when walking a directory. Reading the files sidesteps that too.)
    """
    counts = dict.fromkeys(basenames, 0)
    if not VAULT.is_dir():
        raise FileNotFoundError(f"vault not readable: {VAULT}")
    wanted = [n for n in basenames if n]
    for note in VAULT.glob("*.md"):
        try:
            body = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in wanted:
            if name in body:
                counts[name] += 1
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Anchor hit rate, measured from transcripts.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args(argv)

    seen = collections.defaultdict(set)
    total = 0
    revisit = collections.Counter()
    by_lang = collections.Counter()
    current = None
    touched = set()

    for project, target, session in edits(transcripts(args.days)):
        if current != session:
            for key in touched:
                seen[key].add(current)
            current, touched = session, set()
        total += 1
        key = (project, target)
        touched.add(key)
        # A revisit means SOME EARLIER session touched this file. Within-session repeats are the
        # same problem still being worked, not one being solved twice.
        if seen[key]:
            revisit[os.path.basename(target)] += 1
            by_lang[target.rsplit(".", 1)[-1]] += 1
    for key in touched:
        seen[key].add(current)

    if not total:
        print("[anchor-shadow] 전사에서 코드 편집을 못 찾았다 — --days 를 늘려보라")
        return 1

    revisits = sum(revisit.values())
    mentions = vault_mentions(list(revisit))
    covered_files = sum(1 for n in revisit if mentions.get(n))
    covered_edits = sum(c for n, c in revisit.items() if mentions.get(n))

    print(f"전사 {args.days}일 · 코드 편집 {total:,}")
    print(f"  재방문 편집 (이전 세션이 만진 파일)  {revisits:,} ({revisits / total:.0%})")
    print(
        f"  그중 노트가 있는 것                  {covered_edits:,} ({covered_edits / max(revisits, 1):.0%})"
        f"  · 고유 파일 {covered_files}/{len(revisit)}"
    )
    print(f"  **앵커 트리거 상한**                 {covered_edits / total:.1%} of all code edits")
    print()
    print("  언어:", ", ".join(f"{k} {v}" for k, v in by_lang.most_common(6)))
    print()
    print(f"  {'재방문':>6} {'노트':>5}  파일")
    for name, count in revisit.most_common(args.limit):
        print(f"  {count:>6} {mentions.get(name, 0):>5}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
