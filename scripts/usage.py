#!/usr/bin/env python3
"""What the agents cost, read from the transcripts nobody was aggregating.

Every assistant message in a Claude Code transcript carries `usage` (input, output, and the two
cache counters) and the `model` that produced it. 2947 files and 3.05 GB of it had accumulated
here by 2026-09-02, read only by the distiller, which takes the prose and drops the meters.

This is a meter, not a screen (PRD §4): it prints and exits. The reason it exists is not curiosity
about spend — it is that "the agent did not solve this again" shows up as a *shorter solve*, and
turns and tokens are the only place that is written down. Echo — the words of an injected note
reappearing — is the hour's proxy and the PRD says so; this is the input for a better one.

Read-only, local, no network. Writes exactly one thing: an incremental index under the cache dir,
so a rescan costs seconds instead of the ~19s a cold pass over 3 GB takes.
"""

import argparse
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents" / "shared"))

import boring_config  # noqa: E402
import distill_core  # noqa: E402

#: Counters that are actual token volumes. `input_tokens` excludes anything served from cache, so
#: the four are disjoint and summing them is the whole bill rather than a double count.
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def index_path():
    """Read the redirect at call time, not at import.

    Same reason `uptake_core.ledger_path()` does: a constant frozen at import depends on whether
    a caller set the variable before the first import touched this module, and that ordering is
    not something a test can rely on.
    """
    return os.environ.get("BORING_USAGE_INDEX") or os.path.expanduser(
        "~/.cache/boring-distill/usage-index.json"
    )


def transcript_files():
    """Session transcripts AND the subagent transcripts beside them.

    `boring_config.source_dirs` rather than a hardcoded `~/.claude/projects`, so a machine that
    configured its sources somewhere else is not silently reported as having no usage at all.

    The session collector globs `*/*.jsonl` — top-level only — because a subagent transcript is
    not a session to distil. A meter has the opposite requirement: measured here, top-level is
    609 files and the full tree is 2991, the difference being 2382 transcripts under
    `<session>/subagents/`. Counting only the top level reports a fraction of what the fan-out
    actually cost. Verified not to double count: a session's main transcript and its subagent
    files share zero `requestId`s.
    """
    out = []
    dirs = boring_config.source_dirs(adapter="session-end") or [os.path.expanduser("~/.claude/projects")]
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        out.extend(sorted(p for p in base.rglob("*.jsonl") if p.is_file()))
    return out


def _lane(path, row):
    """Which pool of spend this row belongs to. Never summed into one number by default.

    `subagents/` in the path is a spawned agent's own transcript; `isSidechain` marks a subagent
    turn recorded inline in the parent. Both are fan-out cost, and neither answers the question
    "what did this conversation cost" — so they stay separate lanes rather than inflating it.
    """
    if "subagents" in path.parts:
        return "subagent"
    return "sidechain" if row.get("isSidechain") else "main"


#: cwd -> slug. `repo_slug` shells out to git, and a scan asks about the same handful of
#: directories tens of thousands of times.
_REPO_CACHE = {}


def _repo_of(cwd):
    """The repo, resolved through `distill_core.repo_slug` — not the folder name.

    A linked worktree's folder is named after the task, not the repository, so a basename split
    files `foodspring-가격비교에-포함될것들` as its own project. `repo_slug` reads the git remote (which
    a worktree shares with its parent) and falls back to the main working tree, which is the whole
    reason it was written that way.
    """
    if not cwd:
        return ""
    key = str(cwd)
    if key not in _REPO_CACHE:
        try:
            _REPO_CACHE[key] = distill_core.repo_slug(key)
        except Exception:  # noqa: BLE001 — a meter must not die on one unreadable directory
            _REPO_CACHE[key] = ""
    return _REPO_CACHE[key]


def scan_file(path):
    """Fold one transcript into per-(day, model, repo, lane) token totals.

    Two things that would otherwise inflate the number:

    - **Retries.** A request that is retried writes more than one assistant row with the same
      `requestId`, and the usage on each is the same bill charged once. Counted once.
    - **Sidechains.** `isSidechain` marks a subagent's turns. Those tokens are real, but pooling
      them with the main loop answers neither question — "what did this conversation cost" and
      "what did fanning out cost" are different — so they are a separate lane, never a sum.
    """
    totals = collections.defaultdict(lambda: collections.defaultdict(int))
    seen = set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if row.get("type") != "assistant":
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            request_id = row.get("requestId") or row.get("uuid")
            if request_id:
                if request_id in seen:
                    continue
                seen.add(request_id)
            day = str(row.get("timestamp") or "")[:10]
            key = "|".join(
                (
                    day,
                    str(message.get("model") or "unknown"),
                    _repo_of(row.get("cwd")),
                    _lane(path, row),
                )
            )
            bucket = totals[key]
            bucket["messages"] += 1
            for name in TOKEN_KEYS:
                try:
                    bucket[name] += int(usage.get(name) or 0)
                except (TypeError, ValueError):
                    pass
    return {k: dict(v) for k, v in totals.items()}


