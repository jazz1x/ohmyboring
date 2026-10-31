#!/usr/bin/env python3
"""Per-note «used» table for the hook path (C-UPTAKE1 A), read-only, from records that already exist.

    .venv/bin/python scripts/uptake-by-note.py --since 2026-10-20T00:00Z --until 2026-10-27T00:00Z

Population: sessions with an `injection_uptake` event observed in [since, until). Their handed
notes are the session's `handed` edges (session:<id> -> doc:/vault/wiki/wiki-N.md), their usage is
`uptake_core.hit_was_used` over the session transcript. The ledger rows that carried the stored
phrases are deleted at session end, so phrases are rebuilt from the CURRENT note body: every
value here is an approximation, and the first output line says so.

Reads only: `psql` under default_transaction_read_only, transcripts, the vault, the open-session
ledger. Writes nothing. Hook path only; the recall tool path has no session id and is not here.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agents", "shared"))

import transcript  # noqa: E402
import uptake_core  # noqa: E402
import verdict_core  # noqa: E402
import yaml  # noqa: E402

MATCH_TOLERANCE = 0.20
CELL_FLOOR = 100
CONTROL_PER_SESSION = 20
POSITIVE_CONTROL_NOTES = 20
SNIPPET_CHARS = 280  # uptake_core._fingerprints cuts the injected snippet at the same length
SEP = "\x1f"
KINDS = ("handoff", "decision", "distilled", "other")
AGES = ("≤1d", "≤7d", "≤30d", "30d+")
DEFAULT_VAULT = os.path.expanduser("~/Development/mine/oh-my-boring/vault")
DEFAULT_LEDGER = os.path.expanduser("~/.cache/boring-distill/injections.jsonl")
TRANSCRIPT_GLOBS = (
    "~/.claude/projects/*/*.jsonl",
    "~/Library/Application Support/orca/claude-profiles/*/home/projects/*/*.jsonl",
)


class Stop(Exception):
    """A guard fired: the output says why instead of printing a number."""


def psql(sql):
    """Rows of a SELECT against the live DB, read-only session."""
    res = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "boring-postgres",
            "psql",
            "-U",
            "boring",
            "-d",
            "boring",
            "-At",
            "-F",
            SEP,
            "-q",
        ],
        input=f"SET default_transaction_read_only=on;\n{sql};\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.split(SEP) for line in res.stdout.splitlines() if line]


def parse_z(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


def z(moment):
    return moment.strftime("%Y-%m-%dT%H:%MZ")


def pct(a, b):
    return f"{a}/{b} ({100.0 * a / b:.1f}%)" if b else f"{a}/{b} (못 잼)"


# ---------------------------------------------------------------- records


def load_events(since, until):
    """session id -> (repo, used_hits, total_hits) summed over the window's injection_uptake events."""
    rows = psql(
        "SELECT attributes->>'session_id', coalesce(attributes->>'repo',''), attributes->>'used_hits',"
        " attributes->>'total_hits' FROM event_log WHERE event_name='injection_uptake'"
        f" AND observed_at >= '{z(since)}'::timestamptz AND observed_at < '{z(until)}'::timestamptz"
        " ORDER BY id"
    )
    sessions = {}
    for sid, repo, used, total in rows:
        prev = sessions.get(sid, ("", 0, 0))
        sessions[sid] = (repo, prev[1] + int(used), prev[2] + int(total))
    return sessions, len(rows)


def load_handed(since, until):
    """(session id, doc path, handed epoch or None, doc updated epoch or None) for the window's sessions."""
    rows = psql(
        "SELECT substr(e.src, 9), substr(e.dst, 5), extract(epoch FROM e.first_seen_at)::bigint,"
        " extract(epoch FROM d.updated_at)::bigint"
        " FROM edge e JOIN (SELECT DISTINCT attributes->>'session_id' sid FROM event_log"
        f" WHERE event_name='injection_uptake' AND observed_at >= '{z(since)}'::timestamptz"
        f" AND observed_at < '{z(until)}'::timestamptz) s ON e.src = 'session:' || s.sid"
        " LEFT JOIN document d ON 'doc:' || d.source_path = e.dst WHERE e.kind='handed' ORDER BY 1, 2"
    )
    return [(s, p, int(t) if t else None, int(u) if u else None) for s, p, t, u in rows]


