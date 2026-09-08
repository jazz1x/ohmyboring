#!/usr/bin/env python3
"""Report the injection-channel verdict from the recorded `injection_uptake` events.

Run it on the window's closing date — `verdict_core.WINDOW_UNTIL`, which the PRD-transcription
test keeps honest — or any time before, to see how far the sample has come. (This line used to
spell the date, and still said 2026-09-09 a day after the reset moved it to 09-14.)

    python3 scripts/uptake-verdict.py
    python3 scripts/uptake-verdict.py --agent claude-code --since 2026-08-26

Reads the ENGINE's event store over HTTP, not `~/.cache/oh-my-boring/events.ndjson`. That file is
a spool the writer fills only when the DB sink fails, so on a healthy install it is empty — and an
empty spool reads exactly like an instrument that never fired. It cost this project a false alarm.

Rates are reported per agent, never pooled: Claude Code injects on every prompt while Kimi
throttles to once per session, so a combined rate answers neither product's question.
"""

import argparse
import json
from datetime import datetime, timezone
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agents", "shared"))

import verdict_core  # noqa: E402
import uptake_core  # noqa: E402
from verdict_core import collect, coverage, partition_at_repair, unreported  # noqa: E402

DEFAULT_URL = os.environ.get("BORING_URL") or "http://127.0.0.1:7700"


def fetch_events(base_url, limit):
    url = f"{base_url.rstrip('/')}/events?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")).get("entries") or []
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[uptake-verdict] engine unreachable at {url}: {exc}", file=sys.stderr)
        return None


def session_counts(rows, since, until):
    """Sessions distilled and sessions scored inside the window, for §2's coverage clause.

    Reads the same `/events` feed the verdict already fetches. The automated share is not counted
    here: this file cannot see a transcript, and guessing from an event would be inventing the
    number that decides whether a verdict is allowed. The caller that can classify passes it in;
    a caller that cannot gets the raw ratio and the contract's stricter reading of it.
    """
    distilled, scored = set(), set()
    for row in rows or []:
        observed = (row.get("observed_at") or "")[:10]
        if since and observed < since:
            continue
        if until and observed > until:
            continue
        sid = row.get("session_id") or (row.get("attributes") or {}).get("session_id")
        if not sid:
            continue
        name = row.get("event")
        if name == "distill_resolution":
            distilled.add(sid)
        elif name == "injection_uptake":
            scored.add(sid)
    return len(distilled), len(scored)


#: Exit codes for `--midpoint`. Distinct on purpose: four different absences read as "0 sessions"
#: if the caller only counts — the engine being down, no events at all, every event predating the
#: per-prompt control counter, and every event sitting before the ledger repair. Only the first of
#: those is a shortfall in the sample; the rest are a shortfall in what we can see, and a doctor
#: check that calls them the same thing points at the wrong repair.
MIDPOINT_OK = 0
MIDPOINT_SHORT = 3
MIDPOINT_UNREADABLE = 4
#: Distinct from OK. They both mean "no action", but a shell that has to tell them apart was
#: reading Korean prose to do it — so the wording of a message became the only thing separating
#: "the floor is met" from "the gate is not due", and rephrasing a sentence would have silently
#: turned one into the other.
MIDPOINT_NOT_DUE = 5


def _midpoint(per_agent, skipped_old, today=None):
    """The one-time progress gate. Silent outside its window — see PRD §2."""
    today = verdict_core.window_today(today)
    if today < verdict_core.MIDPOINT:
        print(
            f"[midpoint] 아직 아니다 — 중간점 {verdict_core.MIDPOINT}, 오늘 {today}",
            file=sys.stderr,
        )
        return MIDPOINT_NOT_DUE
    if today > verdict_core.WINDOW_UNTIL:
        # One-time by contract. A gate that keeps firing after the window teaches the reader that
        # a red doctor is the background colour.
        print(
            f"[midpoint] 창이 닫혔다({verdict_core.WINDOW_UNTIL}) — 중간점은 1회성이다",
            file=sys.stderr,
        )
        return MIDPOINT_NOT_DUE
    if not per_agent:
        reason = (
            f"per-prompt 대조 카운터보다 오래된 이벤트 {skipped_old}건뿐"
            if skipped_old
            else "이벤트 0건"
        )
        print(f"[midpoint] 읽을 표본이 없다 — {reason}. 표본 부족이 아니라 관측 불가", file=sys.stderr)
        return MIDPOINT_UNREADABLE
    floor = verdict_core.MIDPOINT_MIN_SCORED
    short = {who: c["sessions"] for who, c in per_agent.items() if c["sessions"] < floor}
    for who, c in sorted(per_agent.items()):
        print(f"[midpoint] {who}: 채점 세션 {c['sessions']} / {floor}", file=sys.stderr)
    if short:
        detail = ", ".join(f"{who} {n}" for who, n in sorted(short.items()))
        print(
            f"[midpoint] 어댑터당 {floor} 미달 ({detail}) — PRD §2 는 창 종료를 기다리지 않고"
            " 계측 조사로 전환하도록 등록했다",
            file=sys.stderr,
        )
        return MIDPOINT_SHORT
    return MIDPOINT_OK