def build_index(files, cached, rescan=False):
    """Rescan only what changed, keyed by size and mtime.

    A transcript is append-only while its session lives and frozen after, so size+mtime is enough
    to know a file is unchanged — and it is what makes this a meter you can run on every wake
    rather than a 19-second job you avoid.
    """
    index, scanned = {}, 0
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
        prior = cached.get(str(path))
        if not rescan and prior and prior.get("stamp") == stamp:
            index[str(path)] = prior
            continue
        index[str(path)] = {"stamp": stamp, "totals": scan_file(path)}
        scanned += 1
    return index, scanned


def fold(index, since=None, until=None):
    out = collections.defaultdict(lambda: collections.defaultdict(int))
    for entry in index.values():
        for key, counts in (entry.get("totals") or {}).items():
            day = key.split("|", 1)[0]
            if since and day < since:
                continue
            if until and day > until:
                continue
            for name, value in counts.items():
                out[key][name] += value
    return out


def _rows(folded, group):
    fields = ("day", "model", "repo", "lane")
    picked = [fields.index(g) for g in group]
    rolled = collections.defaultdict(lambda: collections.defaultdict(int))
    for key, counts in folded.items():
        parts = key.split("|")
        label = " · ".join(parts[i] or "—" for i in picked)
        for name, value in counts.items():
            rolled[label][name] += value
    return rolled


def _fmt(n):
    return f"{n:,}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Token and model usage, read from local transcripts.")
    ap.add_argument("--group", default="model", help="day,model,repo,lane (comma separated)")
    ap.add_argument("--since", help="ISO date, inclusive")
    ap.add_argument("--until", help="ISO date, inclusive")
    ap.add_argument("--rescan", action="store_true", help="ignore the cached index")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    group = [g.strip() for g in args.group.split(",") if g.strip()]
    bad = [g for g in group if g not in ("day", "model", "repo", "lane")]
    if bad:
        print(f"[usage] 알 수 없는 group: {', '.join(bad)}", file=sys.stderr)
        return 2

    files = transcript_files()
    if not files:
        print("[usage] 전사 파일을 못 찾았다 — boring.json 의 session-end source 를 확인하라")
        return 1

    path = index_path()
    cached = {}
    if not args.rescan:
        try:
            with open(path, encoding="utf-8") as fh:
                cached = json.load(fh)
        except (OSError, ValueError):
            cached = {}

    index, scanned = build_index(files, cached, rescan=args.rescan)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    os.replace(tmp, path)

    folded = fold(index, since=args.since, until=args.until)
    if not folded:
        print("[usage] 그 범위에 사용량이 없다")
        return 1

    rolled = _rows(folded, group)
    total = collections.defaultdict(int)
    for counts in rolled.values():
        for name, value in counts.items():
            total[name] += value

    head = " · ".join(group)
    print(f"전사 {len(files)}개 · 이번에 읽은 파일 {scanned}개 · 캐시 {path}")
    print()
    print(f"{head:<38} {'메시지':>9} {'입력':>12} {'출력':>12} {'캐시생성':>13} {'캐시읽기':>14}")
    print("-" * 102)
    ordered = sorted(rolled.items(), key=lambda kv: -kv[1]["output_tokens"])
    for label, counts in ordered[: args.limit]:
        print(
            f"{label:<38} {_fmt(counts['messages']):>9} {_fmt(counts['input_tokens']):>12}"
            f" {_fmt(counts['output_tokens']):>12}"
            f" {_fmt(counts['cache_creation_input_tokens']):>13}"
            f" {_fmt(counts['cache_read_input_tokens']):>14}"
        )
    if len(ordered) > args.limit:
        print(f"… {len(ordered) - args.limit}행 더 (--limit 로 조정)")
    print("-" * 102)
    print(
        f"{'합계':<38} {_fmt(total['messages']):>9} {_fmt(total['input_tokens']):>12}"
        f" {_fmt(total['output_tokens']):>12}"
        f" {_fmt(total['cache_creation_input_tokens']):>13}"
        f" {_fmt(total['cache_read_input_tokens']):>14}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
