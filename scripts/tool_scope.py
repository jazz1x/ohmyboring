#!/usr/bin/env python3
"""Which tools leak: per on/off unit, what it costs to carry and how often it is called (C-TOOLLEAK1).

A unit is what can be switched on or off: one built-in tool, one MCP server, one claude.ai
connector, one plugin MCP, one skill (a plugin's skills are one unit), one hook. Datadog (2)'s
~300 tool names are one row.

The engine counts; hermes picks a scope per row with numbers as its reason; the owner changes
settings. Nothing here writes a setting.

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

`table` prints the engine table only (no model). With no arguments (the card program run by
card_runner) it builds the table, asks hermes's model per batch of rows, checks every quoted
number against the table, and writes the result under $BORING_TOOL_SCOPE_OUT.
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
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import usage  # noqa: E402

PROFILES_DIR = Path(
    os.environ.get("BORING_CLAUDE_PROFILES_DIR")
    or Path.home() / "Library" / "Application Support" / "orca" / "claude-profiles"
)
DEFAULT_PROFILES = ("a0faa78e", "fe5c62d7", "791c085b")
SCOPES = ("유저 범위", "프로젝트 범위", "로컬", "필요할 때만", "끔", "판별 못 함")
CITE_FIELDS = (
    "calls",
    "sessions",
    "repos",
    "den",
    "share_pct",
    "enabled_sessions",
    "front_tok",
    "deferred_tok",
)
BATCH = 12

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
    return Path(
        os.environ.get("BORING_TOOL_SCOPE_OUT") or Path.home() / ".cache" / "boring-distill" / "tool-scope"
    )


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
        for s in mine.values():
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


# ---- quote check ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteCheck:
    missing: list = field(default_factory=list)  # table rows with no answer
    extra: list = field(default_factory=list)  # answers for rows not in the table
    mismatches: list = field(default_factory=list)  # (row id, field, cited, table)
    uncited: list = field(default_factory=list)  # answers citing no number

    @property
    def ok(self):
        return not (self.missing or self.extra or self.mismatches or self.uncited)


def check_quotes(table, answers):
    """Every row answered once, and every number an answer cites equals the table's value."""
    by_id = {r["id"]: r for r in table}
    missing = [r["id"] for r in table if r["id"] not in answers]
    extra = [a for a in answers if a not in by_id]
    mismatches, uncited = [], []
    for row_id, ans in answers.items():
        row = by_id.get(row_id)
        if row is None:
            continue
        cites = ans.get("cites") or {}
        if not cites:
            uncited.append(row_id)
        for key, value in cites.items():
            if row.get(key) != value:
                mismatches.append((row_id, key, value, row.get(key)))
    return QuoteCheck(missing, extra, mismatches, uncited)


# ---- hermes judge ---------------------------------------------------------------------------

PROMPT = """너는 도구 정리 판정자다. 아래 표의 행마다 켜는 범위를 하나 고르고, 표에 있는 숫자로 이유를 댄다.

범위는 이 여섯 중 하나: {scopes}
- 유저 범위: 여러 저장소에서 자주 쓴다 · 프로젝트 범위: 몇 저장소에서만 쓴다 · 로컬: 그 저장소 나만 쓴다
- 필요할 때만: 드물게 쓰는데 싣는 값이 있다 · 끔: 부른 적이 없다 · 판별 못 함: 숫자로 가를 수 없다(이유 적기)
- calls·sessions·repos 가 null 인 행(훅)은 호출을 셀 수 없다. 그래도 front_tok·enabled_sessions 중 하나를 인용한다.

칸 뜻: calls 호출 수 · sessions 부른 세션 수 · repos 부른 저장소 수 · den 그 프로필 세션 수 · share_pct 세션 비율(%) ·
enabled_sessions 첫 요청에 실린 세션 수 · front_tok 앞에 싣는 정의 추정 토큰(중앙) · deferred_tok 이름만 싣는 미룬 도구 추정 토큰(중앙).

규칙: cites 에는 이유에 쓴 숫자를 표의 칸 이름과 값 그대로 넣는다(값을 바꾸거나 계산하지 마라). 표에 없는 칸은 쓰지 마라.
JSON 하나만 답한다: {{"rows":[{{"id":"<행 id>","scope":"<범위>","reason":"<한 줄>","cites":{{"<칸>":<값>}}}}]}}

표:
{rows}
"""


