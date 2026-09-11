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
import glob
import json
import os
import re
import sys
import time
from typing import NamedTuple

import transcript


def _transcript_format(path):
    """Which reader this file needs, from where it lives."""
    return "codex-jsonl" if os.sep + ".codex" + os.sep in path else "claude-json"


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
    """Lower-cased tokens; a sentence-ending `.`/`:`/`,` is not part of the word it follows —
    `per wiki-1603.` names wiki-1603, and until 2026-09-11 the scorer could not see that."""
    return [w for w in (t.rstrip(".:,") for t in _WORD.findall((text or "").lower())) if w]


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
        path = hit.get("source_path") or ""
        src = path.rsplit("/", 1)[-1]
        snippet = " ".join((hit.get("snippet") or "").split())[:280]
        if not (src and snippet):
            continue
        out.append({"src": src, "path": path, "phrases": phrases(snippet)})
    return out


def note_path(hit):
    """The engine-side path of a ledgered hit; rows older than the `path` field get the wiki default."""
    return hit.get("path") or f"/vault/wiki/{hit.get('src', '')}"


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


def sources_already_injected(session_id, path=None):
    """Basenames this session has already been handed, so the hook does not hand them again."""
    return {
        hit.get("src")
        for row in load_records(session_id, path)
        for hit in row.get("hits") or []
        if hit.get("src")
    }


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


def source_names(src):
    """The forms an agent cites a source under: the basename, and its stem when the stem is a
    numbered id. Agents write `wiki-1603`, the ledger stores `wiki-1603.md`; a bare-word stem
    like `pool` is not tried because it would match ordinary prose."""
    src = src.lower()
    if not src:
        return []
    stem = src[: -len(".md")] if src.endswith(".md") else src
    if stem != src and re.search(r"\d", stem):
        return [src, stem]
    return [src]


def hit_was_used(hit, assistant_words_text, prompt_words):
    """True if the assistant echoed this hit's source name or one of its phrases.

    A phrase the user already used is not evidence: the agent would have said it anyway. That
    subtraction is what keeps this from being a similarity score between prompt and answer.
    """
    prompt_blob = " ".join(prompt_words or [])
    # The same subtraction the phrases get. It was missing here, and a note name is the easiest
    # thing for a user to type: "wiki-1292.md 다시 봐" would have counted as the agent using a
    # memory it was told to look at. Every path into this function has to survive the question
    # "would the agent have said this anyway".
    for name in source_names(hit.get("src") or ""):
        if _contains(assistant_words_text, name) and not _contains(prompt_blob, name):
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


#: Words an assistant uses when it follows the fence protocol's second sentence — "if one
#: contradicts the code in front of you, say which". Matched inside the sentence that names the
#: note, so "wiki-1603 is outdated" counts and "wiki-1603 fixed the outdated pool" does too;
#: that overcount is accepted over the alternative of asking a model.
CONTESTED_MARKERS = (
    "contradict", "outdated", "stale", "no longer", "wrong", "incorrect", "superseded",
    "어긋", "낡", "틀렸", "틀린", "맞지 않", "지금은 다르", "더 이상",
)

_SENTENCE = re.compile(r"[.!?\n]+")

#: The sentence shapes in which an assistant says one note replaced another. English names the
#: newer note first ("wiki-1683 instead of wiki-1682"); Korean names the older first
#: ("wiki-1682 대신 wiki-1683"). Both groups are named so the order is carried by the pattern.
_NOTE = r"[\w-]*\d[\w-]*(?:\.md)?"
_SUPERSEDES_FORMS = (
    re.compile(rf"\b(?P<newer>{_NOTE})\b[^.!?\n]{{0,40}}?\b(?:instead of|rather than|replaces|supersedes)\b[^.!?\n]{{0,12}}?\b(?P<older>{_NOTE})\b", re.IGNORECASE),
    re.compile(rf"\b(?P<older>{_NOTE})\b[^.!?\n]{{0,12}}?(?:대신|말고)[^.!?\n]{{0,12}}?\b(?P<newer>{_NOTE})\b", re.IGNORECASE),
)


