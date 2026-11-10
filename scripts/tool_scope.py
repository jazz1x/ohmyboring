#!/usr/bin/env python3
"""Which tools leak: per on/off unit, what it costs to carry and how often it is called (C-TOOLLEAK1).

A unit is what can be switched on or off: one built-in tool, one MCP server, one claude.ai
connector, one plugin MCP, one skill (a plugin's skills are one unit), one hook. Datadog (2)'s
~300 tool names are one row.

The engine counts and picks each row's scope by rule A; the owner changes settings. Nothing here
writes a setting. A model step (gemma4:12b) was tried and removed: on the 318 rows with 0 calls it
answered something other than 끔 on 188, contradicting the numbers it cited (judge 21027).

- calls: assistant `tool_use` blocks, one per id. A line regex counts the same id twice
  (Datadog (2): 14 by regex vs 10 by id, 11-10), so rows are JSON-parsed.
- session: a main transcript; its `subagents/` files fold into it.
- profile: the orca profile whose `claude-profiles/<id>/home/CLAUDE.md` the session loaded. Three
  profiles share `~/.claude/projects`, so the directory cannot tell them apart; sessions that never
  name one are `-` (the default ~/.claude). Profiles with their own projects dir are added.
- carried load: from each session's first request — the tool definitions sent in front, the
  deferred tool names, the skill list, hook context — as tokens estimated by character share of
  that request's input tokens (the 20791 method). Medians over the sessions where the unit was on.
- rule A (fixed by the judge, 20902, before hermes ran): 0 calls → 끔 · repos ≥ 6 → 유저 범위 ·
  repos 4–5 → 판별 못 함 · session share < 1% → 필요할 때만 · else (repos 1–3, ≥ 1%) → 프로젝트 범위.
  Hooks have no calls to count, so no rule scope.

`table` prints the table as TSV. With no arguments it is the monthly run (contract 21265):
- keep: each profile's verdicts go to the ledger as claims `tool-scope:<profile>:<unit>` / `scope`
  (project tool-scope, so the session-start card stays clear), superseding the previous run's note;
  a unit missing from this run is written `없어짐 — 마지막 관찰 <date>`.
- use: candidates = 끔·필요할 때만 rows that are on now, by carried tokens, as JSON and an HTML page.
- refine: against the previous run's JSON — prior_read (rows read), wrong_off (끔 that got calls since)
  and the number of new sessions, so a flip from more transcripts reads apart from a wrong rule.
Run the engine directly; run-tool-scope.py goes through card_runner, which needs the Slack token.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import usage  # noqa: E402  (also puts agents/shared and src on sys.path)
import verdict_core  # noqa: E402

from ohmyboring.adapters.engine import DrudgeClient  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402

PROFILES_DIR = Path(
    os.environ.get("BORING_CLAUDE_PROFILES_DIR")
    or Path.home() / "Library" / "Application Support" / "orca" / "claude-profiles"
)
DEFAULT_PROFILES = ("a0faa78e", "fe5c62d7", "791c085b")

_PROF = re.compile(r"claude-profiles/([0-9a-f]{8})[0-9a-f-]*/home/CLAUDE\.md")
_CWD = re.compile(r'"cwd":"([^"]+)"')
_TS = re.compile(r'"timestamp":"([^"]{19})')
_DEFER_HEAD = "deferred tools are now available"
_SKILL_HEAD = "The following skills are available for use with the Skill tool:"
_HOOK_SEG = re.compile(r"(?:^|\n)\s*(\w+)(?::[^\s]*)? hook additional context")
#: a hook's context runs until the next block of the request starts (base.py's markers), not to the end
_SPLIT = re.compile(
    r"(?=<system-reminder>)|(?=\n\w+(?::[^\s]*)? hook additional context)"
    r"|(?=The following skills are available)|(?=The following deferred tools)|(?=Available agent types)"
    r"|(?=# MCP Server Instructions)|(?=Contents of )|(?=# Environment)|(?=\n\n📚 )|(?=\n\n## 오늘 승인한 것)"
)


def index_path():
    return os.environ.get("BORING_TOOL_SCOPE_INDEX") or os.path.expanduser(
        "~/.cache/boring-distill/tool-scope-index.json"
    )


def out_dir():
    return Path(os.environ.get("BORING_TOOL_SCOPE_OUT") or html_dir() / "runs")


def transcript_files():
    """usage.transcript_files plus each profile's projects dir that is not one already read."""
    files = usage.transcript_files()
    seen_roots = {
        p.resolve() for d in usage.boring_config.source_dirs(adapter="session-end") or [] for p in [Path(d)]
    }
    seen_roots.add((Path.home() / ".claude" / "projects").resolve())
    for proj in sorted(PROFILES_DIR.glob("*/home/projects")):
        root = proj.resolve()
        if root in seen_roots or not root.is_dir():
            continue
        seen_roots.add(root)
        files.extend(sorted(p for p in root.rglob("*.jsonl") if p.is_file()))
    return files


