#!/usr/bin/env python3
"""Shared distillation core for session-end hooks (Claude Code, Kimi, etc.).

Hosts the pure, agent-agnostic pieces the host hooks need — standard library only:
  - marker throttle/retry bookkeeping
  - repo/origin classification
  - uptake / consumption recording at session end

The distillation itself (prompts, LLM call, graph, remember) lives in `ohmyboring.distill` and
runs where the engine dependencies live (hermes). Agent-specific transcript extraction and hook
I/O live in the per-agent modules.
"""

import os
import pathlib
import re
import subprocess
import sys
import time

# Allow import of shared agent policy library regardless of how this script is invoked.
# realpath resolves symlinks (e.g. hooks/distill-session.py → agents/claude-code/…) so the
# sibling agents/shared dir is found from the real file location, not the symlink's dir.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))
import markers  # noqa: E402
import transcript  # noqa: E402
import uptake_core  # noqa: E402

from ohmyboring import config as boring_config  # noqa: E402
from ohmyboring.adapters import events as event_log  # noqa: E402

# Minimum interval (minutes) before re-distilling an in-progress session (Stop hook).
# SessionEnd (final) ignores the throttle.
THROTTLE_MIN = int(os.environ.get("DISTILL_THROTTLE_MIN") or "25")


def _throttled(session_id):
    """True (skip) if this session was already distilled within the last THROTTLE_MIN minutes."""
    if not session_id:
        return False
    done_time = markers.done_time(session_id)
    if done_time is None:
        return False
    return (time.time() - done_time) < THROTTLE_MIN * 60


def _mark(session_id, retry=False, reason=""):
    """Write a done marker (.ts) or a retry marker (.retry).

    A .retry marker tells collect-sessions.py (the backfill scheduler) that this
    SessionEnd/Stop hook failed transiently and the session should be retried later.
    It is distinct from hermes-agent's .pending markers so the two queues don't collide.
    """
    # `make distill-now` sets this: the hook only queues, and hermes drains right away.
    if os.environ.get("BORING_DISTILL_NO_MARK"):
        return
    if not session_id:
        return
    if retry:
        # Pass the reason through. `mark_retry` already writes it into the marker and, on the
        # fifth attempt, into the `.dead` file — but every production caller used to omit it,
        # so dead letters recorded a timestamp and an attempt count and nothing about WHY.
        # Two sessions died that way on 2026-08-06 and 08-09 and are unexplainable today.
        markers.mark_retry(session_id, reason=reason)
    else:
        markers.mark_done(session_id)