def consumption(records, transcript_text):
    """What this session did with each note it was handed.

    Returns `(used_paths, contested_paths, supersedes_pairs)`. Used is the scorer's own test
    (`hit_was_used`). Contested is a note the assistant named in a sentence that also carries a
    contradiction marker — the act the fence protocol asks for. Supersedes is a `(newer, older)`
    pair the assistant named as "X instead of Y" where both were handed to the session. A note
    can be used and contested at once: it was read closely enough to be argued with.
    """
    text = assistant_text(transcript_text)
    blob = " ".join(_words(text))
    sentences = [" ".join(_words(s)) for s in _SENTENCE.split(text.lower()) if s.strip()]
    by_name = {}
    used, contested, supersedes = [], [], []
    for record in records or []:
        prompt_words = record.get("prompt_words") or []
        for hit in record.get("hits") or []:
            path = note_path(hit)
            names = source_names(hit.get("src") or "")
            for n in names:
                by_name.setdefault(n, path)
            if hit_was_used(hit, blob, prompt_words) and path not in used:
                used.append(path)
            if path not in contested and any(
                _contains(s, n) and any(m in s for m in CONTESTED_MARKERS) for s in sentences for n in names
            ):
                contested.append(path)
    for s in sentences:
        for form in _SUPERSEDES_FORMS:
            for m in form.finditer(s):
                newer, older = by_name.get(m.group("newer")), by_name.get(m.group("older"))
                if newer and older and newer != older and (newer, older) not in supersedes:
                    supersedes.append((newer, older))
    return used, contested, supersedes


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


def pipeline_probe(records, transcript_path):
    """`sensitivity_probe` asked through `transcript.extract` instead of around it.

    `(ok, reason)`; `None` when there was nothing to probe with — absence of a probe, not a pass.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None, "no transcript to read — the pipeline cannot be probed without one"
    try:
        body = transcript.extract(transcript_path, _transcript_format(transcript_path))
    except (OSError, ValueError) as exc:
        return False, f"transcript.extract failed on {os.path.basename(transcript_path)}: {exc}"
    if not _TURN.search(body):
        return False, (
            f"{os.path.basename(transcript_path)} read back with no turn markers at all —"
            " the reader and the turn splitter disagree, so every uptake score over it is 0"
            " for a reason that has nothing to do with the channel"
        )
    if not assistant_text(body):
        return None, (
            f"{os.path.basename(transcript_path)} has no assistant turns — nothing to probe with,"
            " which is not the same as a probe that failed"
        )
    for record in records or []:
        prompt_words = set(record.get("prompt_words") or [])
        for hit in record.get("hits") or []:
            for phrase in hit.get("phrases") or []:
                words = _words(phrase)
                if not words or any(w in prompt_words for w in words):
                    continue
                # Appended as its own assistant turn, in the shape the reader emits, so the match
                # has to survive everything between the file and the scorer.
                probe = [dict(record, controls=[], hits=[hit])]
                result = session_uptake(probe, body + "\n[assistant] " + phrase)
                if result.used_prompts >= 1:
                    return True, (
                        f"phrase from {hit.get('src') or '?'} survived"
                        f" {os.path.basename(transcript_path)} → extract → scorer"
                    )
                return False, (
                    f"a phrase from {hit.get('src') or '?'} was appended to a real transcript and"
                    " did not survive the read — the pipeline loses uses the detector can match"
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


def cross_session_rate(transcript_for, path=None, limit=12):
    """Per-hit uptake when each transcript is scored against *another* session's ledger hits.

    §2's instrument self-check. The sensitivity probe answers "can this detector see a use at
    all"; this answers the opposite question — "does it see uses that are not there". A phrase
    from a note that was never injected into this session should not turn up in its answer, so
    whatever rate comes back is what coincidence alone produces on this corpus. If it reaches the
    treatment rate, the measure is reading topic overlap rather than use.

    `transcript_for(session_id)` is passed in rather than read here: this module knows the ledger,
    not where transcripts live, and wiring the two together in one place is how the ledger reader
    ends up depending on an agent's directory layout.

    Returns `((cross_used, cross_total), (own_used, own_total))`. The second pair is the same
    scoring against each session's *own* hits, computed in the same pass over the same sessions:
    §2's bar is the cross rate relative to the treatment rate, and a treatment rate taken from a
    different set of sessions would not be the comparison the contract asks for.

    Zero total means the check could not be run — too few sessions to pair, or no transcript
    readable — which is not the same as a clean result and the caller has to tell them apart.
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
        return (0, 0), (0, 0)
    sessions = sorted(by_session)[:limit]
    if len(sessions) < 2:
        return (0, 0), (0, 0)
    used = total = own_used = own_total = 0
    for index, sid in enumerate(sessions):
        # Pair each session with the next one round the ring: every session is scored exactly
        # once, against hits it never received, with no session paired to itself.
        other = sessions[(index + 1) % len(sessions)]
        text = transcript_for(sid)
        if not text:
            continue
        blob = " ".join(_words(assistant_text(text)))
        for record in by_session[other]:
            prompt_words = record.get("prompt_words") or []
            for hit in record.get("hits") or []:
                total += 1
                if hit_was_used(hit, blob, prompt_words):
                    used += 1
        for record in by_session[sid]:
            prompt_words = record.get("prompt_words") or []
            for hit in record.get("hits") or []:
                own_total += 1
                if hit_was_used(hit, blob, prompt_words):
                    own_used += 1
    return (used, total), (own_used, own_total)


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