def session_of(path):
    parts = path.parts
    if "subagents" in parts:
        return str(Path(*parts[: parts.index("subagents")]))
    return str(path.with_suffix(""))


def profile_of_path(path):
    """A profile with its own projects dir names itself in the path."""
    m = re.search(r"claude-profiles/([0-9a-f]{8})[0-9a-f-]*/home/projects/", str(path))
    return m.group(1) if m else ""


def unit_of(name, skill=""):
    if name == "Skill" and skill:
        return "skill:" + skill.split(":")[0]
    for pre, tag in (("mcp__claude_ai_", "connector:"), ("mcp__plugin_", "plugin-mcp:"), ("mcp__", "mcp:")):
        if name.startswith(pre):
            return tag + name[len(pre) :].split("__")[0]
    return "builtin:" + name


def _blob_text(row):
    message = row.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") if b.get("type") == "text" else "" for b in content if isinstance(b, dict)
    )


def _deferred_names(text):
    i = text.find(_DEFER_HEAD)
    if i < 0:
        return []
    names = []
    for line in text[i:].split("\n")[1:]:
        token = line.strip()
        if not token or " " in token or token.startswith("<"):
            if names:
                break
            continue
        names.append(token)
    return names


def _skill_lines(text):
    i = text.find(_SKILL_HEAD)
    if i < 0:
        return []
    out = []
    for line in text[i + len(_SKILL_HEAD) :].split("\n"):
        if line.startswith("- "):
            name = line[2:].split(": ", 1)[0].strip()
            out.append((name, len(line)))
        elif line.strip() and out:
            break
    return out


def first_request_load(shape, blobs, input_tokens):
    """{unit: [front_tok, deferred_tok]} and {hook_event: tok} for one session's first request."""
    chars = collections.defaultdict(lambda: [0, 0])
    hooks = collections.Counter()
    total = len(json.dumps(shape.get("system", ""), ensure_ascii=False))
    for tool in shape.get("tools") or []:
        n = len(json.dumps(tool, ensure_ascii=False))
        chars[unit_of(str(tool.get("name", "")))][0] += n
        total += n
    for text in blobs:
        total += len(text)
        for name in _deferred_names(text):
            chars[unit_of(name)][1] += len(name) + 1
        for name, n in _skill_lines(text):
            chars["skill:" + name.split(":")[0]][0] += n
        for seg in _SPLIT.split(text):
            m = _HOOK_SEG.match(seg) or _HOOK_SEG.search(seg[:200])
            if m:
                hooks[m.group(1)] += len(seg)
    if not total or not input_tokens:
        return {}, {}
    ratio = input_tokens / total
    return (
        {u: [round(a * ratio), round(b * ratio)] for u, (a, b) in chars.items()},
        {e: round(n * ratio) for e, n in hooks.items()},
    )


