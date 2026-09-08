#!/usr/bin/env python3
"""A read-only window on the injection channel, served on localhost only.

**What this is.** One page and one JSON endpoint that answer "what is the injection channel doing
right now, and may it be judged yet". The verdict, its sample floors and the label floors are read
out of `agents/shared/verdict_core.py` and `agents/shared/label_core.py` — this file computes no
threshold of its own. A second copy of a pre-registered number is how the two start disagreeing at
the exact moment someone wants the sample to be big enough (docs/PRD.md §2).

**Why it is a separate process.** Two reasons, both structural:

* The engine (`drudge/src/serve.rs`) has no CORS layer. A page served from any other origin cannot
  call `/health` or `/events` at all — the browser refuses the response before the page sees it.
* The injection ledger is a file (`~/.cache/boring-distill/injections.jsonl`). No page, on any
  origin, can read a file off the user's disk. Something local has to read it and hand it over.

So the assembly happens SERVER-SIDE here, and the page renders what it is given. That is also why
every key in `/api/state` is always present: a page that has to guess whether a field is missing
because the source was down or because the value was zero will eventually guess wrong, out loud.

**What it deliberately does not show.**

* No prompt text, and no `prompt_words`. That field is the user's raw prompt, unredacted, and
  1154 of 1541 documents in this corpus are company-origin. Only hit source *basenames* and the
  phrase windows the ledger already stores leave this process.
* No absolute paths, no host names, no org identifiers, no tokens. Phrase windows are scrubbed of
  anything path-shaped on the way out.
* No `dist` / `dist_kind` on a prompt row. The ledger stores no `query_log` id, so a distance
  could only be attached by matching a basename inside a time window — an assertion of identity
  the data cannot support, in the one view whose entire job is explaining *why*.
* No psql, ever. Engine HTTP plus the ledger file, nothing else.
* No generative endpoint: `/ask`, `/brief`, `/weekly`, `/status`, `/decisions`, `/risks`,
  `/next_actions`, `/stalled` are measured at 21-64 seconds and are never called from here.
* No writes of any kind. GET only, no mutating route, no file written anywhere.
* No auth, and nothing that invites one: no token flag, no `.env` read, no credential storage.
  It binds 127.0.0.1 and refuses to bind anything else, which is the whole of its access control.
"""

import argparse
import json
import pathlib
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "agents", "shared"))

import boring_config  # noqa: E402
import label_core  # noqa: E402
import uptake_core  # noqa: E402
import verdict_core  # noqa: E402

#: The window, transcribed from docs/PRD.md §8 D1 (the first window, 08-26 -> 09-09, was reset for
#: an instrumentation fault; no threshold moved). Constants rather than flags on purpose: a window
#: you can widen from the command line is a window that gets widened on the day it closes short.
# Dates come from `verdict_core`, which the PRD-transcription test covers. Spelling them here
# again is how a window that was reset on 08-31 left a docstring claiming it closed on 09-09.
WINDOW_SINCE = verdict_core.WINDOW_SINCE
WINDOW_UNTIL = verdict_core.WINDOW_UNTIL

#: `verdict_core.collect()` takes a `since` and has NO upper bound. The ceiling is applied here,
#: before collect() ever sees a row, by the same lexical `observed_at[:10]` comparison collect()
#: uses — otherwise this page starts printing a verdict containing post-window events the moment
#: the window closes.

#: PRD §2 계측 결함 조항: sessions ended but zero `injection_uptake` inside 48h is an
#: instrumentation investigation, not a verdict. `distill_resolution` is the session-end trace —
#: `distill_core.log_uptake_event` and `_log_resolution_event` both fire from SessionEnd.
FAULT_WINDOW_HOURS = 48
SESSION_END_EVENT = "distill_resolution"
UPTAKE_EVENT = "injection_uptake"

#: Newest ledger rows carried to the page. The ledger holds ~1000 rows and each carries up to four
#: hits of four phrase windows; the whole file is a megabyte of JSON that no reader scrolls. When
#: rows are dropped, a note says so — a truncated list must never read as a short one.
MAX_PROMPT_ROWS = 200

EVENT_LIMIT = 5000
ENGINE_TIMEOUT_S = 20

#: Never echoed into the response: it can carry a host name (`http://boring-drudge:7700`).
_ENGINE_URL = (os.environ.get("BORING_URL") or "http://127.0.0.1:7700").rstrip("/")

#: 127.0.0.1 and nothing else. There is no auth anywhere in this system, so the loopback bind IS
#: the access control; a --host flag would be an invitation to hand that away.
WHY_QUERIES = 40  # how many recent `search` rows the distance-band summary reads
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 7788