def _self_check_main(rest):
    """§2's instrument self-check, as a daily line rather than a verdict-day ritual.

    Prints `uptake_self_check=<state> cross=<u>/<t> …`. Exit 1 only when the check ran and the
    cross-session rate reached the treatment rate — a check that could not run is `unknown`, and
    an unrunnable check must not read as a passing one.

    Transcripts are found the way `distill-session` finds them, and that lookup lives here rather
    than in the scorer: the ledger reader must not learn an agent's directory layout.
    """
    import glob

    import verdict_core  # local: keeps the scorer importable without the verdict thresholds

    roots = [
        os.path.expanduser("~/.claude/projects"),
        os.path.expanduser("~/.codex/sessions"),
    ]
    index = {}
    for root in roots:
        for path in glob.iglob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            index.setdefault(os.path.splitext(os.path.basename(path))[0], path)

    def transcript_for(session_id):
        """The session's turns, in the shape `assistant_text` reads."""
        path = index.get(session_id)
        if not path:
            return ""
        try:
            return transcript.extract(path, _transcript_format(path))
        except (OSError, ValueError):
            return ""

    (used, total), (t_used, t_total) = cross_session_rate(
        transcript_for, rest[0] if rest else None
    )
    if not total:
        print("uptake_self_check=unknown cross=0/0 reason=too_few_sessions_or_transcripts")
        return 0
    rate = used / total
    treatment_rate = (t_used / t_total) if t_total else None
    ok = verdict_core.self_check_verdict(rate, treatment_rate)
    state = {True: "ok", False: "contaminated", None: "unknown"}[ok]
    print(
        f"uptake_self_check={state} cross={used}/{total} rate={rate:.4f}"
        f" treatment={t_used}/{t_total}"
    )
    return 1 if ok is False else 0


def _pipeline_probe_main(rest):
    """`--pipeline-probe` over the newest ledger session that has a transcript on disk."""
    records = []
    try:
        with open(rest[0] if rest else ledger_path(), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        print("uptake_pipeline_probe=unknown reason=no_ledger")
        return 0
    if not records:
        print("uptake_pipeline_probe=unknown reason=empty_ledger")
        return 0
    index = {}
    for root in (
        os.path.expanduser("~/.claude/projects"),
        os.path.expanduser("~/.codex/sessions"),
    ):
        for path in glob.iglob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            index.setdefault(os.path.splitext(os.path.basename(path))[0], path)

    for record in reversed(records):
        path = index.get(record.get("session_id"))
        if not path:
            continue
        ok, reason = pipeline_probe([record], path)
        if ok is None:
            continue
        state = {True: "ok", False: "blind"}[ok]
        print(f"uptake_pipeline_probe={state} reason={reason}")
        return 1 if ok is False else 0
    print("uptake_pipeline_probe=unknown reason=no_ledger_session_has_a_transcript")
    return 0


def _transcript_index():
    import glob

    index = {}
    for root in (os.path.expanduser("~/.claude/projects"), os.path.expanduser("~/.codex/sessions")):
        for path in glob.iglob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            index.setdefault(os.path.splitext(os.path.basename(path))[0], path)
    return index


def _consumption_main(rest, write):
    """`--consumption <session_id> [--write]`: what a session did with its notes, from its own
    ledger rows and transcript; `--write` hands it to the engine the way SessionEnd does."""
    if not rest:
        print("usage: uptake_core.py --consumption <session_id> [--write]", file=sys.stderr)
        return 2
    session_id = rest[0]
    path = _transcript_index().get(session_id)
    if not path:
        print(f"consumption session={session_id} reason=no_transcript")
        return 1
    records = load_records(session_id)
    text = transcript.extract(path, _transcript_format(path))
    used, contested, supersedes = consumption(records, text)
    print(
        f"consumption session={session_id} ledger_rows={len(records)} used={len(used)}"
        f" contested={len(contested)} supersedes={len(supersedes)}"
    )
    for p in used:
        print(f"  used       {p}")
    for p in contested:
        print(f"  contested  {p}")
    for newer, older in supersedes:
        print(f"  supersedes {newer} -> {older}")
    if write:
        import distill_core

        distill_core.write_consumption_to_graph(session_id, records, text)
        print("  written to the engine")
    return 0


def _main(argv):
    rest = [a for a in argv if not a.startswith("--")]
    if "--consumption" in argv:
        return _consumption_main(rest, "--write" in argv)
    if "--sensitivity-probe" in argv:
        return _probe_main(rest)
    if "--pipeline-probe" in argv:
        return _pipeline_probe_main(rest)
    if "--self-check" in argv:
        return _self_check_main(rest)
    if "--duplicate-injections" not in argv:
        print(
            "usage: uptake_core.py"
            " [--duplicate-injections|--sensitivity-probe|--pipeline-probe|--self-check]"
            " [ledger-path]",
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