def scan_file(path):
    """One transcript → its session's facts: profile, cwd, time span, calls per unit, first-request load."""
    out = {
        "session": session_of(path),
        "main": "subagents" not in path.parts,
        "prof": profile_of_path(path),
        "cwd": "",
        "first": "",
        "last": "",
        "calls": {},
        "load": None,
        "hooks": None,
    }
    calls = collections.Counter()
    seen = set()
    shape, blobs, done_first = None, [], False
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            if not out["prof"] and "claude-profiles/" in line:
                m = _PROF.search(line)
                if m:
                    out["prof"] = m.group(1)
            if not out["cwd"] and '"cwd":"' in line:
                m = _CWD.search(line)
                if m:
                    out["cwd"] = m.group(1)
            if '"timestamp":"' in line:
                m = _TS.search(line)
                if m:
                    out["first"] = out["first"] or m.group(1)
                    out["last"] = max(out["last"], m.group(1))
            needs_json = '"tool_use"' in line or (
                not done_first
                and ('"api-request-shape"' in line or '"api-request-blob"' in line or '"usage"' in line)
            )
            if not needs_json:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            kind = row.get("type")
            if kind == "api-request-shape" and shape is None:
                shape = row.get("shape") or {}
            elif kind == "api-request-blob" and not done_first:
                blobs.append(_blob_text(row))
            elif kind == "assistant":
                message = row.get("message") or {}
                if not done_first and shape is not None and isinstance(message.get("usage"), dict):
                    u = message["usage"]
                    tokens = sum(
                        int(u.get(k) or 0)
                        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                    )
                    if out["main"]:
                        out["load"], out["hooks"] = first_request_load(shape, blobs, tokens)
                    done_first = True
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict) or c.get("type") != "tool_use" or c.get("id") in seen:
                        continue
                    seen.add(c.get("id"))
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    calls[
                        unit_of(
                            str(c.get("name")), str(inp.get("skill", "")) if c.get("name") == "Skill" else ""
                        )
                    ] += 1
    out["calls"] = dict(calls)
    return out


def build_index(files, cached):
    index, scanned = {}, 0
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        prior = cached.get(str(path))
        if prior and prior.get("stamp") == stamp:
            index[str(path)] = prior
            continue
        index[str(path)] = {"stamp": stamp, **scan_file(path)}
        scanned += 1
    return index, scanned


def sessions_from(index):
    """Fold file entries into sessions; a subagent's profile/cwd only fills what the main left empty."""
    sess = {}
    for entry in sorted(index.values(), key=lambda e: not e["main"]):
        s = sess.setdefault(
            entry["session"],
            {
                "prof": "",
                "cwd": "",
                "first": "",
                "last": "",
                "calls": collections.Counter(),
                "load": None,
                "hooks": None,
            },
        )
        s["prof"] = s["prof"] or entry["prof"]
        s["cwd"] = s["cwd"] or entry["cwd"]
        if entry["first"]:
            s["first"] = min(filter(None, (s["first"], entry["first"])))
        s["last"] = max(s["last"], entry["last"])
        s["calls"].update(entry["calls"])
        if entry["main"] and entry["load"] is not None:
            s["load"], s["hooks"] = entry["load"], entry["hooks"]
    for s in sess.values():
        s["prof"] = s["prof"] or "-"
    return sess


def settings_path(prof):
    if prof == "-":
        return Path.home() / ".claude" / "settings.json"
    hits = sorted(PROFILES_DIR.glob(f"{prof}*/home/settings.json"))
    return hits[0] if hits else None


def hook_units(prof):
    """(unit, event) for each hook in the profile's settings.json :hooks."""
    path = settings_path(prof)
    if path is None or not path.is_file():
        return []
    hooks = (json.loads(path.read_text(encoding="utf-8")).get("hooks")) or {}
    out, seen = [], collections.Counter()
    for event, groups in hooks.items():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = str(h.get("command") or h.get("prompt") or "")
                script = re.search(r"([\w.-]+\.(?:sh|py))\b", cmd)
                label = (
                    script.group(1)
                    if script
                    else (
                        "orca-agent-hook"
                        if "agent-hooks" in cmd or "OSTYPE" in cmd
                        else cmd.split()[0]
                        if cmd
                        else "?"
                    )
                )
                matcher = g.get("matcher") or ""
                unit = f"hook:{event}{'[' + matcher + ']' if matcher else ''}:{label}"
                seen[unit] += 1
                out.append((unit if seen[unit] == 1 else f"{unit}#{seen[unit]}", event))
    return out


