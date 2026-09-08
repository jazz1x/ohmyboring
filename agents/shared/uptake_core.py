"""Did the agent actually use what was injected?

`recall_label` measures whether a hit *should* have helped — a judge's opinion. This module
measures something the judge cannot fake: whether the note the hook pushed in shows up in what
the agent said afterwards. Precision is about the librarian; uptake is about the reader.

It also answers a question precision cannot. The hook fires on every prompt with no opt-in, so
1472 injections a week say the hook is installed, not that anything wanted them — supply, not
demand. Uptake converts those same events into a demand signal.

**The trap this module exists to avoid**: the injected text becomes part of the transcript. Grep
the transcript for the snippet and it matches itself, every time, and the metric reads 100% while
measuring nothing. So uptake is only ever counted over ASSISTANT turns, and only for evidence
that is not already sitting in the user's own prompt.

No I/O beyond a JSONL append the caller hands a path to; the hooks own the rest.
"""

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from typing import NamedTuple

def ledger_path():
    """Where the recall hook leaves its record — resolved per call, never cached at import.

    Same cache dir the distill markers use, so one directory holds everything a session leaves
    behind. `BORING_INJECTION_LEDGER` redirects it, and the redirect has to be readable *now*
    rather than at import: the hook tests drive the real injection path, and a constant frozen
    at import time depends on whether the test set the variable before the first import touched
    this module. It did not, and the suite appended seven rows to the owner's live ledger.
    """
    return os.environ.get("BORING_INJECTION_LEDGER") or os.path.expanduser(
        "~/.cache/boring-distill/injections.jsonl"
    )

#: Words per phrase when fingerprinting a snippet. Long enough that a match is not a coincidence
#: of common words, short enough to survive the agent paraphrasing around it.
PHRASE_WORDS = 8

#: Phrases per injected hit. The snippet is 280 chars ≈ 40 words, so a handful of windows covers
#: it without turning one hit into hundreds of substring scans.
MAX_PHRASES = 4

_WORD = re.compile(r"[\w./:-]+", re.UNICODE)
_TURN = re.compile(r"^\[(user|assistant)\]\s?", re.MULTILINE)


def _words(text):
    return _WORD.findall((text or "").lower())