def git_remote_url(cwd):
    """Return the git remote.origin.url of cwd (or its nearest git ancestor), or ''."""
    if not cwd:
        return ""
    try:
        # Walk up to the git root first so subdirectories resolve to the same
        # project name as the repository root.
        root = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not root:
            return ""
        return subprocess.run(
            ["git", "-C", root, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def git_main_worktree(cwd):
    """The main working tree behind `cwd`, or "" — the repo a worktree belongs to.

    `--git-common-dir` points at the shared `.git` of the checkout a linked worktree was created
    from, so this survives a worktree whose remote cannot be read. Worktrees normally resolve by
    remote already (they share the config), but a task worktree outlives nothing: when the parent
    checkout is gone, or the remote was never set, the remote lookup returns "" and the folder
    name is all that is left — and a task worktree's folder name is `<repo>-<task>`.
    """
    if not cwd:
        return ""
    try:
        common = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""
    if not common:
        return ""
    # `--git-common-dir` is relative to cwd when it is just ".git".
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    parent = os.path.dirname(os.path.abspath(common))
    return parent if os.path.isdir(parent) else ""


def repo_slug(cwd):
    """Category axis: the canonical repo slug, or "" when there is no repo to name.

    Order: the git remote (a linked worktree shares it, so `<repo>-<task>` collapses to `<repo>`),
    then the main working tree's folder name for a worktree whose remote is unreadable.

    A directory that is not a repository at all returns **empty**, deliberately. The old
    behaviour named the project after the folder, which invented one: 18 notes are filed under
    `다시한번` and single notes under `t5-parts` and `boro-janus-worker` — a phantom project takes
    a line in the briefing and a row in the graph, and nothing later can tell it from a real one.
    No project is a fact; a made-up project is not.
    """
    url = git_remote_url(cwd)
    if url:
        slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(?:\.git)?$", r"\1", url)
        if slug and slug != url:
            return boring_config.canonical_repo(slug)
    main = git_main_worktree(cwd)
    if main:
        return boring_config.canonical_repo(os.path.basename(main.rstrip("/")))
    return ""


#: Openings that mark a transcript as a tool running itself rather than a person working. These
#: sessions end, distil, and become notes exactly like a human one -- and they were 143 of the 183
#: notes written inside the measurement window (78%), 25-35 a day. A corpus that is four fifths
#: machine runs dilutes every recall made against it and then feeds those runs back in as memory.
#:
#: Matched against the first `[user]` turn only. A person quoting one of these phrases mid-session
#: is doing real work and their session is not an automated run.
AUTOMATED_RUN_OPENINGS = (
    "review this change for security vulnerabilities",
    # The same tool's second pass. Matching only its first stage let one run in four hundred
    # through, which is how a list like this rots: it is written against one day's transcripts and
    # never revisited. Both stages are named because both were observed.
    "you previously flagged these candidate vulnerabilities",
)


def is_automated_run(transcript_text):
    """True when this transcript is a tool driving itself, not a person solving something.

    The distinction is what the corpus is for: it holds how problems got solved, and a scripted
    review that says the same sentence every time carries no such story. Absence of a first user
    turn is not evidence either way, so it answers False and the session distils as before.
    """
    for line in (transcript_text or "").splitlines():
        if not line.startswith("[user]"):
            continue
        opening = line[len("[user]") :].strip().lower()
        return any(opening.startswith(mark) for mark in AUTOMATED_RUN_OPENINGS)
    return False


def transcript_index(source_dirs=None):
    """Map session id -> transcript path, over the same directories the collector scans.

    The roots come from `boring_config.source_dirs` rather than a fresh `~/.claude/projects`
    literal: a path written down twice is the defect this repo keeps paying for, and the one
    that goes stale is always the copy nobody runs.
    """
    roots = source_dirs
    if roots is None:
        roots = boring_config.source_dirs(adapter="session-end") or [os.path.expanduser("~/.claude/projects")]
    index = {}
    for root in roots:
        base = pathlib.Path(os.path.expanduser(root))
        if not base.is_dir():
            continue
        for path in base.rglob("*.jsonl"):
            index.setdefault(path.stem, path)
    return index


def transcript_reader(index=None, fmt="claude-json"):
    """A `transcript_for` callable over `transcript_index`, for `classify_automated_sessions`."""
    resolved = transcript_index() if index is None else index

    def read(session_id):
        path = resolved.get(session_id)
        if path is None:
            return None
        try:
            return transcript.extract(str(path), fmt)
        except Exception:
            return None

    return read


def classify_automated_sessions(session_ids, transcript_for):
    """Which of these sessions were automated runs, judged from the transcript.

    `is_automated_run` shipped on 2026-09-06 (#286). Every session distilled before that carries
    no `automated_run` reason -- not because it was a person's, but because nothing was asking.
    Inside the measurement window that is 181 sessions of scripted security review sitting in
    §2's coverage denominator, which is the whole distance between 19% coverage and 86%.

    Reading it back off the transcript rather than writing the missing label into the event log:
    the ledger is the measurement series, and a hand-written row in it is indistinguishable from
    one the instrument produced. This is a read-time judgement the caller can re-run and check.

    `transcript_for` maps a session id to its transcript text, or None when it cannot be read.
    Unreadable is not automated -- an unreadable session stays in the denominator, because the
    failure direction that shrinks a denominator is the one that flatters the result.

    Returns `(automated_ids, unreadable_ids)`.
    """
    automated, unreadable = set(), set()
    for sid in session_ids:
        try:
            text = transcript_for(sid)
        except Exception:
            text = None
        if not text:
            unreadable.add(sid)
        elif is_automated_run(text):
            automated.add(sid)
    return automated, unreadable


def write_consumption_to_graph(session_id, records, transcript_text):
    """Hand the graph what this session did with its notes. Spooled sink → no write; never raises."""
    if event_log._event_sink_mode() == "spool":
        return
    used, contested, supersedes, reasons = uptake_core.consumption_detail(records, transcript_text)
    if not (used or contested or supersedes):
        return
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    from ohmyboring.adapters.engine import DrudgeClient, PathMarks
    from ohmyboring.result import Err, Ok

    # 그래프 학습은 최선형(best-effort)이다 — 실패는 한 줄을 남기고 그친다.
    match DrudgeClient(timeout=10, retries=1).consumption(
        session_id,
        observed_at,
        PathMarks(
            used=used,
            contested=contested,
            supersedes=[list(p) for p in supersedes],
            judge="inferred",
        ),
    ):
        case Ok(_):
            pass
        case Err(failure):
            print(f"[distill-session] consumption write failed: {failure}", file=sys.stderr)
            return
    # The agent's own call is proposed, not final: the ranking may use it right away, but the
    # owner can flip it on the morning card. One event per used/contested note — supersedes
    # pairs name no single note, so they propose nothing. Only a successful consumption write
    # may leave these: a failed write with proposed events would show the owner calls the
    # graph never actually received. Each carries the sentence the scorer caught the mark in
    # (`reason`) — the review row's 이유 한 줄; rows proposed before this stored nothing, and
    # the card says 근거 없음 for those rather than inventing one.
    for kind, notes in (("used", used), ("contested", contested)):
        for note in notes:
            try:
                event_log.append_event(
                    "distill",
                    "verdict_proposed",
                    "ok",
                    session_id=session_id,
                    note=note,
                    kind=kind,
                    judge="inferred",
                    reason=reasons.get((kind, note), ""),
                )
            except Exception as e:  # noqa: BLE001 — never raises, same as the write above
                print(f"[distill-session] verdict_proposed event failed: {e}", file=sys.stderr)


def log_uptake_event(session_id, repo, transcript_text, agent):
    """Record whether the agent used any of what was injected into this session.

    Runs at SessionEnd because that is the first moment the whole conversation exists. Emits even
    when uptake is zero — a session where nothing landed is the observation, and dropping those
    rows would leave a ledger that only ever reports success.

    `agent` is required, not inferred: Claude Code injects on every prompt while Kimi throttles to
    once per session (`agents/kimi/recall.py`), so the two adapters are running different products
    and a pooled rate would answer neither. The verdict this measurement exists to settle has to
    be readable per adapter.

    Never raises and never blocks distillation: uptake is a measurement of the product, not part
    of the write door.
    """
    try:
        records = uptake_core.load_records(session_id)
        # The only event in the feed that means "a session ended". `distill_resolution` fires on
        # every distillation including mid-session compactions, so counting it as a session is
        # the defect docs/PRD.md §3 names — which left §2's coverage clause (scored sessions over
        # sessions this channel could have reached) with no denominator it could read, and left
        # "zero uptake rows" unable to say whether any session had ended at all.
        event_log.try_append_event(
            "recall-uptake",
            "session_end",
            "ok",
            session_id=session_id,
            repo=repo,
            agent=agent,
            injected_prompts=len(records),
        )
        if not records:
            return
        uptake = uptake_core.session_uptake(records, transcript_text)
        event_log.try_append_event(
            "recall-uptake",
            "injection_uptake",
            "ok",
            session_id=session_id,
            repo=repo,
            agent=agent,
            used_hits=uptake.used_hits,
            total_hits=uptake.total_hits,
            used_prompts=uptake.used_prompts,
            total_prompts=uptake.total_prompts,
            # The chance rate, recorded beside the treatment rate so no reader can quote one
            # without the other. A verdict from treatment alone cannot tell an effect from a
            # coincidence on the same topic.
            used_controls=uptake.used_controls,
            total_controls=uptake.total_controls,
            # The pre-registered metric is per-prompt on both sides (docs/PRD.md §2). Only the
            # per-hit control was ever emitted, so the recorded series could not answer the
            # question the contract asks. Same denominator as the treatment rate, which is what
            # makes the two comparable as a difference in percentage points.
            used_control_prompts=uptake.used_control_prompts,
        )
        write_consumption_to_graph(session_id, records, transcript_text)
        _ok, aged_sessions, aged_rows = uptake_core.prune_session(session_id)
        if aged_sessions:
            # Sessions that were injected into and never ended — killed terminals, torn-down
            # workers. Their rows are being deleted right now and this is the only record that
            # they existed. On the verdict day the difference between "2.3% of what we measured"
            # and "2.3% of what we sent" is exactly this number.
            event_log.try_append_event(
                "recall-uptake",
                "injection_unreported",
                "ok",
                session_id=session_id,
                repo=repo,
                agent=agent,
                aged_sessions=aged_sessions,
                aged_rows=aged_rows,
            )
    except Exception as e:  # noqa: BLE001 — a measurement must never cost a session its note
        print(f"[distill-session] uptake measurement failed: {e}", file=sys.stderr)