PAGE_PATH = os.path.join(_HERE, "peek.html")

# ------------------------------------------------------------------- note origin

#: Where the vault lives, resolved from this file rather than an env var a caller controls.
_VAULT_WIKI = pathlib.Path(__file__).resolve().parent.parent / "vault" / "wiki"

#: Cache: a page load touches the same handful of notes many times over.
_ORIGIN_CACHE = {}


def _note_is_personal(basename):
    """True only when the note says `origin: personal` in its own frontmatter.

    Unknown counts as NOT personal. A note we cannot read, or one whose frontmatter we cannot
    parse, is withheld — the failure direction has to be silence, because the other direction
    puts company prose on a page.
    """
    if basename in _ORIGIN_CACHE:
        return _ORIGIN_CACHE[basename]
    verdict = False
    path = _VAULT_WIKI / basename
    try:
        # Frontmatter only; never the body.
        with path.open(encoding="utf-8", errors="ignore") as handle:
            for _ in range(24):
                line = handle.readline()
                if not line or line.startswith("---") and _:
                    break
                if line.startswith("origin:"):
                    verdict = line.split(":", 1)[1].strip().strip("\"'") == "personal"
                    break
    except OSError:
        verdict = False
    _ORIGIN_CACHE[basename] = verdict
    return verdict


# --------------------------------------------------------------------------- redaction

#: A phrase window is a slice of a distilled note, and notes quote whatever the session quoted:
#: `ls -l` output with the account name in it, a clone URL, a workspace path with the home
#: directory flattened into `-users-<name>-...`. The scrub is per token and it DENIES rather than
#: escapes — the reader keeps the shape of the phrase and loses the identifier inside it.
#:
#: Path-shaped covers both spellings, because the flattened one is how orca workspace paths appear
#: in transcripts and the `/users/` pattern alone did not see them (measured: three rows carrying
#: the account name got through a slash-only rule).
_DENY = re.compile(
    r"""(
        [/\\]                          # any path separator, either spelling
        | (^|[^a-z0-9])-?(users|home)- # flattened home paths: -users-<name>-...
        | @                            # emails, git ssh remotes
        | \b[a-z0-9-]+\.(com|net|org|io|dev|ai|co|kr|jp|cloud|app)\b
        | \b[0-9a-f]{24,}\b            # long hex: an id or a secret, never worth carrying
        | \bsk-[a-z0-9]                # api-key shapes
    )""",
    re.IGNORECASE | re.VERBOSE,
)

REDACTED = "[redacted]"


def _account_names():
    """Local account names to deny, matched but never emitted.

    Taken from the home directory and the environment rather than hardcoded: the point is that
    this file carries no identity of its own, and a name only ever leaves as `[redacted]`.
    """
    names = {os.path.basename(os.path.expanduser("~")), os.environ.get("USER") or ""}
    return {n.lower() for n in names if len(n) >= 3}


def _company_terms():
    """Company identifiers, read from the repo's own origin policy — never a list typed here.

    `boring.json` `repos[]` already declares which matchers mean `origin: company`; that is the
    SSOT for "this word names the employer". A second hand-maintained list would go stale the
    first time a repo was added, and going stale here means an identifier ships.
    """
    terms = set()
    try:
        rules = boring_config.load().get("repos") or []
    except Exception:  # noqa: BLE001 — a missing/corrupt config must not take the page down
        return terms
    for rule in rules:
        if (rule.get("origin") or "").lower() == "personal":
            continue
        for raw in (rule.get("match"), rule.get("name")):
            token = (raw or "").strip().lower()
            if len(token) >= 4:
                terms.add(token)
            # The distinctive head of `foodspring-admin-front` is `foodspring`, and that is the
            # word that actually appears inside a phrase window.
            head = token.split("-", 1)[0]
            if len(head) >= 5:
                terms.add(head)
    return terms


_DENY_TERMS = _account_names() | _company_terms()


def _deny(token):
    # A bare separator is punctuation, not a path: distilled notes carry "## 배경 / 문제" headings
    # and redacting the slash itself would shred every phrase window while hiding nothing.
    if not token.strip("/\\"):
        return False
    lowered = token.lower()
    if _DENY.search(token):
        return True
    return any(term in lowered for term in _DENY_TERMS)


def _scrub(text):
    """Token-wise scrub of a stored phrase window."""
    if not text:
        return ""
    return " ".join(REDACTED if _deny(tok) else tok for tok in str(text).split())


def _basename(src):
    """Basename only, and scrubbed. Ledger sources are arbitrary basenames, not just wiki-NNNN.md."""
    tail = str(src or "").replace("\\", "/").rsplit("/", 1)[-1]
    return REDACTED if _deny(tail) else tail