def phrases(snippet, size=PHRASE_WORDS, limit=MAX_PHRASES):
    """Distinctive word windows from an injected snippet, in order.

    Windows are spread across the snippet rather than taken from its head: distilled notes open
    with boilerplate section headers ("## 배경 / 문제"), so head-only windows would fingerprint
    the template instead of the content.
    """
    words = _words(snippet)
    if len(words) < size:
        return []
    windows = [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
    if len(windows) <= limit:
        return windows
    step = len(windows) / float(limit)
    return [windows[int(i * step)] for i in range(limit)]


#: A session whose SessionEnd never fires (killed terminal, crash) leaves its rows behind
#: forever: nothing prunes what nothing measures. Those rows are also a selection bias — the
#: sessions that report are the ones that ended cleanly — so they are dropped by age rather than
#: silently counted later against a transcript that no longer exists.
#:
#: The bound has to exceed how long a session actually lives, or it stops being a guard against
#: never-ending sessions and becomes a guard against LONG ones. At 3 days it was the latter:
#: measured 2026-09-02, the live ledger held 22 sessions with a median span of 48h and a max of
#: 177h, 10 of them past 72h, and those 10 held 875 of 1125 rows (78%). `log_uptake_event` returns
#: silently when a session has no rows, so those sessions were scored as nothing at all — 93
#: sessions distilled inside the measurement window, 11 with an uptake row. The exclusion is not
#: random: long sessions receive the most injections and carry the most opportunity for uptake, so
#: the sample kept exactly the sessions least likely to show a signal. 14 days clears the observed
#: maximum with margin and still bounds the file (~2k rows). See docs/PRD.md §8 D4.
LEDGER_MAX_AGE_DAYS = 14


def _fingerprints(hits, limit):
    out = []
    for hit in (hits or [])[:limit]:
        src = (hit.get("source_path") or "").rsplit("/", 1)[-1]
        snippet = " ".join((hit.get("snippet") or "").split())[:280]
        if not (src and snippet):
            continue
        out.append({"src": src, "phrases": phrases(snippet)})
    return out


def injection_record(session_id, prompt, hits, max_results, controls=None):
    """The row the recall hook appends when it injects.

    Stores the source basename and the snippet fingerprints — never the snippet itself, so the
    ledger cannot become a second copy of the vault. `prompt_words` is kept so a later match can
    be discounted when the user had already said the same thing.

    `controls` are hits the search returned but the hook did NOT inject. Scoring them the same way
    yields the chance rate: how often a note on this topic gets echoed anyway. An uptake number
    without that floor cannot distinguish "the memory was used" from "any note about this subject
    would have shared words with the answer", which is the mistake that produced 0.514.
    """
    # No session id means SessionEnd can never attribute this row to a transcript, so it could
    # only ever inflate the denominator. Dropping it here also stops any test that drives the
    # real injection path from appending to the owner's live ledger — a guard in code, because
    # the convention "remember to redirect the ledger in your test" already failed twice.
    if not session_id:
        return None
    injected = _fingerprints(hits, max_results)
    if not injected:
        return None
    return {
        "session_id": session_id,
        "ts": time.time(),
        "prompt_words": _words(prompt)[:400],
        "hits": injected,
        "controls": _fingerprints(controls, len(controls or [])),
    }


@contextlib.contextmanager
def _locked(target, mode):
    """Hold an exclusive lock on the ledger for the whole read-modify-write.

    The ledger is one file shared by every session on the machine, and pruning rewrites it
    whole. Without a lock the sequence is: a session ends, reads the file, filters its own rows
    out, and writes back what it saw -- discarding every row that other sessions appended while
    it was thinking. That is not a rare race. Measured 2026-09-06, 1,216 subagent transcripts and
    several hundred workflow runs in fourteen days, and the share of distilled sessions that
    reached the verdict fell from 95% on a quiet day to 12% once the machine ran hot.

    Yields None instead of a handle when the file cannot be opened, so callers keep their
    never-raises contract: a ledger failure must not cost the user a prompt.
    """
    handle = None
    try:
        handle = open(target, mode, encoding="utf-8")
    except OSError:
        yield None
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    except OSError:
        yield None
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        handle.close()


def append_record(record, path=None):
    """Append one record. Never raises — a failed ledger write must not cost the user a prompt."""
    if not record:
        return False
    target = path or ledger_path()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with _locked(target, "a") as handle:
            if handle is None:
                return False
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def load_records(session_id, path=None):
    """Records for one session, oldest first. Unreadable or malformed lines are skipped."""
    target = path or ledger_path()
    out = []
    try:
        with open(target, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("session_id") == session_id:
                    out.append(row)
    except OSError:
        return []
    return out


def assistant_text(transcript_text):
    """Only what the assistant said, concatenated.

    `transcript.extract` emits `[user] ...` / `[assistant] ...` turns. Uptake counted over user
    turns would count the injection quoting itself — the failure this module is built around.
    """
    if not transcript_text:
        return ""
    parts = _TURN.split(transcript_text)
    # split() yields [pre, role, body, role, body, ...]; keep bodies whose role is assistant.
    kept = [parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i] == "assistant"]
    return "\n".join(kept)


def _contains(blob, needle):
    """Substring match with word boundaries.

    Both sides are space-joined token streams, so padding each with a space turns a substring
    search into a token-sequence search. Without it `pool.md` matches inside `connection-pool.md`
    and the phrase `a b c` matches inside `xa b c` — ledger sources are arbitrary basenames, not
    only collision-safe `wiki-NNNN.md`.
    """
    if not needle:
        return False
    return f" {needle} " in f" {blob} "


def hit_was_used(hit, assistant_words_text, prompt_words):
    """True if the assistant echoed this hit's source name or one of its phrases.

    A phrase the user already used is not evidence: the agent would have said it anyway. That
    subtraction is what keeps this from being a similarity score between prompt and answer.
    """
    prompt_blob = " ".join(prompt_words or [])
    src = (hit.get("src") or "").lower()
    # The same subtraction the phrases get. It was missing here, and a note name is the easiest
    # thing for a user to type: "wiki-1292.md 다시 봐" would have counted as the agent using a
    # memory it was told to look at. Every path into this function has to survive the question
    # "would the agent have said this anyway".
    if _contains(assistant_words_text, src) and not _contains(prompt_blob, src):
        return True
    for phrase in hit.get("phrases") or []:
        if _contains(assistant_words_text, phrase) and not _contains(prompt_blob, phrase):
            return True
    return False


class Uptake(NamedTuple):
    """One session's treatment and control counts.

    Named rather than positional because the verdict reads specific pairs out of this and a
    seven-wide tuple of ints is a transposition waiting to happen — the two control fields sit
    next to each other and mean opposite denominators.
    """

    used_hits: int
    total_hits: int
    used_prompts: int
    total_prompts: int
    used_controls: int
    total_controls: int
    used_control_prompts: int


def session_uptake(records, transcript_text):
    """Treatment and control counts for one session.

    The control counts are scored identically over hits the search returned but the hook never
    injected: the agent could not have used them, so whatever rate they show is the chance rate
    that the treatment number has to beat. Reporting treatment alone is how a coincidence gets
    read as an effect.

    Two treatment rates, because they answer different questions: per-hit uptake says how much of
    what we push gets used, per-prompt uptake says how often an injection mattered at all.

    The control side carries both denominators for the same reason. The pre-registered metric is
    per-prompt treatment against per-prompt control (docs/PRD.md §2), and only the per-hit control
    was ever counted -- so the contract named a quantity the instrument did not produce, and the
    window would have closed with a comparison that could not be made. A per-hit control against a
    per-prompt treatment is not a smaller version of the right answer; it is a different ratio.
    """
    assistant_blob = " ".join(_words(assistant_text(transcript_text)))
    used_hits = total_hits = used_prompts = 0
    used_controls = total_controls = used_control_prompts = 0
    records = records or []
    for record in records:
        prompt_words = record.get("prompt_words") or []
        hits = record.get("hits") or []
        total_hits += len(hits)
        used_here = sum(1 for h in hits if hit_was_used(h, assistant_blob, prompt_words))
        used_hits += used_here
        if used_here:
            used_prompts += 1
        controls = record.get("controls") or []
        total_controls += len(controls)
        used_control_here = sum(
            1 for c in controls if hit_was_used(c, assistant_blob, prompt_words)
        )
        used_controls += used_control_here
        if used_control_here:
            used_control_prompts += 1
    return Uptake(
        used_hits,
        total_hits,
        used_prompts,
        len(records),
        used_controls,
        total_controls,
        used_control_prompts,
    )


def sensitivity_probe(records):
    """Can this detector see a use it is handed on a plate? Returns `(ok, reason)`.

    Treatment and control both sitting at zero is the signature of a channel nobody used AND the
    signature of a detector that sees nothing, and the rates cannot tell them apart. docs/PRD.md
    §2 therefore refuses to read a "not working" verdict until sensitivity is shown, and this is
    what shows it: take a phrase from a hit that was really injected, put it in an assistant turn
    verbatim, and require the scorer to find it.

    Real ledger records, not a fixture, because the failure modes worth catching live between the
    parts — a snippet that yields no phrases, a transcript format the turn splitter stopped
    matching, a normalisation change that makes stored phrases unmatchable. A fixture built from
    the same constants would pass through all three.

    The phrase is chosen to avoid `prompt_words`: a hit whose words the user already typed is
    excluded by design (that exclusion is the whole reason this measure is not self-fulfilling),
    so probing with one would fail for a correct reason and read as a broken detector.
    """
    for record in records or []:
        prompt_words = set(record.get("prompt_words") or [])
        for hit in record.get("hits") or []:
            for phrase in hit.get("phrases") or []:
                words = _words(phrase)
                if not words or any(w in prompt_words for w in words):
                    continue
                probe = [dict(record, controls=[], hits=[hit])]
                result = session_uptake(probe, "[assistant] " + phrase)
                if result.used_prompts >= 1:
                    return True, f"phrase from {hit.get('src') or '?'} was detected"
                return False, (
                    f"a phrase injected from {hit.get('src') or '?'} was handed back verbatim and"
                    " the scorer did not count it — the detector is blind, so a zero rate is not"
                    " evidence about the channel"
                )
    return None, "no ledger record carries a phrase outside its own prompt — nothing to probe with"


def prune_session(session_id, path=None, now=None, max_age_days=LEDGER_MAX_AGE_DAYS):
    """Drop this session's records, and any left behind by sessions that never ended.

    Never raises. Rows older than `max_age_days` go regardless of session: without that the
    ledger grows without bound, because the only thing that prunes a session is the SessionEnd
    that also measures it — and a killed session has neither.

    Returns `(ok, aged_sessions, aged_rows)`. Those two counts are the only trace a killed
    session ever leaves: its injections happened, were never scored, and are about to be
    deleted. Without them the verdict can report a rate but not what share of the channel it
    saw, and "2.3% of what we measured" reads exactly like "2.3% of what we sent". The caller
    records them; deleting the evidence of a blind spot silently is how a biased sample gets
    quoted as a population.
    """
    target = path or ledger_path()
    try:
        with _locked(target, "r+") as handle:
            if handle is None:
                return False, 0, 0
            return _prune_locked(handle, session_id, now, max_age_days)
    except OSError:
        return False, 0, 0


def _prune_locked(handle, session_id, now, max_age_days):
    """The body of `prune_session`, with the ledger already locked and open for read+write."""
    try:
        lines = handle.readlines()
    except OSError:
        return False, 0, 0
    cutoff = (now if now is not None else time.time()) - max_age_days * 86400
    # Age out whole sessions by their NEWEST row, never row by row. A session resumed across days
    # is still live, and dropping its early rows would leave it measured against a denominator
    # missing its own beginning — long sessions would be systematically under-counted while
    # looking perfectly healthy.
    newest = {}
    parsed = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            parsed.append((line, None, None))  # keep what we cannot parse rather than deleting it
            continue
        sid = row.get("session_id")
        ts = row.get("ts")
        parsed.append((line, sid, ts))
        if isinstance(ts, (int, float)):
            newest[sid] = max(newest.get(sid, ts), ts)
    kept = []
    aged_sessions = set()
    aged_rows = 0
    for line, sid, _ts in parsed:
        if sid is None:
            kept.append(line)
            continue
        if sid == session_id:
            continue
        last_seen = newest.get(sid)
        if isinstance(last_seen, (int, float)) and last_seen < cutoff:
            # This session never ended, so it was never scored. Count it before it is gone.
            aged_sessions.add(sid)
            aged_rows += 1
            continue
        kept.append(line)
    try:
        # Same handle, still locked. Reopening for write would drop the lock between the read and
        # the rewrite, which is the whole race this function was losing rows to.
        handle.seek(0)
        handle.truncate()
        handle.writelines(kept)
        handle.flush()
        return True, len(aged_sessions), aged_rows
    except OSError:
        return False, 0, 0


#: Two ledger rows for one prompt this far apart or closer are one UserPromptSubmit that ran the
#: recall hook twice, not a person asking the same thing again. The window is what the data
#: chose, not a number picked to be safe: when the hook was registered under two path spellings
#: (#245) all 455 duplicate pairs landed within 0.14s, and there was not a single pair anywhere
#: between that and the next observation. A real repeat cannot arrive inside it.
DUPLICATE_WINDOW_S = 1.0


def duplicate_injections(path=None, window=DUPLICATE_WINDOW_S):
    """Rows that are a second recording of one prompt: (extra_rows, total_rows, sessions).

    Double-firing does not move the uptake rate — numerator and denominator both double — so
    nothing in the numbers looks wrong. What it moves is `total_prompts`, and that is a
    pre-registered sample floor (docs/PRD.md §2). A floor met at half the evidence it names is
    not the floor that was registered, and the only trace is here.
    """
    target = path or ledger_path()
    seen = {}
    extra = total = 0
    sessions = set()
    try:
        with open(target, encoding="utf-8") as handle:
            rows = []
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return 0, 0, 0
    for row in sorted(rows, key=lambda r: (r.get("session_id") or "", r.get("ts") or 0)):
        total += 1
        key = (row.get("session_id"), tuple(row.get("prompt_words") or []))
        ts = row.get("ts") or 0
        previous = seen.get(key)
        if previous is not None and (ts - previous) <= window:
            extra += 1
            sessions.add(row.get("session_id"))
            continue
        seen[key] = ts
    return extra, total, len(sessions)


def _probe_main(rest):
    """Run the sensitivity probe against the newest real session in the ledger."""
    by_session = {}
    try:
        with open(rest[0] if rest else ledger_path(), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                sid = record.get("session_id")
                if sid:
                    by_session.setdefault(sid, []).append(record)
    except OSError:
        pass
    if not by_session:
        print("uptake_sensitivity=unknown reason=empty_ledger")
        return 0
    newest = max(
        by_session.values(), key=lambda rows: max((r.get("ts") or 0) for r in rows)
    )
    ok, reason = sensitivity_probe(newest)
    state = {True: "ok", False: "blind", None: "unknown"}[ok]
    print(f"uptake_sensitivity={state} rows={len(newest)} reason={reason}")
    return 1 if ok is False else 0


def detector_is_sensitive(path=None):
    """Whether the detector can see a use handed to it, for the verdict to gate on.

    The same probe `--sensitivity-probe` runs, returned rather than printed so the verdict path can
    read it. §2 makes this a precondition for the "not working" reading, and a precondition that
    only a CLI subcommand can reach is one the verdict never actually applies — which is how a
    dashboard came to print 비작동 on 2026-09-08 with both arms at zero and nobody having asked.

    None when there is no ledger to probe. That is not a passing answer and not a failing one; the
    caller treats anything other than True as grounds to withhold, so an absent ledger withholds.
    """
    by_session = {}
    try:
        with open(path or ledger_path(), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                sid = record.get("session_id")
                if sid:
                    by_session.setdefault(sid, []).append(record)
    except OSError:
        return None
    if not by_session:
        return None
    newest = max(
        by_session.values(), key=lambda rows: max((r.get("ts") or 0) for r in rows)
    )
    ok, _reason = sensitivity_probe(newest)
    return ok


def _main(argv):
    rest = [a for a in argv if not a.startswith("--")]
    if "--sensitivity-probe" in argv:
        return _probe_main(rest)
    if "--duplicate-injections" not in argv:
        print(
            "usage: uptake_core.py [--duplicate-injections|--sensitivity-probe] [ledger-path]",
            file=sys.stderr,
        )
        return 2
    extra, total, sessions = duplicate_injections(rest[0] if rest else None)
    print(f"injection_ledger duplicate_rows={extra} total_rows={total} sessions={sessions}")
    if extra:
        print(
            "  a prompt recorded twice means the recall hook fired twice; the uptake rate looks"
            " unchanged while the sample floor counts double",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