def load_superseded():
    """doc path -> epochs (None = time unknown) at which a `supersedes` edge made it the older note."""
    rows = psql(
        "SELECT substr(dst, 5), extract(epoch FROM first_seen_at)::bigint FROM edge"
        " WHERE kind='supersedes' AND dst LIKE 'doc:%'"
    )
    out = defaultdict(list)
    for path, t in rows:
        out[path].append(int(t) if t else None)
    return out


def null_edges_overall():
    null, total = psql(
        "SELECT count(*) FILTER (WHERE first_seen_at IS NULL), count(*) FROM edge WHERE kind='handed'"
    )[0]
    return int(null), int(total)


def transcript_index():
    """session id -> largest transcript file carrying that stem (resumed sessions leave copies)."""
    found = {}
    for pattern in TRANSCRIPT_GLOBS:
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            sid = os.path.basename(path)[: -len(".jsonl")]
            if sid not in found or os.path.getsize(path) > os.path.getsize(found[sid]):
                found[sid] = path
    return found


# ---------------------------------------------------------------- notes


class Note:
    """What the current vault file says about a note: its kind, its birth date and rebuilt phrases."""

    def __init__(self, path, vault):
        self.path = path
        self.src = path.rsplit("/", 1)[-1]
        self.kind, self.born, self.project, self.phrases, self.present = "other", None, "", [], False
        file = os.path.join(vault, path[len("/vault/") :]) if path.startswith("/vault/") else path
        try:
            with open(file, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            return
        self.present = True
        head, _, body = raw.removeprefix("---\n").partition("\n---")
        meta = yaml.safe_load(head) or {}
        self.project = str(meta.get("project") or "")
        self.kind = note_kind(meta)
        self.born = born_epoch(meta.get("date"))
        self.phrases = uptake_core.phrases(" ".join(body.split())[:SNIPPET_CHARS])

    def hit(self):
        return {"src": self.src, "phrases": self.phrases}


def note_kind(meta):
    """handoff by title; decision when any claim is one; distilled when no agent authored it."""
    if re.search("인계|handoff", str(meta.get("title") or ""), re.IGNORECASE):
        return "handoff"
    if any(isinstance(c, dict) and c.get("kind") == "decision" for c in meta.get("claims") or []):
        return "decision"
    if str(meta.get("author") or "") in ("", "unknown", "inferred"):
        return "distilled"
    return "other"


def born_epoch(date):
    try:
        day = datetime.strptime(str(date), "%Y-%m-%d").replace(tzinfo=verdict_core.WINDOW_TZ)
    except ValueError:
        return None
    return int(day.timestamp())


def age_bin(handed, born):
    days = max(0.0, (handed - born) / 86400.0)
    return "≤1d" if days <= 1 else "≤7d" if days <= 7 else "≤30d" if days <= 30 else "30d+"


# ---------------------------------------------------------------- scoring


class Session:
    def __init__(self, path):
        text = transcript.extract(path)
        self.assistant = " ".join(uptake_core._words(uptake_core.assistant_text(text)))
        user_turns = uptake_core._TURN.split(text)
        user_text = "\n".join(
            user_turns[i + 1] for i in range(1, len(user_turns) - 1, 2) if user_turns[i] == "user"
        )
        self.prompt_words = uptake_core._words(user_text)

    def used(self, note):
        return uptake_core.hit_was_used(note.hit(), self.assistant, self.prompt_words)


def chance_pool_order(sid, paths):
    return sorted(paths, key=lambda p: hashlib.sha1(f"{sid}|{p}".encode()).hexdigest())


def positive_control(notes):
    """A phrase planted in an assistant blob must read as used, and its absence must not."""
    probes = [n for n in sorted(notes.values(), key=lambda n: n.path) if n.phrases][:POSITIVE_CONTROL_NOTES]
    planted = sum(uptake_core.hit_was_used(n.hit(), f"filler {n.phrases[0]} filler", []) for n in probes)
    absent = sum(uptake_core.hit_was_used(n.hit(), "filler words only here", []) for n in probes)
    return planted, absent, len(probes)


def open_session_check(notes_for, ledger_path, index):
    """Per (open session, note): the ledger's own used vs this script's recomputed used."""
    by_session = defaultdict(list)
    try:
        with open(ledger_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("session_id"):
                    by_session[row["session_id"]].append(row)
    except OSError:
        return 0, 0, 0, 0
    agree = compared = unreadable = 0
    for sid, records in sorted(by_session.items()):
        path = index.get(sid)
        if not path:
            unreadable += 1
            continue
        session = Session(path)
        ledger_used, recomputed = {}, {}
        for record in records:
            for hit in record.get("hits") or []:
                src = hit.get("src") or ""
                used = uptake_core.hit_was_used(hit, session.assistant, record.get("prompt_words") or [])
                ledger_used[src] = ledger_used.get(src, False) or used
        for src in ledger_used:
            note = notes_for(f"/vault/wiki/{src}")
            if note.present:
                recomputed[src] = session.used(note)
        for src, value in recomputed.items():
            compared += 1
            agree += value == ledger_used[src]
    return len(by_session), unreadable, agree, compared


# ---------------------------------------------------------------- main


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", required=True, help="window start, e.g. 2026-10-20T00:00Z")
    parser.add_argument("--until", required=True, help="window end (exclusive)")
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    return parser


def main():
    args = build_parser().parse_args()
    since, until = parse_z(args.since), parse_z(args.until)
    events, event_rows = load_events(since, until)
    handed = load_handed(since, until)
    superseded = load_superseded()
    index = transcript_index()
    notes = {}

    def note_for(path):
        if path not in notes:
            notes[path] = Note(path, args.vault)
        return notes[path]

    window = f"[{z(since)}, {z(until)})"
    for _s, path, _t, _u in handed:
        note_for(path)

    planted, absent, probes = positive_control(notes)
    if probes == 0 or planted != probes or absent != 0:
        raise Stop(
            f"양성 대조 실패: 심은 문구 {planted}/{probes} 읽힘, 없는 문구 {absent}/{probes} 읽힘 — 표를 쓰지 않음"
        )

    by_session = defaultdict(list)
    for sid, path, t, u in handed:
        by_session[sid].append((path, t, u))
    scored_sessions = sorted(s for s in by_session if s in index)

    # score every pair of a session that has a transcript
    pairs = []  # (sid, path, handed, updated, used)
    chance_used = chance_tested = 0
    all_handed_paths = sorted({p for _s, p, _t, _u in handed if notes[p].present})
    for sid in scored_sessions:
        session = Session(index[sid])
        own = {p for p, _t, _u in by_session[sid]}
        for path, t, u in by_session[sid]:
            note = notes[path]
            pairs.append((sid, path, t, u, session.used(note) if note.present else None))
        for path in chance_pool_order(sid, [p for p in all_handed_paths if p not in own])[
            :CONTROL_PER_SESSION
        ]:
            chance_tested += 1
            chance_used += session.used(notes[path])

    scored_pairs = [p for p in pairs if p[4] is not None]
    if not any(p[4] for p in scored_pairs):
        raise Stop("실데이터에서 used 가 0 건 — 계기가 아무것도 못 본 것일 수 있어 표를 쓰지 않음")

    # match line: same sessions, same window, hook-path events vs this script
    hook_used = sum(events[s][1] for s in scored_sessions)
    hook_total = sum(events[s][2] for s in scored_sessions)
    a_total = len(pairs)
    a_used = sum(1 for p in pairs if p[4])
    drift_total = (a_total - hook_total) / hook_total if hook_total else None
    drift_used = (a_used - hook_used) / hook_used if hook_used else None
    out = []
    match_line = (
        f"맞춤: A 건넴 {a_total} / 훅 total_hits {hook_total} ({drift_total:+.1%}) · "
        f"A used {a_used} / 훅 used_hits {hook_used} ({drift_used:+.1%}) · 같은 세션 {len(scored_sessions)}, {window}"
        if hook_total and hook_used
        else f"맞춤: 훅 합계 {hook_used}/{hook_total} — 분모 0 은 못 잼"
    )
    in_band = (
        hook_total
        and hook_used
        and abs(drift_total) <= MATCH_TOLERANCE
        and abs(drift_used) <= MATCH_TOLERANCE
    )
    if not in_band:
        print("노트별 사용 표 없음 — 맞춤 ±20% 초과")
        print("훅 길만, recall 도구 길 제외")
        print(match_line)
        print(
            f"원인 후보: 사건 세션 {len(events)} 중 전사 있음 {len(scored_sessions)}; 간선 건넴 {len(handed)}; "
            f"A 의 phrases 는 지금 노트 본문 앞 {SNIPPET_CHARS}자에서 다시 만든 근사 (원장은 세션 끝에 삭제)"
        )
        return 0

    # table accounting: every handed pair lands in exactly one place
    no_session = [h for h in handed if h[0] not in index]
    null_time = [p for p in scored_pairs if p[2] is None]
    no_note = [p for p in pairs if p[4] is None]
    timed = [p for p in scored_pairs if p[2] is not None]
    undated = [p for p in timed if notes[p[1]].born is None]
    full = [p for p in timed if notes[p[1]].born is not None]
    replaced = [p for p in full if any(s is not None and s > p[2] for s in superseded.get(p[1], []))]
    replaced_ids = {id(p) for p in replaced}
    unreplaced = [p for p in full if id(p) not in replaced_ids]
    mtime_moved = [p for p in timed if p[3] is not None and p[3] > p[2]]

    def fill(rows):
        cells = defaultdict(lambda: [0, 0])
        kind_cells = defaultdict(lambda: [0, 0])
        for sid, path, t, _u, used in rows:
            note = notes[path]
            key = (
                note.kind,
                age_bin(t, note.born),
                "yes" if note.project and note.project == events[sid][0] else "no",
            )
            for target in (cells[key], kind_cells[key[0]]):
                target[0] += bool(used)
                target[1] += 1
        return cells, kind_cells

    def kind_order(kind_cells):
        return [
            k
            for _r, k in sorted(
                (kind_cells[k][0] / kind_cells[k][1], k) for k in KINDS[:3] if kind_cells[k][1] >= CELL_FLOOR
            )
        ]

    cells_1, kinds_1 = fill(full)
    cells_2, kinds_2 = fill(unreplaced)
    order_1, order_2 = kind_order(kinds_1), kind_order(kinds_2)
    if not order_1 or not order_2:
        headline = "가장 안 쓰이는 종류 아직"
    elif order_1 == order_2:
        headline = f"가장 안 쓰이는 종류 {order_1[0]}"
    else:
        headline = f"종류 순위 갈림 — ① {' < '.join(order_1)} · ② {' < '.join(order_2)} (판정에 넘김)"

    out.append(f"노트별 사용 표(근사) — {headline}")
    out.append("훅 길만, recall 도구 길 제외")
    out.append(match_line)
    out.append(
        f"대조(건네지 않은 노트 이름·문구, 세션당 최대 {CONTROL_PER_SESSION}, 같은 판정 함수): "
        f"{pct(chance_used, chance_tested)} · 건넨 쪽 {pct(a_used, a_total)}"
    )
    out.append(
        f"양성 대조: 심은 문구 {planted}/{probes} 읽힘, 없는 문구 {absent}/{probes} 읽힘 · 실데이터 used {a_used} 건"
    )

    ledger_sessions, ledger_unreadable, agree, compared = open_session_check(note_for, args.ledger, index)
    if ledger_sessions == 0:
        out.append("a) 열린 세션 0 — 원장 줄 없음")
    else:
        out.append(
            f"a) 원장 used vs 재계산 used 일치 {agree} / 비교 {compared} "
            f"(원장 있는 세션 {ledger_sessions}, 전사 못 찾음 {ledger_unreadable})"
        )

    out.append(
        f"b) 건넨 뒤 supersedes 로 대체된 노트 {len({p[1] for p in replaced})} / 건넨 노트 {len({p[1] for p in full})}, "
        f"쌍 {len(replaced)} / {len(full)} — ② 에서만 뺌"
    )
    out.append(
        f"버린 기준: 파일 mtime 「바뀜」 쌍 {len(mtime_moved)} / {len(timed)} — sync 가 머리말만 다시 써도 mtime 이 움직여 본문 바뀜을 못 가름"
    )
    null_all, total_all = null_edges_overall()
    out.append(
        f"NULL first_seen_at: 창 세션 쌍 {len(null_time)} / {a_total} (표에서 뺌, 0 이 아니라 시각 모름) · "
        f"전체 handed 간선 {null_all} / {total_all}"
    )
    out.append(
        f"대체 쌍의 used (② 에서 뺀 것이 결과를 바꾸는지 보라고): 대체 {pct(sum(1 for p in replaced if p[4]), len(replaced))}"
        f" · ② {pct(sum(1 for p in unreplaced if p[4]), len(unreplaced))}"
    )
    out.append(
        f"쌍 장부: 건넴 {a_total} = ① {len(full)} + NULL {len(null_time)} + 날짜 없음 {len(undated)}"
        f" + 노트 파일 없음 {len(no_note)}; ① {len(full)} = ② {len(unreplaced)} + 대체 {len(replaced)};"
        f" 전사 없는 세션의 간선 {len(no_session)} 은 장부 밖, {window}"
    )
    out.append("")
    out.append("종류별 used/handed — ① 전체 · ② 대체 뺌 (건넴 100 미만은 「아직」)")
    for kind in KINDS:
        shown = []
        for kind_cells in (kinds_1, kinds_2):
            a, b = kind_cells[kind]
            shown.append(pct(a, b) if b >= CELL_FLOOR else f"아직 (건넴 {b})")
        out.append(f"  {kind:<10} ① {shown[0]} · ② {shown[1]}")
    out.append("")
    out.append("종류 × 나이 × project 일치 — used/handed, ① 전체 · ② 대체 뺌")
    lowest = ([], [])
    for kind in KINDS:
        for age in AGES:
            for match in ("yes", "no"):
                shown = []
                for side, cells in enumerate((cells_1, cells_2)):
                    a, b = cells[(kind, age, match)]
                    shown.append(pct(a, b) if b >= CELL_FLOOR else f"아직 (건넴 {b})")
                    if b >= CELL_FLOOR:
                        lowest[side].append((a / b, kind, age, match, a, b))
                out.append(f"  {kind:<10} {age:<4} project={match:<3} ① {shown[0]} · ② {shown[1]}")
    out.append("")
    for mark, rows in zip("①②", lowest):
        out.append(
            f"가장 낮은 칸 3개 {mark} (건넴 100 이상): "
            + " | ".join(f"{k} {ag} project={m} {a}/{b}" for _r, k, ag, m, a, b in sorted(rows)[:3])
        )
    out.append(f"명령: .venv/bin/python scripts/uptake-by-note.py --since {z(since)} --until {z(until)}")
    out.append(
        "값은 근사: phrases 를 지금 노트 본문에서 다시 만들었고, 프롬프트 차감은 세션 전체 user 글 기준"
    )
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Stop as stop:
        print(f"멈춤: {stop}")
        sys.exit(2)