# --------------------------------------------------------------------------- engine


def _get_json(path):
    """GET one engine endpoint, or None. None means "the engine would not say" — never zero."""
    try:
        with urllib.request.urlopen(_ENGINE_URL + path, timeout=ENGINE_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def engine_block(health):
    if not isinstance(health, dict):
        return {
            "reachable": False,
            "status": None,
            "build_sha": None,
            "sync": None,
            "corpus_count": None,
            "vector": None,
        }
    count = health.get("corpus_count")
    return {
        "reachable": True,
        "status": health.get("status"),
        "build_sha": health.get("build_sha"),
        "sync": health.get("sync"),
        "corpus_count": int(count) if isinstance(count, (int, float)) else None,
        "vector": health.get("vector") if isinstance(health.get("vector"), bool) else None,
    }


# --------------------------------------------------------------------------- window / verdict


def _in_window(row):
    """The same day bucket `verdict_core.collect()` uses, and the same window on both sides.

    `observed_at` is RFC3339 UTC but the window's dates are the owner's (§2, `WINDOW_TZ`), so the
    conversion belongs in one place and this asks for it rather than repeating the arithmetic.
    """
    # The day bucket comes from `verdict_core.window_day`, which converts to the owner's calendar
    # (WINDOW_TZ) rather than slicing the UTC string. Bucketing here and there differently is how
    # one boundary event lands inside the window on one surface and outside it on the other.
    observed = verdict_core.window_day(row.get("observed_at"))
    if not observed:
        return False
    return WINDOW_SINCE <= observed <= WINDOW_UNTIL


def window_block(rows, notes):
    """The verdict, taken verbatim from verdict_core. No threshold is recomputed here."""
    windowed = [r for r in (rows or []) if _in_window(r)]
    # The owner kept the window and split the sample at the ledger repair (PRD §8 D4). The page
    # must read the same half the verdict reads, or it becomes a second opinion nobody registered.
    pre_rows, post_rows = verdict_core.partition_at_repair(windowed)
    pre_agent, _ = verdict_core.collect(pre_rows, since=WINDOW_SINCE)
    per_agent, skipped_old = verdict_core.collect(post_rows, since=WINDOW_SINCE)
    if pre_agent:
        pre_sessions = sum(c["sessions"] for c in pre_agent.values())
        pre_prompts = sum(c["total_prompts"] for c in pre_agent.values())
        notes.append(
            f"수리 이전 표본 세션 {pre_sessions} · 주입 프롬프트 {pre_prompts} 은 판정에서 제외된다"
            f" (경계 {verdict_core.LEDGER_REPAIR_AT}). 원장이 3일에 잘리던 시기라 3일보다 오래 산"
            " 세션은 채점 전에 증거가 사라졌고, 그 편향은 세션 길이 방향이다 — PRD §8 D4."
        )

    # Per agent, never pooled: Claude Code injects on every prompt while Kimi throttles to once a
    # session (`verdict_core` / `distill_core.log_uptake_event`), so a pooled rate answers neither
    # product's question. When more than one adapter reported, the largest sample is the one shown
    # and the others are named in `notes` — summing them would fabricate a rate nobody registered.
    if per_agent:
        agent, counts = max(per_agent.items(), key=lambda kv: kv[1]["total_prompts"])
        if len(per_agent) > 1:
            others = ", ".join(sorted(a for a in per_agent if a != agent))
            notes.append(
                f"판정은 어댑터 '{agent}' 표본만이다. 같은 창에 {others} 도 보고했지만 합산하지"
                " 않는다 — 어댑터마다 주입 빈도가 달라 합산 비율은 어느 쪽 질문에도 답하지 못한다."
            )
        else:
            notes.append(f"판정 표본은 어댑터 '{agent}' 것이다 (창 안에서 보고한 유일한 어댑터).")
        # §2 gates the "not working" reading on the detector having been shown to see. Passing the
        # probe rather than omitting it is the difference between a verdict and a guess: on
        # 2026-09-08 both arms sat at zero and this page printed 비작동 with nobody having asked.
        verdict = verdict_core.verdict(
            counts["sessions"],
            counts["used_prompts"],
            counts["total_prompts"],
            counts["used_control_prompts"],
            detector_sensitive=uptake_core.detector_is_sensitive(),
        )
    else:
        notes.append(
            "이벤트 저장소를 못 읽어 판정 표본이 없다 — 아래 판정 줄은 표본 0 에 대한 판정"
            " 거부이며, 업테이크가 0 이라는 뜻이 아니다."
            if rows is None
            else "창 안에 집계할 injection_uptake 이벤트가 없다 — 아래 판정 줄은 표본 0 에 대한"
            " 판정 거부이며, 비율이 0 이라는 뜻이 아니다."
        )
        verdict = verdict_core.verdict(0, 0, 0, 0)

    lost_sessions, lost_rows = verdict_core.unreported(windowed)
    if skipped_old:
        notes.append(
            f"per-prompt 대조 카운터보다 오래된 이벤트 {skipped_old}건은 집계에서 빠졌다."
            " 0 으로 세면 우연율이 0 인 것처럼 읽힌다 — 계측 결함이 아니라 카운터가 더 어린 것이다."
        )
    # §2's coverage clause, which this page did not read until 2026-09-08: below half, the number
    # stops being a verdict and becomes an instrumentation investigation. The CLI knew
    # (`clears_floor: false`) and the page printed 비작동 anyway — the clause lived in one of the
    # two surfaces that render the same window.
    scored_sessions, distilled_sessions = set(), set()
    for row in windowed:
        sid = row.get("session_id") or (row.get("attributes") or {}).get("session_id")
        if not sid:
            continue
        if row.get("event") == UPTAKE_EVENT:
            scored_sessions.add(sid)
        elif row.get("event") == SESSION_END_EVENT:
            distilled_sessions.add(sid)
    cov_ratio, cov_eligible, cov_ok = verdict_core.coverage(
        len(distilled_sessions), len(scored_sessions)
    )
    if not cov_ok:
        notes.append(
            f"커버리지 {len(scored_sessions)}/{cov_eligible}"
            f" = {'—' if cov_ratio is None else f'{cov_ratio:.0%}'}"
            f" < {verdict_core.COVERAGE_FLOOR:.0%} — PRD §2 에 따라 판정이 아니라 계측 조사다."
            " 위 판정 줄은 우리가 **본 것**에 대한 진술이지 우리가 **보낸 것**에 대한 진술이 아니다."
        )

    return {
        "line": list(verdict_core.format_verdict(verdict)),
        "label": verdict.label,
        # Reported beside the verdict, never folded into it: §2 wants both numbers visible so a
        # reader cannot quote the rate without knowing what share of the channel produced it.
        "coverage": {
            "scored_sessions": len(scored_sessions),
            "eligible": cov_eligible,
            "ratio": None if cov_ratio is None else round(cov_ratio, 4),
            "clears_floor": bool(cov_ok),
            "floor": verdict_core.COVERAGE_FLOOR,
            "denominator": "all_distilled_sessions",
        },
        "floor_sessions": verdict.sessions,
        "min_sessions": verdict_core.MIN_SESSIONS,
        "floor_prompts": verdict.total_prompts,
        "min_prompts": verdict_core.MIN_INJECTED_PROMPTS,
        "since": WINDOW_SINCE,
        "until": WINDOW_UNTIL,
        "excluded_pre_counter": int(skipped_old),
        "unreported": {"sessions": int(lost_sessions), "rows": int(lost_rows)},
    }


# --------------------------------------------------------------------------- instrument fault


def _observed_dt(row):
    raw = row.get("observed_at") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def instrument_fault(rows):
    """PRD §2 계측 결함 조항 as a STATE, not a metric.

    `unknown` when the engine would not say — an unreachable event store is not a clean bill of
    health, and it is not a fault either. Both counts are null there, so the page cannot render
    "0 session ends" for "I could not look".
    """
    if rows is None:
        return {
            "state": "unknown",
            "reason": "엔진 이벤트 저장소에 접근 못 함 — 계측이 정상인지 아닌지 판단할 근거가 없다.",
            "session_ends_48h": None,
            "uptake_events_48h": None,
        }
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FAULT_WINDOW_HOURS)
    # Distinct sessions, not rows. `distill_resolution` can fire more than once for one session
    # (a retry, a repair), and PRD §3 names a surface that reports those rows as a session count
    # as an instrumentation fault in itself. Measured 2026-09-08: 324 rows against 302 sessions
    # inside the window, so the two diverge by enough to move a ratio.
    end_sessions, uptake_sessions = set(), set()
    for row in rows:
        when = _observed_dt(row)
        if when is None or when < cutoff:
            continue
        sid = row.get("session_id") or (row.get("attributes") or {}).get("session_id")
        if row.get("event") == SESSION_END_EVENT:
            end_sessions.add(sid or f"row:{id(row)}")
        elif row.get("event") == UPTAKE_EVENT:
            uptake_sessions.add(sid or f"row:{id(row)}")
    ends, uptakes = len(end_sessions), len(uptake_sessions)
    if ends and not uptakes:
        state = "investigate"
        reason = (
            f"최근 {FAULT_WINDOW_HOURS}h 에 세션 종료 {ends}건이 있는데 injection_uptake 는 0건이다"
            " — PRD §2 에 따라 판정이 아니라 계측 조사 대상."
        )
    elif not ends and not uptakes:
        state = "unknown"
        reason = (
            f"최근 {FAULT_WINDOW_HOURS}h 에 세션 종료도 uptake 도 0건 — 계측이 고장난 것과"
            " 세션이 없었던 것을 구분할 수 없다."
        )
    else:
        state = "ok"
        reason = (
            f"최근 {FAULT_WINDOW_HOURS}h: 세션 종료 {ends}건 · injection_uptake {uptakes}건 —"
            " 계측이 돌고 있다."
        )
    return {
        "state": state,
        "reason": reason,
        "session_ends_48h": ends,
        "uptake_events_48h": uptakes,
    }


