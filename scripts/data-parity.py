#!/usr/bin/env python3
"""데이터 이전 대조 검사기 — 같은 덤프의 두 사본 DB 를 물린 임시 엔진 둘에 같은 질문을 던져 네 가지를 잰다.

① 현재 기록 집합(철자 접기 후, (canon(subject), canon(predicate), value, source_path))
② 활성 프로젝트(최근 14일)마다 카드 다섯(정체·위험·다음·결정·재발)의 응답
③ 검색 품질(data/eval/golden.json, run_eval.py 의 recall@3)
④ 판정 간선(handed·used·contested·supersedes) 종류별 개수 + event_log 행 수

모두 같으면 종료코드 0, 하나라도 다륾으면 1, 엔진 불통이면 2. 차이가 있으면 상위 5건을
data/loop/parity-<UTC시각>.json 에 적는다(data/ 는 gitignore). 이 검사기는 운영 DB(boring)와
운영 엔진(:7700)을 모른다 — 사본과 임시 엔진만 본다. 운영에 검사 질의를 던지면 운영 query_log 와
handed 간선을 오염시키므로(probes-must-not-write-to-the-measurement) 사본 엔진에만 질의한다.
"""

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

EDGE_KINDS = ("handed", "used", "contested", "supersedes")
CARD_ENDPOINTS = ("stalled", "risks", "next_actions", "decisions", "recurrences")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
RUN_EVAL = os.path.join(REPO_ROOT, "data", "eval", "run_eval.py")
RE_CALL = re.compile(r"^Recall@3: (\d+)/(\d+)", re.M)
PSQL_CONTAINER = os.environ.get("PARITY_PSQL_CONTAINER", "boring-postgres")
PSQL_USER = os.environ.get("PARITY_PSQL_USER", "boring")
HTTP_TIMEOUT = 120
EVAL_TIMEOUT = 900


class EngineDown(Exception):
    """엔진 불통 — 종료코드 2 가 될 보이는 실패."""


def canon(s: str) -> str:
    """drudge/src/ingest.rs::canon 과 같은 규칙: 소문자 → 공백·밑줄·하이픈 연속을 하이픈 하나로 → 앞뒤 하이픈 제거."""
    out: list[str] = []
    prev_sep = False
    for c in s.lower():
        if c.isspace() or c in "_-":
            prev_sep = True
        else:
            if prev_sep and out:
                out.append("-")
            prev_sep = False
            out.append(c)
    return "".join(out)


def symdiff(a: set, b: set) -> tuple[list, list]:
    """집합 대칭차를 (a 에만 있는 것, b 에만 있는 것)으로, 각각 정렬된 리스트로 돌려준다."""
    return sorted(a - b), sorted(b - a)


def psql_csv(db: str, sql: str) -> list[list[str]]:
    cmd = [
        "docker",
        "exec",
        "-i",
        PSQL_CONTAINER,
        "psql",
        "-U",
        PSQL_USER,
        "-d",
        db,
        "--csv",
        "-c",
        sql,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"psql 실패 ({db}): {p.stderr.strip()}")
    rows = list(csv.reader(io.StringIO(p.stdout)))
    return rows[1:] if rows else []


def psql_count(db: str, sql: str) -> int:
    rows = psql_csv(db, sql)
    return int(rows[0][0])


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise EngineDown(f"{method} {url} 불통: {e}") from e


def health(url: str) -> None:
    try:
        http_json("GET", f"{url}/health")
    except EngineDown:
        raise
    except Exception as e:  # EngineDown 이 아닌 예상 밖 실패도 불통으로 — 조용한 통과 없음
        raise EngineDown(f"GET {url}/health 실패: {e}") from e


def current_records(db: str) -> set:
    rows = psql_csv(
        db,
        "select subject, predicate, value, source_path from claim where superseded_at is null",
    )
    bad = [r for r in rows if len(r) != 4]
    if bad:
        raise RuntimeError(f"{db} claim 행의 열 수가 4 가 아님: {bad[:1]}")
    return {(canon(r[0]), canon(r[1]), r[2], r[3]) for r in rows}