#: where a switch lives, how far it reaches, and how to turn it back on — by unit kind.
REACH = {
    "builtin": ("프로필", "settings.json permissions.deny 에서 빼면 다시 켜짐"),
    "mcp": (
        "프로필 또는 저장소",
        ".claude.json mcpServers / projects.<경로>.disabledMcpServers 에서 빼면 다시 켜짐",
    ),
    "connector": ("계정 전체", "claude.ai 설정 > 커넥터에서 다시 연결"),
    "plugin-mcp": ("프로필", "settings.json enabledPlugins 에서 true 로"),
    "skill": ("프로필", "플러그인 스킬은 enabledPlugins true, 사용자 스킬은 skills 폴더에 되돌림"),
    "hook": ("프로필", "settings.json hooks 에 그 항목을 되돌림"),
}


def rule_a(calls, repos, share_pct, kind):
    if kind == "hook":
        return "-"
    if calls == 0:
        return "끔"
    if repos >= 6:
        return "유저 범위"
    if repos >= 4:
        return "판별 못 함"
    if share_pct < 1.0:
        return "필요할 때만"
    return "프로젝트 범위"


def build_table(sess, profiles):
    """Rows per (profile, unit): called units ∪ units loaded in the first request ∪ hooks."""
    rows = []
    for prof in profiles:
        mine = {k: s for k, s in sess.items() if s["prof"] == prof}
        den = len(mine)
        if not den:
            continue
        span = (min(s["first"] for s in mine.values() if s["first"]), max(s["last"] for s in mine.values()))
        calls, sessions, repos = collections.Counter(), collections.Counter(), collections.defaultdict(set)
        front, deferred, enabled = (
            collections.defaultdict(list),
            collections.defaultdict(list),
            collections.Counter(),
        )
        hook_tok = collections.defaultdict(list)
        first_on = {}
        loaded = [s for s in mine.values() if s["load"] and s["first"]]
        latest = max(loaded, key=lambda s: s["first"])["load"] if loaded else {}
        for s in mine.values():
            for u in s["load"] or {}:
                if s["first"]:
                    first_on[u] = min(filter(None, (first_on.get(u), s["first"])))
            repo = usage._repo_of(s["cwd"]) or "-"
            for u, n in s["calls"].items():
                calls[u] += n
                sessions[u] += 1
                repos[u].add(repo)
            for u, (a, b) in (s["load"] or {}).items():
                enabled[u] += 1
                front[u].append(a)
                deferred[u].append(b)
            for e, t in (s["hooks"] or {}).items():
                hook_tok[e].append(t)
        units = sorted(set(calls) | set(enabled))
        hooks = hook_units(prof)
        per_event = collections.Counter(e for _, e in hooks)
        for u in units + [h for h, _ in hooks]:
            kind = u.split(":", 1)[0]
            event = dict(hooks).get(u) if kind == "hook" else None
            share = round(100 * sessions[u] / den, 2)
            reach, reenable = REACH[kind]
            row = {
                "id": f"{prof}|{u}",
                "profile": prof,
                "unit": u,
                "kind": kind,
                "calls": calls[u] if kind != "hook" else None,
                "sessions": sessions[u] if kind != "hook" else None,
                "repos": len(repos[u]) if kind != "hook" else None,
                "den": den,
                "window": f"{span[0][:10]}~{span[1][:10]}",
                "share_pct": share if kind != "hook" else None,
                "enabled_sessions": enabled[u] if kind != "hook" else len(hook_tok.get(event, [])),
                "front_tok": int(statistics.median(front[u])) if front[u] else 0,
                "deferred_tok": int(statistics.median(deferred[u])) if deferred[u] else 0,
                "reach": reach,
                "reenable": reenable,
                # transcript stamps are UTC cut to 19 chars (_TS); the day is the owner's
                "first_on": verdict_core.window_day(first_on[u] + "Z") if u in first_on else "",
                # on now = in the first request of the profile's newest session that recorded one
                "on_now": u in latest,
            }
            if kind == "hook":
                tok = hook_tok.get(event, [])
                row["front_tok"] = int(statistics.median(tok)) if tok else 0
                row["note"] = (
                    f"호출 셀 수 없음 · 싣는 값은 같은 이벤트 훅 {per_event[event]}개 합(첫 요청) · "
                    "머리 없이 붙는 주입(📚 Project context 등)은 어느 훅인지 몰라 빠짐"
                )
            row["rule_a"] = rule_a(row["calls"] or 0, row["repos"] or 0, row["share_pct"] or 0.0, kind)
            rows.append(row)
    return rows