# --------------------------------------------------------------------------- ledger


def _load_ledger(path):
    """Every ledger row, newest last, or None when the file is not readable.

    `uptake_core.load_records` is the shape of record this parses — one JSON object per line with
    `session_id` / `ts` / `hits[{src,phrases}]` / `controls[...]` / `prompt_words` — but it filters
    to a single session, and this page is a view over all of them. Same skip-the-malformed-line
    rule, same "unreadable is not empty" distinction: None, not [].
    """
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return None
    return rows


def _iso(ts):
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def _echo_map(rows):
    """session_id -> True/False/None from that session's `injection_uptake` event.

    A row is only scored at SessionEnd, so a session with no event was never scored: that is
    unknown, not false. When an event exists it is a SESSION-level count, and this page holds no
    transcript to re-score with. So the only per-prompt claims that can be made honestly are the
    unanimous ones: `used_prompts == 0` means no prompt in that session was echoed, and
    `used_prompts == total_prompts` means all were. A partial session stays None — picking which
    of its prompts was the used one would be an invention.
    """
    out = {}
    for row in rows or []:
        if row.get("event") != UPTAKE_EVENT:
            continue
        sid = row.get("session_id") or (row.get("attributes") or {}).get("session_id")
        if not sid:
            continue
        used = verdict_core.field(row, "used_prompts")
        total = verdict_core.field(row, "total_prompts")
        if total and used == total:
            out[sid] = True
        elif used == 0:
            out[sid] = False
        else:
            out[sid] = None
    return out