def _coverage_payload(rows, since, until):
    """The coverage clause as data: the ratio, its denominator, and whether §2 allows a verdict."""
    distilled, scored = session_counts(rows, since, until)
    ratio, eligible, ok = coverage(distilled, scored)
    return {
        "distilled_sessions": distilled,
        "scored_sessions": scored,
        "eligible": eligible,
        "ratio": None if ratio is None else round(ratio, 4),
        "clears_floor": ok,
        # What the denominator is, said out loud. This script reads events, not transcripts, so it
        # cannot tell a person's session from a tool reviewing a diff -- and on 2026-09-07 that
        # distinction moved the ratio from 14% to 83% (§8 D8). A reader who does not know which
        # population a coverage number describes cannot use it, and a field named
        # `automated_excluded: 0` would have read as "we checked, there were none".
        "denominator": "all_distilled_sessions",
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--limit", type=int, default=5000, help="events to scan (default 5000)")
    ap.add_argument("--since", help="ISO date; drop events observed before it")
    ap.add_argument("--agent", help="report only this adapter")
    ap.add_argument(
        "--midpoint",
        action="store_true",
        help="the one-time progress gate (PRD §2): exit 3 below the floor, 4 when unreadable",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="machine-readable counts, so every consumer reads the verdict's own numbers",
    )
    args = ap.parse_args(argv)

    rows = fetch_events(args.url, args.limit)
    if rows is None:
        # Exit 2, distinct from "no events" (1) and "verdict printed" (0). A caller that cannot
        # tell an unreachable engine from an empty one reads absence as a measurement, which is
        # the failure this repo keeps finding in itself.
        if args.json:
            print(json.dumps({"reachable": False}, ensure_ascii=False))
        return 2

    # The owner kept the window and split the sample (PRD §8 D4). Rows written while the ledger
    # was pruned at 3 days measured a sample biased toward short sessions, so they are reported
    # but never read by the verdict — and they are reported rather than dropped, because hiding
    # them is the same move as quoting them.
    pre_rows, post_rows = partition_at_repair(rows)
    # The ceiling matters from 09-15: without it this counts events the pre-registered window
    # excludes while `peek` filters them out, and the two surfaces stop describing the same sample.
    pre_agent, _ = collect(
        pre_rows, since=args.since, agent=args.agent, until=verdict_core.WINDOW_UNTIL
    )
    per_agent, skipped_old = collect(
        post_rows, since=args.since, agent=args.agent, until=verdict_core.WINDOW_UNTIL
    )
    # `--json` keeps stdout to exactly one line. A caller that pipes this into a parser should not
    # have to strip advisory prose, and prose on stdout is how a machine-readable mode stops being
    # machine-readable without anybody noticing.
    note = sys.stderr if args.json else sys.stdout
    if skipped_old:
        print(
            f"[uptake-verdict] {skipped_old}건 제외 — per-prompt 대조가 없던 시기의 이벤트"
            " (0 으로 세면 우연율이 0 인 것처럼 읽힌다)",
            file=note,
        )
    # Ahead of the "no sample" early return on purpose. That branch answers the human question
    # ("is anything accruing?") with exit 1, and routing the gate through it made
    # MIDPOINT_UNREADABLE unreachable from the CLI — the function returned 4 and the command
    # returned 1, so the four-way distinction existed only in a unit test that called the function
    # directly. It also broke the out-of-window silence: before 09-08, an empty post-repair bucket
    # exited 1 and doctor warned every day about a gate that is not due.
    if args.midpoint:
        if args.json:
            print(
                "[uptake-verdict] --midpoint 와 --json 은 함께 못 쓴다 —"
                " 전자는 종료코드로, 후자는 페이로드로 답한다",
                file=sys.stderr,
            )
            return 2
        if args.agent:
            # The gate is per-adapter by contract (PRD §2). Filtering to one adapter would let
            # another adapter's shortfall pass unseen, which is the same arithmetic the per-adapter
            # rule exists to forbid — just arrived through a flag instead of a sum.
            print(
                "[uptake-verdict] --midpoint 는 --agent 로 좁힐 수 없다 —"
                " 게이트는 어댑터별이고, 거르면 다른 어댑터의 미달이 숨는다",
                file=sys.stderr,
            )
            return 2
        # `BORING_TODAY` exists so the gate can be exercised through the CLI on a date other
        # than today. A unit test that calls `_midpoint` directly cannot see the wiring, and the
        # wiring is exactly where this gate was broken.
        return _midpoint(per_agent, skipped_old)

    if not per_agent:
        # §2 turns "0 events" into an instrumentation investigation, but the clause it registers
        # is "세션 종료가 있는데 ... 0건" — sessions ended and nothing was recorded. Rows that were
        # skipped for predating `used_control_prompts` are proof the instrument fires; what is
        # young is the counter, not the measurement. Calling that a fault sends someone to debug
        # a hook that works, and worse, teaches them to distrust the clause when it is real.
        if skipped_old:
            print(
                f"[uptake-verdict] 아직 판정할 이벤트가 없다 — 계측은 돌고 있고"
                f"(옛 형식 {skipped_old}건) per-prompt 대조 카운터가 그보다 어리다."
                " 계측 결함이 아니라 표본이 쌓이는 중",
                file=note,
            )
            return 1
        if pre_agent:
            # Same trap the `skipped_old` branch above exists to avoid: an empty post-repair
            # bucket is not silence from the instrument, it is a boundary that everything is
            # still on the near side of. Calling it a fault sends someone to debug a hook that
            # works, and teaches them to distrust the clause when it finally is real.
            pre_sessions = sum(c["sessions"] for c in pre_agent.values())
            print(
                f"[uptake-verdict] 수리 이후 이벤트가 아직 없다 — 계측은 돌고 있고"
                f"(수리 이전 세션 {pre_sessions}건) 경계 {verdict_core.LEDGER_REPAIR_AT}"
                " 이후 표본이 쌓이는 중. 계측 결함이 아니다",
                file=note,
            )
            return 1
        print("[uptake-verdict] 집계할 injection_uptake 이벤트가 없다 — 계측 조사 대상(PRD §2)", file=note)
        return 1

    lost_sessions, lost_rows = unreported(rows)
    if lost_sessions:
        print(
            f"[uptake-verdict] 측정 안 된 주입: 세션 {lost_sessions} · 프롬프트 {lost_rows}"
            " — 종료되지 않고 사라진 세션. 판정에 합산하지 않는다(결과가 없으므로)",
            file=note,
        )

    if args.json:
        payload = {
            "reachable": True,
            "boundary": verdict_core.LEDGER_REPAIR_AT,
            "pre_repair": {
                who: {"sessions": c["sessions"], "total_prompts": c["total_prompts"]}
                for who, c in sorted(pre_agent.items())
            },
            "post_repair": {
                who: {
                    "sessions": c["sessions"],
                    "used_prompts": c["used_prompts"],
                    "total_prompts": c["total_prompts"],
                    "used_control_prompts": c["used_control_prompts"],
                }
                for who, c in sorted(per_agent.items())
            },
            "floors": {
                "sessions": verdict_core.MIN_SESSIONS,
                "prompts": verdict_core.MIN_INJECTED_PROMPTS,
            },
            # Injected into and never scored: the size of the blind spot, beside the rate. Without
            # it "3 sessions" reads as what we sent rather than what we saw.
            "unreported": {"sessions": lost_sessions, "prompts": lost_rows},
            "skipped_pre_counter": skipped_old,
            # §2 requires coverage beside the rate: below half, the number stops being a verdict
            # and becomes an instrument investigation. Reported raw here because this script
            # cannot read transcripts — the automated share that made the raw ratio read 14%
            # against 83% for people (§8 D8) has to come from a caller that can classify.
            "coverage": _coverage_payload(rows, args.since, verdict_core.WINDOW_UNTIL),
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if pre_agent:
        print(
            f"\n[수리 이전 · 판정에 쓰지 않음]  경계 {verdict_core.LEDGER_REPAIR_AT}"
        )
        for who, c in sorted(pre_agent.items()):
            used, total = c["used_prompts"], c["total_prompts"]
            print(
                f"  {who}: 세션 {c['sessions']} · 주입 프롬프트 {total} · 처치 {used}"
                f" · 대조 {c['used_control_prompts']}"
            )
        print(
            "  원장이 3일에 잘리던 시기다 — 3일보다 오래 산 세션은 채점 전에 증거가 사라졌고,"
            " 그 편향은 세션 길이 방향이다(PRD §8 D4). 비율을 내지 않는다."
        )

    # Probed once, not per adapter: the detector is one instrument and the ledger is one file.
    sensitive = uptake_core.detector_is_sensitive()
    # §2 puts coverage before the thresholds: a rate over a seventh of the channel is not a
    # smaller version of the right answer, it is an answer about a different population.
    cov = _coverage_payload(rows, args.since, verdict_core.WINDOW_UNTIL)
    for who, c in sorted(per_agent.items()):
        v = verdict_core.verdict(
            c["sessions"],
            c["used_prompts"],
            c["total_prompts"],
            c["used_control_prompts"],
            detector_sensitive=sensitive,
            coverage_ok=cov["clears_floor"],
        )
        print(f"\n[{who} · 수리 이후]")
        for line in verdict_core.format_verdict(v):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