def active_projects(db: str) -> list[str]:
    rows = psql_csv(
        db,
        "select project, count(*) from document where project<>'' "
        "and updated_at > now() - make_interval(days=>14) group by 1",
    )
    return sorted(r[0] for r in rows)


def card_responses(url: str, projects: list[str]) -> dict:
    out = {}
    for project in projects:
        for ep in CARD_ENDPOINTS:
            out[(project, ep)] = normalize_card(ep, http_json("POST", f"{url}/{ep}", {"project": project}))
    return out


def normalize_card(endpoint: str, resp: dict) -> dict:
    """카드 응답을 비교용으로 정규화한다.

    엔진 레지스터 SQL 의 ORDER BY 에 동점을 가르는 키가 없어(store.rs::recent_register_rows,
    stalled_register_rows, recurrences), 데이터가 바이트 같아도 두 사본에서 계획 의존적으로
    행 순서·동점 행 선정이 갈린다(2026-09-23 통제군 실측, stalled 가 같은 사실의 철자 두 줄 중
    다른 쪽을 고르기도 함). 변환 대조가 재는 것은 「카드가 읽는 목록」의 내용이므로, ① 과 같은
    철자 접기(canon)와 행 순서를 접고 비교한다. 행의 지워짐·값 변화·개수 변화는 그대로 잡힌다.
    """
    if endpoint == "recurrences":
        return _norm_recurrences(resp)
    return _norm_register(resp)


def _norm_register(resp: dict) -> dict:
    lines = resp.get("answer", "").split("\n")
    header, rows = (lines[0] if lines else ""), []
    for line in lines[1:]:
        if not line.startswith("* "):
            continue
        subject, sep, rest = line[2:].partition(" — ")
        if not sep:
            raise RuntimeError(f"레지스터 행을 파싱 못 함: {line!r}")
        rows.append(f"{canon(subject)} — {rest}")
    return {
        "header": header,
        "rows": sorted(rows),
        "sources": sorted(canon(s) for s in resp.get("sources", [])),
        "injected_claims": resp.get("injected_claims", []),
    }


def _norm_recurrences(resp: dict) -> dict:
    def stable(obj) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)

    def fold(claim: dict) -> dict:
        c = dict(claim)
        c["subject"] = canon(c.get("subject", ""))
        return c

    rows = []
    for r in resp.get("rows", []):
        # distance·days_apart 는 그룹의 첫 쌍 표시값인데, 첫 쌍은 ORDER BY distance 의 동점을 계획이
        # 가르기 때문에 사본끼리 갈린다(2026-09-23 통제군: 같은 쌍 0.11347193 인데 36 vs 42 일).
        # 파생 표시값이고 newer·older 의 valid_from 에서 도출되므로 비교에서 뺀다.
        rows.append(
            {
                "newer": fold(r.get("newer", {})),
                "older": sorted((fold(o) for o in r.get("older", [])), key=stable),
                "label_only": r.get("label_only"),
            }
        )
    rows.sort(key=stable)
    return {
        "rows": rows,
        "days": resp.get("days"),
        "max_distance": resp.get("max_distance"),
        "min_days_apart": resp.get("min_days_apart"),
    }


def run_recall(url: str) -> str:
    env = dict(os.environ, BORING_URL=url)
    try:
        p = subprocess.run(
            [sys.executable, RUN_EVAL], capture_output=True, text=True, env=env, timeout=EVAL_TIMEOUT
        )
    except subprocess.TimeoutExpired as e:
        raise EngineDown(f"run_eval 900초 초과 ({url})") from e
    m = RE_CALL.search(p.stdout)
    if not m:
        raise EngineDown(f"run_eval 에서 Recall@3 를 못 읽었다 ({url}): {p.stderr.strip()[:300]}")
    return f"{m.group(1)}/{m.group(2)}"