def prompt_rows(ledger, echoes, notes, page=None):
    """The sanctioned question, newest first: which sources went in, and was anything echoed.

    `page` is filled in with what was kept and what the ledger actually held. The prose note stays
    too, but a caveat at the bottom of a page is not a number a reader can see beside the list —
    "200 prompts" and "200 of 974 prompts" are different claims about the same screen.
    """
    rows = sorted(ledger or [], key=lambda r: r.get("ts") or 0, reverse=True)
    if page is not None:
        page["total"] = len(rows)
        page["limit"] = MAX_PROMPT_ROWS
        page["returned"] = min(len(rows), MAX_PROMPT_ROWS)
        page["truncated"] = len(rows) > MAX_PROMPT_ROWS
    if len(rows) > MAX_PROMPT_ROWS:
        notes.append(
            f"원장 {len(rows)}행 중 최신 {MAX_PROMPT_ROWS}행만 실었다 — 목록이 짧은 것이 아니라"
            " 자른 것이다. 판정·표본 하한은 잘리지 않은 이벤트 전량에서 계산된다."
        )
        rows = rows[:MAX_PROMPT_ROWS]
    out = []
    for row in rows:
        sid = row.get("session_id") or ""
        hits = []
        for hit in row.get("hits") or []:
            src = _basename(hit.get("src"))
            if not src:
                continue
            # A phrase window is arbitrary note prose, and 1154 of 1541 notes are company-origin.
            # A deny-list over arbitrary Korean prose can never be complete — it stops the shapes
            # someone thought of. The note's own `origin:` is the repo's SSOT for this exact
            # distinction, so a company note yields its basename and nothing else: structural,
            # not a guess about what a secret looks like.
            personal = _note_is_personal(src)
            hits.append(
                {
                    "src": src,
                    "phrases": (
                        [_scrub(p) for p in (hit.get("phrases") or []) if p] if personal else []
                    ),
                    "origin_withheld": not personal,
                }
            )
        out.append(
            {
                "ts": _iso(row.get("ts")),
                # First 8 characters. Enough to group a session's prompts on the page, not enough
                # to be a handle on anything, and never the repo or origin the row also carries.
                "session": str(sid)[:8],
                "hits": hits,
                "controls_n": len(row.get("controls") or []),
                "echoed": echoes.get(sid),
            }
        )
    return out