# ---- use, keep, refine (C-TOOLLEAK1 3단계, 계약 21265 over 21252) ---------------------------

CANDIDATE_SCOPES = ("끔", "필요할 때만")


def html_dir():
    return Path(
        os.environ.get("BORING_TOOL_SCOPE_HTML_DIR")
        or Path.home() / "Documents" / "ohmyboring" / "tool-scope"
    )


def read_prior(out):
    """The previous run: the newest tool-scope-<ts>.json in out_dir and when it was written; None on a first run."""
    files = sorted(out.glob("tool-scope-*.json"))
    if not files:
        return None, ""
    newest = files[-1]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(newest.stat().st_mtime))
    return json.loads(newest.read_text(encoding="utf-8")), stamp


def judged(table):
    return [r for r in table if r["kind"] != "hook"]


def compare(table, prior, prior_at, sess, profiles):
    """What the previous run's verdicts say about this run: rows read, 끔 that got called, units gone."""
    if prior is None:
        return {
            "prior_read": 0,
            "first_run": True,
            "wrong_off": [],
            "prior_off": 0,
            "new_sessions": 0,
            "gone": [],
        }
    now = {r["id"]: r for r in table}
    before = judged(prior["table"])
    prior_off = [r for r in before if r["rule_a"] == "끔"]
    wrong = [r["id"] for r in prior_off if (now.get(r["id"]) or {}).get("calls")]
    gone = [r for r in before if r["id"] not in now]
    new_sessions = sum(1 for s in sess.values() if s["prof"] in profiles and s["first"] > prior_at)
    return {
        "prior_read": len(before),
        "first_run": False,
        "wrong_off": wrong,
        "prior_off": len(prior_off),
        "new_sessions": new_sessions,
        "gone": [
            {"id": r["id"], "profile": r["profile"], "unit": r["unit"], "last_seen": r["window"][-10:]}
            for r in gone
        ],
    }


def scope_value(r):
    tok = r["front_tok"] + r["deferred_tok"]
    return (
        f"{r['rule_a']} — 호출 {r['calls']} · 저장소 {r['repos']} · 세션 {r['sessions']}/{r['den']} · "
        f"토큰 {tok} · 켜진 세션 {r['enabled_sessions']} · 첫 켜짐 {r['first_on'] or '-'}"
    )


def claims_by_profile(table, gone):
    out = collections.defaultdict(list)
    for r in judged(table):
        out[r["profile"]].append(
            {
                "subject": f"tool-scope:{r['profile']}:{r['unit']}",
                "predicate": "scope",
                "value": scope_value(r),
                "kind": "fact",
            }
        )
    for g in gone:
        out[g["profile"]].append(
            {
                "subject": f"tool-scope:{g['profile']}:{g['unit']}",
                "predicate": "scope",
                "value": f"없어짐 — 마지막 관찰 {g['last_seen']}",
                "kind": "fact",
            }
        )
    return out


def remember_scopes(claims, prior_notes, stamp, client):
    """One note per profile on the remember path; each names the previous run's notes for that profile
    as superseded (that also takes it past the duplicate gate). Returns {profile: source_path} or the failure."""
    notes = {}
    for prof, rows in sorted(claims.items()):
        scopes = collections.Counter(c["value"].split(" — ")[0] for c in rows)
        payload = {
            "title": f"도구 범위 판정 {prof} {stamp}",
            "body": (
                f"tool_scope.py 실행 {stamp} 의 프로필 {prof} 판정 {len(rows)}행(규칙 A, 훅 제외). "
                f"범위별 {dict(scopes)}. 값 형식: <범위> — 호출 · 저장소 · 세션/프로필 세션 · 실리는 토큰(추정) · "
                "켜진 세션 · 첫 켜짐. 읽는 법: claims(project=tool-scope) 또는 recall(project=tool-scope)."
            ),
            "concepts": ["tool-leak-triage"],
            "tags": ["tool-scope"],
            "repo": "tool-scope",
            "author": "agent:tool-scope",
            "claims": rows,
        }
        if prior_notes.get(prof):
            payload["supersedes"] = [prior_notes[prof]]
        match client.request("POST", "/remember", payload, timeout=120):
            case Ok(resp):
                notes[prof] = resp["source_path"]
            case Err(failure):
                return Err(f"remember {prof}: {failure}")
    return Ok(notes)