def _prompt_rows(rows):
    keep = (
        "id",
        "kind",
        "calls",
        "sessions",
        "repos",
        "den",
        "share_pct",
        "enabled_sessions",
        "front_tok",
        "deferred_tok",
        "reach",
    )
    return "\n".join(json.dumps({k: r.get(k) for k in keep}, ensure_ascii=False) for r in rows)


def make_judge(model=None):
    """hermes's card model through the same ChatOllama seam the cards use; returns (text, tokens in, out)."""
    from langchain_ollama import ChatOllama

    llm = ChatOllama(
        model=model or os.environ.get("CARD_MODEL") or "gemma4:12b",
        format="json",
        temperature=0,
        reasoning=False,
        num_ctx=16384,
    )

    def invoke(prompt):
        msg = llm.invoke(prompt)
        meta = getattr(msg, "usage_metadata", None) or {}
        return str(msg.content), int(meta.get("input_tokens") or 0), int(meta.get("output_tokens") or 0)

    return invoke


def parse_answer(raw):
    """Model text → {row id: answer}; text that is not the asked shape answers nothing."""
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    out = {}
    for a in rows:
        if isinstance(a, dict) and isinstance(a.get("id"), str) and a.get("scope") in SCOPES:
            cites = a.get("cites") if isinstance(a.get("cites"), dict) else {}
            out[a["id"]] = {"scope": a["scope"], "reason": str(a.get("reason", "")), "cites": cites}
    return out


def judge_table(table, invoke):
    answers, tin, tout = {}, 0, 0
    for i in range(0, len(table), BATCH):
        chunk = table[i : i + BATCH]
        text, a, b = invoke(PROMPT.format(scopes=" · ".join(SCOPES), rows=_prompt_rows(chunk)))
        tin += a
        tout += b
        ids = {r["id"] for r in chunk}
        answers.update({k: v for k, v in parse_answer(text).items() if k in ids})
    return answers, tin, tout


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
    }
    return table, meta


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
    ap = argparse.ArgumentParser(description="켜고 끄는 단위별 호출·싣는 값 표, 그리고 hermes 범위 판단")
    ap.add_argument("mode", nargs="?", default="judge", choices=("judge", "table"))
    ap.add_argument("--profiles", default=",".join(DEFAULT_PROFILES))
    args = ap.parse_args(argv)
    profiles = [p for p in args.profiles.split(",") if p]

    table, meta = load_table(profiles)
    if not table:
        print("[tool-scope] 표가 비었다 — 프로필 세션 0", file=sys.stderr)
        return 1
    if args.mode == "table":
        print_table(table, meta)
        return 0

    t0 = time.monotonic()
    answers, tin, tout = judge_table(table, make_judge())
    elapsed = round(time.monotonic() - t0, 1)
    check = check_quotes(table, answers)
    judged = [r for r in table if r["id"] in answers and r["rule_a"] != "-"]
    agree = sum(1 for r in judged if answers[r["id"]]["scope"] == r["rule_a"])
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out = out_dir()
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "meta": {**meta, "judge_s": elapsed, "tokens_in": tin, "tokens_out": tout, "batch": BATCH},
        "table": [
            {
                **r,
                **{
                    "scope": answers.get(r["id"], {}).get("scope"),
                    "reason": answers.get(r["id"], {}).get("reason"),
                    "cites": answers.get(r["id"], {}).get("cites"),
                },
            }
            for r in table
        ],
        "check": {
            "missing": check.missing,
            "extra": check.extra,
            "mismatches": check.mismatches,
            "uncited": check.uncited,
        },
        "rule_a_agree": [agree, len(judged)],
    }
    path = out / f"tool-scope-{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"[tool-scope] 행 {len(table)} · 답 {len(answers)} · 빠짐 {len(check.missing)} · 인용 불일치 {len(check.mismatches)} · 인용 없음 {len(check.uncited)}"
    )
    print(
        f"[tool-scope] 규칙 A 일치 {agree}/{len(judged)} (훅 행 제외) · 모델 {elapsed}s · 토큰 입력 {tin} 출력 {tout} · 훑기 {meta['scan_s']}s"
    )
    print(f"[tool-scope] {path}")
    return 0 if check.ok else 4


if __name__ == "__main__":
    sys.exit(main())