# --------------------------------------------------------------------------- labels


def labels_block(stats):
    """Label counts with label_core's own floors. `null` precision is the contract, not a gap."""
    block = {
        "llm_precision": None,
        "llm_n": 0,
        "human_n": 0,
        "compared": 0,
        "min_compared": label_core.MIN_COMPARED,
        "owed": label_core.MIN_COMPARED,
    }
    if not isinstance(stats, dict):
        return block
    judges = {j.get("judge"): j for j in stats.get("judges") or [] if isinstance(j, dict)}
    for judge, key in ((label_core.JUDGE_LLM, "llm_n"), (label_core.JUDGE_HUMAN, "human_n")):
        row = judges.get(judge) or {}
        block[key] = int(row.get("relevant") or 0) + int(row.get("irrelevant") or 0)
    llm = judges.get(label_core.JUDGE_LLM) or {}
    block["llm_precision"] = label_core.precision(
        int(llm.get("relevant") or 0), int(llm.get("irrelevant") or 0)
    )
    block["compared"] = int(stats.get("compared") or 0)
    block["owed"] = label_core.audit_backlog(stats)
    return block



# --------------------------------------------------------------------------- scoring / pace


def scoring_block(prompt_rows_out):
    """How much of what is on screen has actually been scored — of what is ON SCREEN.

    The population is the ledger rows this page is showing: the newest 200, capped to the last
    three days the ledger keeps. It is NOT the verdict's population, which is the
    `injection_uptake` events, and the two do not agree — read together on 2026-09-02, this block
    said 61 scored with 0 echoes while the event series held 11 echoes across 770 prompts. Quoting
    this number as "the echo rate" is quoting a sample as a population, and it was done four times
    before anyone checked.

    The hidden risk this exists to surface: `echoed` is only known when a session ENDS, sessions
    here run for days, and the ledger ages out after three. So the newest rows structurally
    over-represent sessions that have not been scored yet — the table looks busy and permanently
    pending, and whether scoring happens at all never shows. Filling the 200-prompt floor buys
    nothing if the scored share stays near zero.
    """
    total = len(prompt_rows_out)
    determined = sum(1 for r in prompt_rows_out if r.get("echoed") is not None)
    echoed = sum(1 for r in prompt_rows_out if r.get("echoed") is True)
    return {
        # `of` names the population on every read, so the number cannot travel without it.
        "of": "화면의 원장 행 (최근 3일, 판정 모집단 아님)",
        "rows": total,
        "determined": determined,
        "echoed": echoed,
        "pending": total - determined,
    }


def pace_block(window, now=None):
    """Pure measurement logistics: at this rate, does the sample reach its floor by the close?

    NOT evidence about the channel. It says whether there will be anything to judge, which is a
    different question from whether the thing works, and it breaks the moment traffic changes.
    """
    try:
        start = datetime.fromisoformat(WINDOW_SINCE + "T00:00:00+00:00")
        end = datetime.fromisoformat(WINDOW_UNTIL + "T23:59:59+00:00")
    except ValueError:
        return None
    # The counted sample begins at the ledger repair, not at the window open (PRD §8 D4): the
    # verdict reads only post-repair rows. Measuring elapsed time from the window open instead
    # divides a restarted count by days it was never accumulating over, and prints
    # `reaches_floor: false` — which reads as "we will miss the floor" when the truth is that the
    # baseline is hours old.
    repair = verdict_core._instant(verdict_core.LEDGER_REPAIR_AT)
    if repair and repair > start:
        start = repair
    now = now or datetime.now(timezone.utc)
    elapsed = max((now - start).total_seconds() / 86400.0, 0.25)
    span = max((end - start).total_seconds() / 86400.0, 1.0)
    # A linear projection over a day and a half is arithmetic, not a forecast. This exact error
    # has already been made in this project once — a weekend's session count extrapolated into a
    # verdict about the whole window — so the flag travels with the number.
    out = {
        "elapsed_days": round(elapsed, 1),
        "window_days": round(span, 1),
        "reliable": elapsed >= 3.0,
    }
    for key, have, floor in (
        ("prompts", window["floor_prompts"], window["min_prompts"]),
        ("sessions", window["floor_sessions"], window["min_sessions"]),
    ):
        per_day = have / elapsed
        out[key] = {
            "have": have,
            "floor": floor,
            "per_day": round(per_day, 1),
            "projected": int(per_day * span),
            "reaches_floor": int(per_day * span) >= floor,
        }
    return out