def candidates(table):
    rows = [r for r in judged(table) if r["rule_a"] in CANDIDATE_SCOPES and r["on_now"]]
    return sorted(rows, key=lambda r: (r["profile"], -(r["front_tok"] + r["deferred_tok"]), r["unit"]))


_CSS = (
    "body{margin:0;background:#FBFAF7;color:#1A1A1A;font-family:'Noto Serif KR',Georgia,serif;line-height:1.7}"
    "main{max-width:1000px;margin:0 auto;padding:40px 24px 80px}h1{font-size:32px;margin:0 0 8px}"
    "h2{font-size:22px;margin:48px 0 8px}.abstract{border-top:1px solid #1A1A1A;border-bottom:1px solid #1A1A1A;padding:16px 0}"
    "table{width:100%;border-collapse:collapse;font-family:'Noto Sans KR',Arial,sans-serif;font-size:13px}"
    "th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #E4E1D8;vertical-align:top}"
    "th{font-size:12px;color:#6B6B66;border-bottom:1px solid #1A1A1A}.m{color:#6B6B66}"
)


def write_html(path, cands, undecided, comp, meta):
    e = __import__("html").escape
    by = collections.defaultdict(list)
    for r in cands:
        by[r["profile"]].append(r)
    head = " · ".join(
        f"프로필 {p[:4]} 후보 {len(rs)}개 약 {sum(r['front_tok'] + r['deferred_tok'] for r in rs):,} 토큰"
        for p, rs in sorted(by.items())
    )
    refine = (
        "첫 실행 — 비교할 이전 판정 없음"
        if comp["first_run"]
        else f"이전 판정 {comp['prior_read']}행 읽음 · 틀린 끔 {len(comp['wrong_off'])} / 이전 끔 {comp['prior_off']} · "
        f"그 뒤 새 세션 {comp['new_sessions']} · 없어짐 {len(comp['gone'])}"
    )
    parts = [
        f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>도구 범위 후보</title><style>{_CSS}</style></head><body><main>",
        f"<h1>지금 켜져 있는데 끄거나 필요할 때만 켤 도구</h1><p class='m'>{e(meta['run_at'])} UTC · 전사 {meta['files']:,} · 세션 {meta['sessions']:,} · 규칙 A</p>",
        f"<div class='abstract'><p>{e(head)}.</p><p class='m'>{e(refine)}</p></div>",
    ]
    for p, rs in sorted(by.items()):
        parts.append(
            f"<h2>프로필 {p[:4]} — {len(rs)}개</h2><table><thead><tr><th>도구</th><th>범위</th><th>호출</th><th>세션</th>"
            "<th>저장소</th><th>켜진 세션</th><th>첫 켜짐</th><th>실리는 토큰(추정)</th><th>바꿀 자리</th><th>다시 켜는 법</th></tr></thead><tbody>"
        )
        parts += [
            f"<tr><td>{e(r['unit'])}</td><td>{e(r['rule_a'])}</td><td>{r['calls']}</td><td>{r['sessions']}/{r['den']}</td>"
            f"<td>{r['repos']}</td><td>{r['enabled_sessions']}</td><td>{e(r['first_on'])}</td><td>{r['front_tok'] + r['deferred_tok']:,}</td>"
            f"<td class='m'>{e(r['reach'])}</td><td class='m'>{e(r['reenable'])}</td></tr>"
            for r in rs
        ]
        parts.append("</tbody></table>")
    parts.append(
        f"<h2>판별 못 함 — {len(undecided)}개</h2><p class='m'>규칙 A 저장소 4~5. 그대로 두고 판정 자리가 본다.</p><table><tbody>"
    )
    parts += [
        f"<tr><td>{r['profile'][:4]}</td><td>{e(r['unit'])}</td><td>호출 {r['calls']} · 저장소 {r['repos']} · 세션 {r['sessions']}/{r['den']}</td></tr>"
        for r in undecided
    ]
    if comp["wrong_off"]:
        parts.append(f"</tbody></table><h2>틀린 끔 — {len(comp['wrong_off'])}개</h2><table><tbody>")
        parts += [f"<tr><td>{e(i)}</td></tr>" for i in comp["wrong_off"]]
    parts.append("</tbody></table></main></body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")


