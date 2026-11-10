#!/usr/bin/env python3
"""Daily secretary check: one owner-calendar day of what the seats spent and did, against the week before.

Run by hand, once a day: `python scripts/secretary_daily.py --day 2026-11-09`. Writes
~/Documents/ohmyboring/secretary/<day>-daily.html (only the metrics that left their usual range) and
<day>-daily.json beside it (every value; weekly and monthly read these), and prints five lines.
No model call, no ledger write, nothing installed (contract wiki-00000021376 over 21370).

(가) tokens per model — usage.fold over usage's own index.
(나) claims closed that day ("닫힘:") — read from the ledger; per seat it cannot count (seat names are
     not normalised).
(다) cross-session messages received, the receiving side's tokens, share with no write after.
(라) test runs re-run with nothing changed since the previous run of the same command / runs executed.
(마) owner corrections — cannot count: no correction marker on a claim.
(다)(라) read only transcripts touched since the day began; their week baseline comes from earlier
daily JSONs, so until seven exist the comparison says 「아직」.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import html
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import usage  # noqa: E402  (also puts agents/shared on sys.path)
import verdict_core  # noqa: E402

OUT_DIR = Path.home() / "Documents" / "ohmyboring" / "secretary"
FRAME = Path.home() / "Documents" / "ohmyboring" / "global-recipe" / "report-paper-frame.html"
#: provisional, set by [omb/judge] in 21376: replaced by the weekday distribution after five days
HIGH, LOW = 2.0, 0.5
TOKEN_KEYS = usage.TOKEN_KEYS
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PEER = "<cross-session-message from="


def day_bounds(day):
    """[start, end) of a WINDOW_TZ day as UTC 'YYYY-MM-DDTHH:MM:SS' strings and the start epoch."""
    start = dt.datetime.fromisoformat(day).replace(tzinfo=verdict_core.WINDOW_TZ)
    end = start + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return start.astimezone(dt.UTC).strftime(fmt), end.astimezone(dt.UTC).strftime(fmt), start.timestamp()


def shift(day, n):
    return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()


# ---- (가) tokens per model ---------------------------------------------------------------------


def tokens_by_model(day):
    path = usage.index_path()
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = {}
    index, _ = usage.build_index(usage.transcript_files(), cached)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    os.replace(path + ".tmp", path)

    def per_model(since, until):
        out = collections.Counter()
        for key, counts in usage.fold(index, since=since, until=until).items():
            out[key.split("|")[1]] += sum(counts.get(k, 0) for k in TOKEN_KEYS)
        return out

    return per_model(day, day), per_model(shift(day, -7), shift(day, -1))


MODEL_FAMILIES = ("haiku", "sonnet", "opus", "fable")


def model_family(model_id):
    """claude-opus-5-5 → opus. An id outside the four families (<synthetic>) stays its own row."""
    return next((f for f in MODEL_FAMILIES if f in model_id), model_id)


def tokens_by_family(per_version):
    out = collections.Counter()
    for model_id, tokens in per_version.items():
        out[model_family(model_id)] += tokens
    return out


# ---- (나) closed claims ------------------------------------------------------------------------


def closed_claims(lo, hi):
    sql = (
        "select count(*), count(*) filter (where kind in ('next','decision')) from claim "
        f"where value like '닫힘:%' and valid_from >= '{lo}Z' and valid_from < '{hi}Z'"
    )
    out = subprocess.run(
        ["docker", "exec", "boring-postgres", "psql", "-U", "boring", "-d", "boring", "-Atc", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    total, nd = out.stdout.strip().split("|")
    return int(total), int(nd)


# ---- (다)(라) transcript scan (stage 0 scan.py, one day) ----------------------------------------


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, dict) and b.get("type") == "tool_result":
            parts.append(text_of(b.get("content")))
    return "\n".join(parts)


def split_pieces(cmd):
    """Shell command → simple-command pieces, split on ; newline | ( ) && || $( outside quotes."""
    pieces, cur, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"' and i + 1 < len(cmd):
                i += 1
                cur.append(cmd[i])
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            cur.append(ch)
            i += 1
            cur.append(cmd[i])
        elif (
            ch in ";\n|()"
            or (ch == "&" and cmd[i + 1 : i + 2] == "&")
            or (ch == "$" and cmd[i + 1 : i + 2] == "(")
        ):
            pieces.append("".join(cur))
            cur = []
            if cmd[i + 1 : i + 2] in ("|", "&", "(") and ch in "|&$":
                i += 1
        else:
            cur.append(ch)
        i += 1
    pieces.append("".join(cur))
    return [p.strip() for p in pieces if p.strip()]


def tokens(piece):
    try:
        words = shlex.split(piece)
    except ValueError:
        words = piece.split()
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif re.match(r"^\d*>>?$|^&>$", w):
            skip = True
        elif not (re.match(r"^\d*>>?&?\S*$|^&>\S+$", w) and w[0] in "2&>0123456789"):
            out.append(w)
    return out


def strip_head(words):
    i = 0
    while i < len(words):
        w = words[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or w in (
            "time",
            "env",
            "nohup",
            "command",
            "nice",
            "npx",
            "bunx",
        ):
            i += 1
        elif w == "timeout":
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.match(r"^\d+[smhd]?$", words[i])):
                i += 1
        elif w == "uv" and words[i + 1 : i + 2] == ["run"]:
            i += 2
            while i < len(words) and words[i].startswith("-"):
                i += 1
        else:
            break
    return words[i:]


def test_kind(words):
    """(runner, positional args) when the piece runs tests, else None."""
    words = strip_head(words)
    if not words:
        return None
    head, args = os.path.basename(words[0]), words[1:]
    if any(a in ("--version", "--help") for a in args):
        return None
    kind, rest = None, args
    if head in ("pytest", "py.test"):
        kind = "pytest"
    elif re.match(r"^python[\d.]*$", head) and args[:1] == ["-m"] and args[1:2] in (["pytest"], ["unittest"]):
        kind, rest = args[1], args[2:]
    elif head == "make" and any(a in ("test", "check") for a in args if not a.startswith("-")):
        kind = "make " + next(a for a in args if a in ("test", "check"))
    elif head == "bun" and args[:1] == ["test"]:
        kind, rest = "bun test", args[1:]
    elif head == "bun" and args[:1] == ["run"] and args[1:2] and args[1].startswith("test"):
        kind, rest = "bun run test", args[1:]
    elif head == "vitest":
        kind = "vitest"
    elif head == "sbt" and any(a == "test" or a.startswith("test") or a.endswith("/test") for a in args):
        kind = "sbt"
    if kind is None:
        return None
    return kind, tuple(a for a in rest if not a.startswith("-"))


def is_change(words):
    words = strip_head(words)
    if not words:
        return False
    head, args = os.path.basename(words[0]), words[1:]
    first = args[0] if args else ""
    return (
        head in ("sd", "cp", "mv", "tee", "black", "prettier", "biome", "gofmt")
        or (head == "sed" and any(a.startswith("-i") for a in args))
        or (head == "git" and first in ("checkout", "rebase", "pull", "reset", "restore", "switch"))
        or (head == "git" and first == "stash" and args[1:2] not in (["list"], ["show"]))
        or (head == "cat" and any(w.startswith(">") for w in words))
        or (head in ("bun", "uv") and first in ("install", "i", "add", "sync"))
        or (head == "ruff" and first == "format")
    )


#: `> file` / `>> file` that writes somewhere other than /dev/null or another descriptor
FILE_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(?!/dev/null|&)[^\s>&|;]")
FAIL = re.compile(r"\b[1-9]\d* (failed|fail|errors?)\b|FAILED |\(fail\)|make: \*\*\*|\[error\]")
PASS = re.compile(r"\b\d+ passed|\b0 fail|\bOK\b|\[success\]")


def verdict_of(text):
    if text is None:
        return "unknown"
    if "PreToolUse:Bash hook error" in text or "BLOCKED:" in text:
        return "blocked"
    if FAIL.search(text):
        return "fail"
    if PASS.search(text):
        return "pass"
    return "unknown"


def read_rows(path):
    """(seat title, user/assistant/attachment rows) of one transcript, JSON-parsed."""
    rows, seat = [], None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return seat, rows
    with fh:
        for line in fh:
            if '"customTitle"' in line or '"agentName"' in line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                seat = r.get("customTitle") or r.get("agentName") or seat
            elif '"type":"user"' in line or '"type":"assistant"' in line or '"queued_command"' in line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return seat, rows


def scan_transcript(path, lo, hi):
    """One transcript → (peer messages received in [lo, hi), test runs in [lo, hi))."""
    seat, rows = read_rows(path)
    inday = lambda ts: lo <= ts[:19] < hi  # noqa: E731
    if not any(inday(r.get("timestamp", "")) for r in rows):
        return [], []
    sub = "subagents" in Path(path).parts
    seat = seat or ("subagent" if sub else "cwd:" + os.path.basename(os.path.dirname(str(path))))
    replies, order, results = {}, [], {}
    for r in rows:
        kind = r.get("type")
        if kind == "assistant":
            m = r.get("message") or {}
            key = m.get("id") or r.get("requestId") or r.get("uuid")
            if key not in replies:
                replies[key] = {"ts": r.get("timestamp", ""), "usage": None, "uses": {}}
                order.append(("A", key))
            if m.get("usage"):
                replies[key]["usage"] = m["usage"]
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    replies[key]["uses"][b.get("id")] = (
                        b.get("name"),
                        b.get("input") or {},
                        r.get("timestamp", ""),
                    )
        elif kind == "user":
            c = (r.get("message") or {}).get("content")
            if (
                isinstance(c, list)
                and c
                and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
            ):
                for b in c:
                    results[b.get("tool_use_id")] = (r.get("timestamp", ""), text_of(b.get("content")))
            else:
                order.append(("U", r))

    peers, seg = [], None
    for kind, x in order:
        if kind == "U":
            text = text_of((x.get("message") or {}).get("content"))
            seg = {"ts": x.get("timestamp", ""), "tok": 0, "turns": 0, "acts": 0, "peer": None}
            if PEER in text:
                m = re.search(r'from-name="([^"]*)"', text)
                seg["peer"] = {
                    "from": m.group(1) if m else "?",
                    "to": seat,
                    "uuid": x.get("uuid"),
                    "file": str(path),
                }
                seg["key"] = hashlib.md5(
                    re.sub(r"\s+", "", text.split("<cross-session-message")[1][:300]).encode()
                ).hexdigest()
            peers.append(seg)
        elif seg is not None:
            reply = replies[x]
            seg["tok"] += sum((reply["usage"] or {}).get(k) or 0 for k in TOKEN_KEYS)
            seg["turns"] += 1
            for name, inp, _ in reply["uses"].values():
                if (
                    name == "mcp__ohmyboring__remember"
                    or name in WRITE_TOOLS
                    or (name == "Bash" and "git commit" in (inp.get("command") or ""))
                ):
                    seg["acts"] += 1
    received = [
        {**s["peer"], "tok": s["tok"], "turns": s["turns"], "acts": s["acts"]}
        for s in peers
        if s["peer"] and inday(s["ts"])
    ]

    runs, changes, last = [], 0, {}
    uses = sorted(
        (
            (ts, tid, name, inp)
            for reply in replies.values()
            for tid, (name, inp, ts) in reply["uses"].items()
        ),
        key=lambda u: u[0],
    )
    for ts, tid, name, inp in uses:
        if name in WRITE_TOOLS:
            changes += 1
            continue
        if name != "Bash":
            continue
        result = results.get(tid)
        verdict = verdict_of(result[1] if result else None)
        # walk the pieces in order: a `sed -i` or `> file` between two test pieces of one command is a
        # change too (hand check 21376: 2 of 5 reruns were false because only calls were compared)
        for raw in split_pieces(inp.get("command") or ""):
            words = tokens(raw)
            k = test_kind(words)
            if k is None:
                changes += is_change(words) or bool(FILE_REDIRECT.search(raw))
                continue
            unchanged = False
            if verdict != "blocked":
                unchanged = last.get(k) == changes
                last[k] = changes
            if inday(ts):
                runs.append(
                    {
                        "seat": seat,
                        "kind": k[0],
                        "verdict": verdict,
                        "unchanged": unchanged,
                        "tid": tid,
                        "file": str(path),
                    }
                )
    return received, runs


def scan_day(lo, hi, start_epoch):
    files = [p for p in usage.transcript_files() if os.path.getmtime(p) >= start_epoch]
    received, runs = [], []
    for p in files:
        r, t = scan_transcript(p, lo, hi)
        received += r
        runs += t
    return files, received, runs


# ---- compare, write ----------------------------------------------------------------------------


def prior_values(day):
    """{metric: [values]} from the daily JSONs of the seven days before."""
    out = collections.defaultdict(list)
    for k in range(1, 8):
        try:
            prev = json.loads((OUT_DIR / f"{shift(day, -k)}-daily.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, value in prev["metrics"].items():
            out[name].append(value)
    return out


def judge(value, baseline):
    """('벗어남'|'평소'|'아직', ratio) against the day mean of the baseline."""
    if baseline is None or len(baseline) < 7:
        return "아직", None
    mean = sum(baseline) / 7
    if mean == 0:
        return ("벗어남" if value else "평소"), None
    ratio = value / mean
    return ("벗어남" if ratio > HIGH or ratio < LOW else "평소"), round(ratio, 2)


def build_page(day, rows, notes, meta):
    out_rows = [r for r in rows if r["state"] == "벗어남"]
    waiting = [r for r in rows if r["state"] == "아직"]
    e = html.escape
    if out_rows:
        body = "".join(
            f"<tr><td class='id'>{e(r['name'])}</td><td>{r['value']:,}</td><td>{r['mean']:,}</td><td>{r['ratio']}</td></tr>"
            for r in out_rows
        )
        table = (
            "<table><caption><b>표 1.</b> 벗어난 지표 — 그날 값 / 직전 7일 하루 평균이 "
            f"{HIGH} 초과 또는 {LOW} 미만</caption><thead><tr><th>지표</th><th>그날</th>"
            f"<th>7일 평균</th><th>배수</th></tr></thead><tbody>{body}</tbody></table>"
        )
        thesis = f"벗어남 {len(out_rows)} / 비교한 지표 {len(rows) - len(waiting)}"
    else:
        table = "<p>벗어남 없음.</p>"
        thesis = "벗어남 없음"
    wait = "".join(f"<li><b>{e(r['name'])}</b><span>아직 — 표본 {r['n']}/7일</span></li>" for r in waiting)
    cant = "".join(f"<li><b>{e(k)}</b><span>{e(v)}</span></li>" for k, v in notes)
    page = FRAME.read_text(encoding="utf-8")
    swaps = [
        ("<title>제목</title>", f"<title>비서 일간 점검 {day}</title>"),
        ("제품·주제<small>부제</small>", "ohmyboring<small>비서 점검</small>"),
        ("<span>문서 종류 · 날짜 · 상태</span>", f"<span>일간 · {day} · 잠정 문턱</span>"),
        ("Report · 상태", "Daily · 잠정 문턱"),
        ("제목(물음 꼴)", f"{day} 에 평소에서 벗어난 것은?"),
        ("English subtitle", "Daily secretary check"),
        ("작성 자리", "scripts/secretary_daily.py"),
        ("<dd>날짜</dd>", f"<dd>{day} (WINDOW_TZ)</dd>"),
        ("근거(버전·SHA·원문 수)", f"전사 {meta['files']} · {meta['secs']}s"),
        (
            "이 문서가 무엇의 짝인지, 출처를 어떻게 확인했는지.",
            "계약 wiki-00000021376. 전체 값은 같은 폴더 JSON. 모델 0 · 장부 쓰기 0.",
        ),
        ("결론과 그 결론이 기대는 수(분모와 함께).", "맨 위는 벗어난 지표만. 독자는 [omb/judge]."),
        ("한 문장 결론.", e(thesis)),
    ]
    for old, new in swaps:
        page = page.replace(old, new, 1)
    start, end = page.index("  <!-- 1 -->"), page.index("  <footer")
    section = (
        f"  <h2><span class='num'>1</span>벗어남</h2>\n  {table}\n"
        f"  <h2><span class='num'>2</span>아직 · 못 셈</h2>\n  <ol class='nongoals'>{wait}{cant}</ol>\n"
    )
    page = page[:start] + section + page[end:]
    page = page.replace(
        "제품·주제</span> · 문서 종류 · 날짜 · 저자", f"ohmyboring</span> · 일간 점검 · {day}", 1
    )
    return page.replace("basis: … · status: …", f"basis: 문턱 {HIGH}/{LOW} 잠정 · status: 첫 판", 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="비서 일간 점검 (모델 0, 장부 쓰기 0)")
    ap.add_argument("--day", required=True, help="WINDOW_TZ (UTC+9) date, e.g. 2026-11-09")
    args = ap.parse_args(argv)
    day = args.day
    t0 = time.monotonic()
    lo, hi, start_epoch = day_bounds(day)

    today_by_version, week_by_version = tokens_by_model(day)
    # compared by family: a version bump (opus-5 → opus-5-5) is not a deviation (21432)
    today, week = tokens_by_family(today_by_version), tokens_by_family(week_by_version)
    closed, closed_nd = closed_claims(lo, hi)
    week_closed = [closed_claims(*day_bounds(shift(day, -k))[:2])[0] for k in range(1, 8)]
    files, received, runs = scan_day(lo, hi, start_epoch)

    executed = [r for r in runs if r["verdict"] != "blocked"]
    reruns = [r for r in executed if r["unchanged"]]
    no_write = [m for m in received if m["acts"] == 0]
    # a model used all week and absent today is a 0 that must reach the comparison
    models = sorted(set(today) | set(week))
    metrics = {
        "(가) 토큰 합": sum(today.values()),
        **{f"(가) 토큰 {m}": today.get(m, 0) for m in models},
        "(나) 닫힘 claim": closed,
        "(다) 받은 메시지": len(received),
        "(다) 쓰기 0 메시지": len(no_write),
        "(라) 실행한 시험": len(executed),
        "(라) 변경 없는 재실행": len(reruns),
    }
    prior = prior_values(day)
    baselines = {
        "(가) 토큰 합": [sum(week.values()) / 7] * 7,
        **{f"(가) 토큰 {m}": [week.get(m, 0) / 7] * 7 for m in models},
        "(나) 닫힘 claim": week_closed,
    }
    rows = []
    for name, value in metrics.items():
        base = baselines.get(name, prior.get(name))
        state, ratio = judge(value, base)
        mean = round(sum(base) / 7) if base and len(base) >= 7 else None
        rows.append(
            {"name": name, "value": value, "mean": mean, "ratio": ratio, "state": state, "n": len(base or [])}
        )
    secs = round(time.monotonic() - t0, 1)
    detail = {
        "(가) 판 단위 토큰": dict(sorted(today_by_version.items())),
        "(나) next·decision": closed_nd,
        "(다) 받는 쪽 토큰 중앙값": int(statistics.median([m["tok"] for m in received]))
        if received
        else None,
        "(라) 재실행 중 실패": sum(1 for r in reruns if r["verdict"] == "fail"),
    }
    notes = [
        ("(나) 자리별", "못 셈 — 자리 이름 정규화 없음"),
        ("(마) 소유자 교정", "못 셈 — claim 단위 교정 표지 없음"),
        ("(라) 재실행", "하한 — 시험 명령 인식이 규칙 목록 기반"),
    ]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"files": len(files), "secs": secs, "lo": lo, "hi": hi}
    record = {
        "day": day,
        "meta": meta,
        "metrics": metrics,
        "detail": detail,
        "rows": rows,
        "notes": notes,
        "received": received,
        "reruns": [{k: r[k] for k in ("seat", "kind", "verdict", "tid", "file")} for r in reruns],
    }
    (OUT_DIR / f"{day}-daily.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    page = OUT_DIR / f"{day}-daily.html"
    page.write_text(build_page(day, rows, notes, meta), encoding="utf-8")

    out_rows = [r for r in rows if r["state"] == "벗어남"]
    print(f"[secretary] {day} · 전사 {len(files)} · {secs}s · 벗어남 {len(out_rows)} / 지표 {len(rows)}")
    print(
        f"[secretary] (가) 토큰 합 {metrics['(가) 토큰 합']:,} · 계열 {len(today)} (판 {len(today_by_version)})"
    )
    print(f"[secretary] (나) 닫힘 {closed} (next·decision {closed_nd}) · (마) 못 셈")
    print(
        f"[secretary] (다) 받은 메시지 {len(received)} · 쓰기 0 {len(no_write)} · (라) 재실행 {len(reruns)} / 실행 {len(executed)}"
    )
    print(f"[secretary] {page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
