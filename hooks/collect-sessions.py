#!/usr/bin/env python3
"""Lazy 백필 수집기 — 못 쌓은 과거 세션을 1회당 소량만 슬금슬금 적재.

SessionEnd 훅은 세션 *종료* 때만 발동 → 긴/열린/과거 세션은 안 잡힘. 이 수집기는
~/.claude/projects 의 top-level 세션 .jsonl(서브에이전트/워크플로 제외)을 훑어
**아직 안 한 것만**(마커 없음) 1회당 LIMIT개 증류한다. 한꺼번에 안 돌려 CPU 안 태움.

- 마커: distill-session.py 와 같은 ~/.cache/boring-distill/<sid>.ts. 있으면 skip(이미 함).
- LIMIT(기본 1, COLLECT_LIMIT): 1회 호출당 처리 수. launchd/cron 으로 주기 호출 → 천천히 소진.
- WINDOW(기본 720h=30d, COLLECT_WINDOW_HOURS): 너무 오래된 건 무시.
- 각 세션 → distill-session.py (DISTILL_NO_SYNC=1), 끝에 /sync 한 번.
- cwd = 인코딩된 프로젝트 디렉터리명 → distill-session 이 DISTILL_COMPANY_CWD 토큰으로 origin 판별.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

DRUDGE_URL = os.environ.get("DRUDGE_URL", "http://localhost:7700")
WINDOW_H = float(os.environ.get("COLLECT_WINDOW_HOURS") or "720")
LIMIT = int(os.environ.get("COLLECT_LIMIT") or "1")
MIN_KB = float(os.environ.get("COLLECT_MIN_KB") or "20")  # 작은 세션 skip(distill이 어차피 SKIP)
HOOK = os.path.expanduser("~/oh-my-boring/hooks/distill-session.py")
PROJECTS = os.path.expanduser("~/.claude/projects")
MARK_DIR = os.path.expanduser("~/.cache/boring-distill")


def _marked(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"
    return os.path.exists(os.path.join(MARK_DIR, f"{safe}.ts"))


def main():
    cutoff = time.time() - WINDOW_H * 3600
    paths = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))  # top-level 만
    # 아직 안 한 것(마커 없음) + 윈도우 내 → 최신순
    todo = [
        p
        for p in paths
        if os.path.getmtime(p) >= cutoff
        and os.path.getsize(p) >= MIN_KB * 1024
        and not _marked(os.path.splitext(os.path.basename(p))[0])
    ]
    todo.sort(key=os.path.getmtime, reverse=True)
    batch = todo[:LIMIT]
    print(f"[collect] 미수집={len(todo)} 이번배치={len(batch)} (LIMIT={LIMIT})", flush=True)
    if not batch:
        print("[collect] 다 했음 — 할 일 없음", flush=True)
        return

    env = dict(os.environ, DISTILL_NO_SYNC="1")
    done = 0
    for tp in batch:
        proj = os.path.basename(os.path.dirname(tp))
        sid = os.path.splitext(os.path.basename(tp))[0]
        payload = json.dumps(
            {"transcript_path": tp, "cwd": proj, "session_id": sid, "hook_event_name": "SessionEnd"}
        )
        try:
            r = subprocess.run(
                [sys.executable, HOOK], input=payload, text=True, env=env, timeout=180
            )
            done += 1 if r.returncode == 0 else 0
            print(f"[collect] {'ok' if r.returncode == 0 else 'fail'}  {proj}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[collect] timeout  {proj}", flush=True)

    try:
        req = urllib.request.Request(f"{DRUDGE_URL}/sync", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=900) as resp:
            print("[collect] sync ok", flush=True)
    except Exception as e:
        print(f"[collect] sync 실패(무시): {e}", flush=True)
    print(f"[collect] done={done}/{len(batch)}  남은={len(todo) - done}", flush=True)


if __name__ == "__main__":
    main()
    sys.exit(0)
