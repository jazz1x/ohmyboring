#!/usr/bin/env python3
"""Claude Code SessionEnd 훅용 스크립트 — 세션을 증류해 개인 메모리 노트로 저장.

이 훅은 *호스트 전용* 일만 한다: 트랜스크립트 읽기·텍스트 추출·throttle·세션 mtime 보정.
LLM 증류·KEEP/SKIP 게이트·시크릿 스크럽·raw 노트 포맷은 hermes-rs 엔진(/distill, SSOT)이
담당한다 — 과거 이 스크립트가 ollama.generate/redact 를 재구현하던 중복을 제거(엔진 ollama.rs
가 LLM 호출 SSOT). 추출한 텍스트만 엔진에 POST → 엔진이 ~/oh-my-boring/vault/raw 에 기록.
실패·짧은 세션·엔진 다운이면 조용히 skip. 절대 세션 종료를 막지 않음(항상 exit 0).

설치(영속화)는 사용자가 직접: ~/.claude/settings.json 의 hooks.SessionEnd 에
  {"type":"command","command":"python3 ~/oh-my-boring/hooks/distill-session.py",
   "timeout":130,"async":true}
를 추가.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

RAW_DIR = os.path.expanduser("~/oh-my-boring/vault/raw")
HERMES_RS = os.environ.get("HERMES_RS_URL", "http://localhost:7700")
# 진행중 세션(Stop 훅) 재증류 최소 간격(분). SessionEnd(final)는 throttle 무시.
THROTTLE_MIN = int(os.environ.get("DISTILL_THROTTLE_MIN") or "25")
MARK_DIR = os.path.expanduser("~/.cache/olympus-distill")  # 세션별 마지막 증류 시각


def _trigger_sync():
    """증류 노트를 즉시 RAG에 반영 — hermes-rs /sync(compile→ingest→extract)를
    detached 로 호출. 훅을 블록하지 않는다(distill+sync 동기 체이닝은 130s 초과 위험).
    엔진 다운/실패는 무시 — 4h 스케줄러가 캐치한다(세션 종료를 절대 막지 않음).
    DISTILL_NO_SYNC 설정 시 skip(백필 수집기가 끝에 한 번만 sync 하려고)."""
    if os.environ.get("DISTILL_NO_SYNC"):
        return
    try:
        subprocess.Popen(
            ["curl", "-sS", "-m", "600", "-X", "POST", f"{HERMES_RS}/sync"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # 부모(훅) 종료해도 살아남아 적재 완주
        )
    except Exception:
        pass


def _mark_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"
    return os.path.join(MARK_DIR, f"{safe}.ts")


def _throttled(session_id):
    """이 세션을 최근 THROTTLE_MIN 분 내에 이미 증류했으면 True(skip). 값싼 검사."""
    if not session_id:
        return False
    try:
        age = time.time() - os.path.getmtime(_mark_path(session_id))
        return age < THROTTLE_MIN * 60
    except OSError:
        return False  # 마커 없음 = 첫 증류


def _mark(session_id):
    if not session_id:
        return
    try:
        os.makedirs(MARK_DIR, exist_ok=True)
        with open(_mark_path(session_id), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _session_mtime(path):
    """트랜스크립트 최신 메시지 timestamp → epoch(float). 세션 실제 시각.
    없으면 None(호출측: 파일 mtime 그대로 = 증류 시각)."""
    latest = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    ts = json.loads(line).get("timestamp")
                except Exception:
                    continue
                if not ts:
                    continue
                try:
                    e = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if latest is None or e > latest:
                    latest = e
    except OSError:
        return None
    return latest


def extract(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message") or {}
            role = msg.get("role") or obj.get("type") or ""
            if role not in ("user", "assistant"):
                continue
            c = msg.get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            else:
                t = ""
            t = t.strip()
            if t:
                out.append(f"[{role}] {t}")
    return "\n".join(out)


def post_distill(text, session_id, origin, phase, cwd):
    """추출 텍스트를 hermes-rs /distill 로 POST → 엔진이 증류·스크럽·raw 노트 기록(SSOT).
    반환: {"written": bool, "filename": str|None} 또는 None(엔진 다운/에러 → no-op).
    엔진이 길이 클램프·KEEP/SKIP 게이트·시크릿 스크럽을 모두 수행한다."""
    body = json.dumps(
        {"text": text, "session_id": session_id, "origin": origin, "phase": phase, "cwd": cwd}
    ).encode()
    req = urllib.request.Request(
        f"{HERMES_RS}/distill", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except Exception:
        return None  # 엔진 미가동/에러 — 4h 스케줄러가 캐치(세션 절대 미차단)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tp = data.get("transcript_path") or ""
    if not tp or not os.path.exists(tp):
        return
    session_id = data.get("session_id") or ""
    # SessionEnd=최종 1회(throttle 무시). Stop=진행중 주기 적재(THROTTLE_MIN 간격).
    # 진행중인데 최근 이미 증류했으면 transcript 도 안 읽고 값싸게 빠진다.
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return
    # 세션 '경험'은 격리하지 않고 같은 raw/ 에 쌓는다. origin 은 회수 토글용 태그일 뿐.
    #   DISTILL_COMPANY_CWD(':' 구분) 에 cwd 토큰을 넣으면 그 세션을 origin=company 로 태깅.
    #   기본 빈값 = 회사 개념 미사용(전부 personal). (배제 아님 — 태그만.)
    cwd = data.get("cwd") or ""
    company_tokens = (os.environ.get("DISTILL_COMPANY_CWD") or "").split(":")
    is_company = any(tok and tok in cwd for tok in company_tokens)
    text = extract(tp)
    if len(text) < 500:  # 너무 짧은 세션은 skip (값싼 호스트 선차단 — 헛 POST 방지)
        return
    # 격리 없음 — 개인·회사 세션 경험 모두 같은 raw/ 에. 구분은 origin 태그로만.
    origin = "company" if is_company else "personal"
    phase = "종료" if is_final else "진행중"
    # 엔진(SSOT)이 길이 클램프·LLM 증류·KEEP/SKIP 게이트·시크릿 스크럽·raw 노트 기록을 수행.
    resp = post_distill(text, session_id, origin, phase, cwd)
    if not resp or not resp.get("written"):
        return  # SKIP/짧음(엔진 판정) 또는 엔진 다운 → 마커도 안 남김(다음 Stop 에 재시도)
    filename = resp.get("filename")
    if not filename:
        return
    fp = os.path.join(RAW_DIR, filename)  # 엔진은 파일명만 반환 → 호스트 RAW_DIR 와 조인
    # 최근성 정렬키 교정: 노트 mtime = 세션 실제 시각(트랜스크립트 최신 timestamp).
    # 백필이 옛 세션을 지금 증류해도 mtime=세션시각이라 brief가 가짜최신으로 안 띄움.
    # (compile 이 이 mtime 을 wiki 로 보존 → ingest 가 updated_at 으로 사용.)
    st = _session_mtime(tp)
    if st:
        try:
            os.utime(fp, (st, st))
        except OSError:
            pass
    _mark(session_id)  # throttle 마커 갱신
    _trigger_sync()  # 노트 즉시 적재(detached) — 4h 스케줄러 안 기다림


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 절대 세션을 막지 않음
    sys.exit(0)