def graph_metrics(db: str) -> dict:
    kinds = {
        r[0]: int(r[1])
        for r in psql_csv(
            db,
            "select kind, count(*) from edge where kind in "
            "('handed','used','contested','supersedes') group by 1",
        )
    }
    events = psql_count(db, "select count(*) from event_log")
    metrics = {f"edge:{k}": kinds.get(k, 0) for k in EDGE_KINDS}
    metrics["event_log"] = events
    return metrics


def fmt_graph(m: dict) -> str:
    return "/".join(f"{k.removeprefix('edge:')}:{v}" for k, v in m.items() if k != "event_log") + (
        f"/event:{m['event_log']}"
    )


def write_report(diff: dict) -> str:
    os.makedirs(os.path.join(REPO_ROOT, "data", "loop"), exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(REPO_ROOT, "data", "loop", f"parity-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(diff, f, ensure_ascii=False, indent=2, default=list)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="두 사본 DB 의 데이터 이전 전후 대조 검사")
    ap.add_argument("--a", required=True, help="기준 A 임시 엔진 URL")
    ap.add_argument("--b", required=True, help="변환 대상 B 임시 엔진 URL")
    ap.add_argument("--a-db", required=True, help="기준 A 사본 DB 이름")
    ap.add_argument("--b-db", required=True, help="변환 대상 B 사본 DB 이름")
    args = ap.parse_args()

    try:
        health(args.a)
        health(args.b)

        rec_a = current_records(args.a_db)
        rec_b = current_records(args.b_db)
        only_a, only_b = symdiff(rec_a, rec_b)

        projects = active_projects(args.a_db)
        cards_a = card_responses(args.a, projects)
        cards_b = card_responses(args.b, projects)
        card_diffs = sorted(k for k in cards_a if cards_a[k] != cards_b[k])

        recall_a = run_recall(args.a)
        recall_b = run_recall(args.b)

        graph_a = graph_metrics(args.a_db)
        graph_b = graph_metrics(args.b_db)
        graph_diff_keys = sorted(k for k in graph_a if graph_a[k] != graph_b[k])
    except EngineDown as e:
        print(f"엔진 불통: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        # 측정 장비(psql·행 파싱) 불통은 데이터 차이(1) 와 같은 코드로 읽히면 안 된다 — 고장 낸 장비를
        # "차이 없음/있음"으로 읽는 것보다 "잰 게 아니다"(2) 가 맞다.
        print(f"측정 장비 불통: {e}", file=sys.stderr)
        return 2

    results = [
        ("① 현재기록", len(rec_a), len(rec_b), len(only_a) + len(only_b)),
        ("② 카드응답", len(cards_a), len(cards_b), len(card_diffs)),
        ("③ recall@3", recall_a, recall_b, 0 if recall_a == recall_b else 1),
        ("④ 간선·이벤트", fmt_graph(graph_a), fmt_graph(graph_b), len(graph_diff_keys)),
    ]
    for name, va, vb, d in results:
        print(f"{name} A={va} B={vb} 차이={d} {'같음' if d == 0 else '다름'}")

    total_diff = sum(r[3] for r in results)
    if total_diff == 0:
        return 0

    samples = {
        "records": {
            "a_only_top5": [list(r) for r in only_a[:5]],
            "b_only_top5": [list(r) for r in only_b[:5]],
        },
        "cards": [
            {"project": p, "endpoint": ep, "a": cards_a[(p, ep)], "b": cards_b[(p, ep)]}
            for p, ep in card_diffs[:5]
        ],
        "recall": {"a": recall_a, "b": recall_b},
        "graph": {k: {"a": graph_a[k], "b": graph_b[k]} for k in graph_diff_keys},
    }
    path = write_report(samples)
    print(f"차이 상세(상위 5건): {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