def why_block(entries):
    """Why these notes came back — the retriever's own numbers, not a guess.

    The ledger records no distance and no query_log id, so a prompt row cannot honestly carry one
    (a basename-and-timestamp match asserts an identity the data does not support). `query_log` is
    the engine's own record of the same retrieval, so it is read as its own view instead of
    stitched onto rows. The query TEXT is not carried: it is the raw prompt.

    Summarised as distance BANDS rather than a row list. A reader asking "why this note" wants the
    shape of the distances the retriever accepted, and 120 individual numbers do not show a shape.
    Bands are per `dist_kind`, never pooled: `vector_cosine` and `ts_rank` are different scales, so
    a single band table across both would compare numbers that mean different things.
    """
    if not isinstance(entries, list):
        return None
    by_kind = {}
    queries = 0
    for row in entries:
        if row.get("endpoint") != "search":
            continue
        dists = row.get("hit_dists") or []
        kinds = row.get("hit_dist_kinds") or []
        took = False
        for i, raw in enumerate(dists[:3]):
            try:
                d = float(raw)
            except (TypeError, ValueError):
                continue
            kind = kinds[i] if i < len(kinds) else None
            by_kind.setdefault(kind or "unknown", []).append(d)
            took = True
        if took:
            queries += 1
        if queries >= WHY_QUERIES:
            break
    if not by_kind:
        return None

    out = []
    for kind, values in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        values.sort()
        edges = [0.0, 0.30, 0.40, 0.45, 0.50, 0.55, 1.0]
        bands = []
        for lo, hi in zip(edges, edges[1:]):
            n = sum(1 for v in values if lo <= v < hi)
            bands.append({"lo": lo, "hi": hi, "n": n})
        out.append(
            {
                "kind": kind,
                "n": len(values),
                "min": round(values[0], 3),
                "median": round(values[len(values) // 2], 3),
                "max": round(values[-1], 3),
                "bands": bands,
            }
        )
    return {"queries": queries, "kinds": out}


# --------------------------------------------------------------------------- state


def build_state():
    """Assemble the whole response. Every source is optional; every absence is distinguishable."""
    notes = []
    health = _get_json("/health")
    events = _get_json(f"/events?limit={EVENT_LIMIT}")
    rows = events.get("entries") if isinstance(events, dict) else None
    if rows is None:
        notes.append(
            "엔진 이벤트 저장소에 접근 못 함 — 판정·계측 상태는 '알 수 없음'이다. 0 이 아니다."
        )
    if not isinstance(health, dict):
        notes.append("엔진 /health 응답 없음 — 엔진 블록은 미도달 표시이며 코퍼스가 빈 것이 아니다.")

    ledger_file = uptake_core.ledger_path()
    ledger = _load_ledger(ledger_file)
    if ledger is None:
        notes.append(
            "주입 원장 파일을 읽을 수 없다(없거나 권한 없음) — 프롬프트 목록은 빈 목록이며,"
            " 주입이 0건이었다는 뜻이 아니다."
        )

    notes.append(
        "원장은 주입된 텍스트를 저장하지 않는다 — 출처 basename 과"
        f" {uptake_core.PHRASE_WORDS}단어 구문창(최대 {uptake_core.MAX_PHRASES}개)만 저장한다"
        f" (agents/shared/uptake_core.py). 그래서 이 화면은 무엇이 주입됐는지 원문으로 보여줄 수 없다."
    )
    notes.append(
        f"원장 행은 {uptake_core.LEDGER_MAX_AGE_DAYS}일이 지나면 세션째로 사라진다"
        " (uptake_core.LEDGER_MAX_AGE_DAYS). 아래 목록은 최근 며칠의 창이고 창의 전량이 아니다."
    )
    notes.append(
        "종료되지 않은 세션의 주입(window.unreported)은 결과가 없어 비율의 분모에도 분자에도"
        " 들어가지 않는다 — 비율은 '측정된 것 중'이며 '보낸 것 중'이 아니다."
    )
    notes.append(
        "타임스탬프는 RFC3339 UTC 로 저장되지만 창 경계는 오너의 달력(KST, PRD §2)으로 판정한다"
        " — verdict_core.window_day 가 한 곳에서 변환한다. 표에 찍히는 시각은 UTC 원문이므로"
        " 자정 근처 행은 표의 날짜와 창 소속이 하루 다를 수 있다."
    )
    notes.append(
        "prompt 행에는 거리(dist)가 없다. 원장에 query_log id 가 없어서 basename 과 시간창으로"
        " 이어붙이는 수밖에 없고, 그것은 증명할 수 없는 동일성 주장이다. 그래서 거리는 행에"
        " 붙이지 않고 'why these notes' 에서 query_log 자신의 뷰로 따로 읽는다 — 같은 회수의"
        " 기록이지만 어느 프롬프트의 것인지는 주장하지 않는다."
    )
    notes.append(
        "프롬프트 원문은 이 응답에 없다 — 원장이 함께 보관하는 사용자 발화 토큰 필드도 같이"
        " 빠졌다. 무편집 원문이고 코퍼스 문서 1154/1541 이 회사 출처라 나갈 수 없다."
    )

    # Filled by prompt_rows below; a ledger we could not read leaves every count at zero and
    # `truncated` false, which the page reads together with the "could not read" note.
    page = {"returned": 0, "total": 0, "limit": MAX_PROMPT_ROWS, "truncated": False}
    echoes = _echo_map(rows or [])
    if ledger:
        extra, total, sessions = uptake_core.duplicate_injections(ledger_file)
        if extra:
            notes.append(
                f"원장에 한 프롬프트를 두 번 기록한 행 {extra}/{total} (세션 {sessions}) — 비율은"
                " 그대로지만 표본 하한(주입 프롬프트 수)이 부풀어 보인다."
            )

    prompts_out = prompt_rows(ledger, echoes, notes, page)
    # The engine's own record of the same retrievals. Read as its own view, never joined onto a
    # prompt row — the ledger holds no query_log id and matching by basename would assert an
    # identity the data cannot support.
    qlog = (_get_json("/query-log?limit=200") or {}).get("entries")
    if qlog is None:
        notes.append(
            "query_log 을 못 읽어 '왜 뽑혔나'(거리) 패널이 비었다 — 거리가 0 이라는 뜻이 아니다."
        )
    window = window_block(rows, notes)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine_block(health),
        "window": window,
        "instrument_fault": instrument_fault(rows),
        "prompts": prompts_out,
        "labels": labels_block(_get_json("/recall-label-stats")),
        "scoring": scoring_block(prompts_out),
        "pace": pace_block(window),
        "why": why_block(qlog),
        "page": page,
        "notes": notes,
    }


# --------------------------------------------------------------------------- server


class PeekHandler(BaseHTTPRequestHandler):
    """Exactly two GET routes. No directory listing, no static tree, no mutating method."""

    server_version = "peek/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # This page must never be framed or fetched by anything else; it holds no auth to lose,
        # but it does hold a view a browser tab on another origin has no business reading.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        """One POST, and it only ever stops this process.

        POST rather than GET on purpose: a GET that shuts a server down is a link a browser
        prefetch, a link checker, or an extension can follow, and the first symptom is a viewer
        that keeps dying for no visible reason. This route touches no corpus, no ledger and no
        measurement — it is process lifecycle for a tool the user started by hand, and it must
        never grow into anything that writes.
        """
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route != "/api/stop":
            self._send(404, "not found\n", "text/plain; charset=utf-8")
            return
        self._send(200, json.dumps({"stopping": True}), "application/json; charset=utf-8")
        # Shut down from another thread: serve_forever() is running on this one and calling
        # shutdown() from inside a handler deadlocks.
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/":
            try:
                with open(PAGE_PATH, "rb") as handle:
                    page = handle.read()
            except OSError:
                self._send(
                    503,
                    f"peek.html not found next to this script ({os.path.basename(PAGE_PATH)}).\n"
                    "GET /api/state still serves the data.\n",
                    "text/plain; charset=utf-8",
                )
                return
            self._send(200, page, "text/html; charset=utf-8")
            return
        if route == "/api/state":
            try:
                body = json.dumps(build_state(), ensure_ascii=False, indent=2)
            except Exception as exc:  # noqa: BLE001 — an observability page must not 500 silently
                body = json.dumps(
                    {"error": f"state assembly failed: {type(exc).__name__}"}, ensure_ascii=False
                )
                self._send(500, body, "application/json; charset=utf-8")
                return
            self._send(200, body, "application/json; charset=utf-8")
            return
        self._send(404, "not found\n", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write("[peek] %s\n" % (fmt % args))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only injection observability on loopback.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    args = ap.parse_args(argv)

    # There is no --host, and this assertion is here so adding one later fails loudly rather than
    # quietly exposing an unauthenticated view of a private corpus to the network.
    if BIND_HOST != "127.0.0.1":
        print("[peek] refusing to bind anything but 127.0.0.1", file=sys.stderr)
        return 2
    httpd = HTTPServer((BIND_HOST, args.port), PeekHandler)
    print(f"[peek] http://{BIND_HOST}:{args.port}/  (GET / · GET /api/state · POST /api/stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[peek] stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