# ---- entry ----------------------------------------------------------------------------------


def load_table(profiles):
    path = index_path()
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = {}
    files = transcript_files()
    t0 = time.monotonic()
    index, scanned = build_index(files, cached)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    os.replace(path + ".tmp", path)
    sess = sessions_from(index)
    table = build_table(sess, profiles)
    meta = {
        "files": len(files),
        "scanned": scanned,
        "scan_s": round(time.monotonic() - t0, 1),
        "sessions": len(sess),
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
    }
    return table, meta, sess


TSV_COLS = (
    "profile",
    "unit",
    "calls",
    "sessions",
    "repos",
    "den",
    "window",
    "share_pct",
    "enabled_sessions",
    "front_tok",
    "deferred_tok",
    "rule_a",
    "reach",
)


def print_table(table, meta):
    print(
        f"# 전사 {meta['files']} · 이번에 읽음 {meta['scanned']} · 세션 {meta['sessions']} · 훑기 {meta['scan_s']}s"
    )
    print("# front_tok·deferred_tok = 추정(글자 비율, 20791 방법), 첫 요청 기준 중앙값")
    print("\t".join(TSV_COLS))
    for r in table:
        print("\t".join("-" if r.get(c) is None else str(r.get(c)) for c in TSV_COLS))


def main(argv=None):
    ap = argparse.ArgumentParser(description="켜고 끄는 단위별 호출·싣는 값 표와 규칙 A 범위")
    ap.add_argument("mode", nargs="?", default="write", choices=("write", "table"))
    ap.add_argument("--profiles", default=",".join(DEFAULT_PROFILES))
    args = ap.parse_args(argv)
    profiles = [p for p in args.profiles.split(",") if p]

    table, meta, sess = load_table(profiles)
    if not table:
        print("[tool-scope] 표가 비었다 — 프로필 세션 0", file=sys.stderr)
        return 1
    if args.mode == "table":
        print_table(table, meta)
        return 0

    out = out_dir()
    out.mkdir(parents=True, exist_ok=True)
    prior, prior_at = read_prior(out)
    comp = compare(table, prior, prior_at, sess, profiles)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    claims = claims_by_profile(table, comp["gone"])
    match remember_scopes(claims, (prior or {}).get("notes") or {}, stamp, DrudgeClient(timeout=120)):
        case Err(reason):
            print(
                f"[tool-scope] 적재 실패 — {reason}; 결과 파일을 안 써서 다음 실행이 같은 이전 판정과 비교한다",
                file=sys.stderr,
            )
            return 5
        case Ok(notes):
            pass
    cands = candidates(table)
    undecided = [r for r in judged(table) if r["rule_a"] == "판별 못 함"]
    path = out / f"tool-scope-{stamp}.json"
    result = {
        "meta": meta,
        "table": table,
        "candidates": [r["id"] for r in cands],
        "notes": notes,
        **comp,
    }
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    page = html_dir() / f"{verdict_core.window_day(meta['run_at'] + 'Z')}.html"
    write_html(page, cands, undecided, comp, meta)
    per = collections.Counter(r["profile"] for r in cands)
    toks = collections.Counter()
    for r in cands:
        toks[r["profile"]] += r["front_tok"] + r["deferred_tok"]
    print(
        f"[tool-scope] 행 {len(table)} · 판정 행(훅 제외) {len(judged(table))} · claim {sum(len(c) for c in claims.values())} · 노트 {len(notes)}"
    )
    print(
        "[tool-scope] "
        + ("첫 실행 · prior_read=0" if comp["first_run"] else f"prior_read={comp['prior_read']}")
        + f" · wrong_off={len(comp['wrong_off'])}/{comp['prior_off']} {comp['wrong_off']} · 새 세션 {comp['new_sessions']} · 없어짐 {len(comp['gone'])}"
    )
    print(f"[tool-scope] 후보 {dict(per)} · 토큰 {dict(toks)}")
    print(f"[tool-scope] {path}")
    print(f"[tool-scope] {page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
