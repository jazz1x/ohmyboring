#!/usr/bin/env python3
"""Claude Code SessionEnd 훅용 스크립트 — 세션을 증류해 개인 메모리 노트로 저장.

로컬 Ollama(gemma4:12b, think=false)로 핵심 배움/결정/사실만 뽑아 vault raw 레이어
(~/oh-my-boring/vault/raw)에 기록 → `hermes vault compile` 이 wiki 로 큐레이션 →
ingest 가 흡수. raw 트랜스크립트 통째 적재 금지(증류만).
실패·짧은 세션·저장가치 없음이면 조용히 skip. 절대 세션 종료를 막지 않음(항상 exit 0).

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
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
HERMES_RS = os.environ.get("HERMES_RS_URL", "http://localhost:7700")
MODEL = "gemma4:12b"  # 엔진과 통일. think=false 로 추론모드 차단(비대상 모델은 무시)
MAX_CHARS = 40000
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


# ── 시크릿 스크럽 (가벼움) ──────────────────────────────────────────────────
# 개인 로컬이라 빡센 redact 불필요. 단 vault/ 는 git 추적 → 공유 시 누수구.
# 그 한 경계만 막는다: 알려진 토큰 패턴을 ‹REDACTED› 로 치환 후 파일에 쓴다.
_SECRET_RE = re.compile(
    "|".join(
        f"(?:{p})"
        for p in (
            r"xox[baprs]-[0-9A-Za-z-]{10,}",          # Slack bot/user/...
            r"xapp-[0-9A-Za-z-]{10,}",                # Slack app-level
            r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}",        # OpenAI/Anthropic
            r"AKIA[0-9A-Z]{16}",                      # AWS access key
            r"gh[pousr]_[A-Za-z0-9]{30,}",            # GitHub
            r"AIza[0-9A-Za-z_-]{35}",                 # Google API
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",    # PEM
            r"(?i:api[_-]?key|secret|token|password|passwd|bearer)[\"' ]*[:=][\"' ]*[A-Za-z0-9._/+-]{12,}",
        )
    )
)


def _redact(text):
    """vault(git 추적) 진입 전 시크릿 스크럽 — 누수구(git/공유)만 막는 가벼운 게이트."""
    return _SECRET_RE.sub("‹REDACTED›", text)


def distill(text):
    prompt = (
        "아래는 내(사용자)가 Claude와 함께 작업한 세션 기록이다. "
        "미래의 내가 '전에 이거 어떻게 했더라'를 다시 참고할 수 있게, "
        "**문제해결 서사**를 기록해라. 다음 틀로 한국어 markdown 작성:\n"
        "  🎯 **풀던 문제** — 무엇을 하려 했나 (1줄)\n"
        "  🧪 **시도/실패** — 시도한 것들, 특히 안 됐던 것과 *왜* 안 됐는지\n"
        "  🚧 **포기/우회** — 버린 길과 이유 (다음에 또 헛짚지 않게)\n"
        "  ✅ **통한 해결** — 결국 뭐가 먹혔나 (구체적으로: 명령·설정·근본원인)\n"
        "  🔄 **미완/다음** — 하다 만 것, 이어서 할 일\n"
        "해당 없는 항목은 생략. 설정파일 덤프·문서 인용·스키마·일반 잡담은 무시하라 "
        "(그건 '서사'가 아니다). "
        "실제 시도-실패-해결 흐름이 전혀 없으면 첫 줄에 'SKIP'만.\n\n"
        "출력 첫 줄은 반드시 'KEEP' 또는 'SKIP' 한 단어. KEEP이면 다음 줄부터 노트 본문.\n\n"
        "=== 세션 ===\n" + text
    )
    body = json.dumps(
        {"model": MODEL, "prompt": prompt, "stream": False, "think": False, "keep_alive": "5m"}
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("response", "").strip()


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
    if len(text) < 500:  # 너무 짧은 세션은 skip
        return
    # 문제해결 서사는 세션 전체에 걸침 → 꼬리만 자르면 도입부(문제설정)를 잃음.
    # 길면 앞 1/3 + 뒤 2/3 를 살려 '문제→해결' 양끝을 모두 보존.
    if len(text) > MAX_CHARS:
        head = text[: MAX_CHARS // 3]
        tail = text[-(MAX_CHARS - MAX_CHARS // 3):]
        text = head + "\n…(중략)…\n" + tail
    note = distill(text)
    # 첫 줄 게이트: KEEP/SKIP 한 단어로 모델이 명시 판단. SKIP 또는 비정상이면 저장 안 함.
    lines = note.splitlines()
    head_line = lines[0].strip().upper() if lines else ""
    if not head_line.startswith("KEEP"):
        return
    body = "\n".join(lines[1:]).strip()
    if len(body) < 40:  # KEEP인데 알맹이 없으면 버림
        return
    body = _redact(body)  # vault(git) 진입 전 시크릿 스크럽
    # 격리 없음 — 개인·회사 세션 경험 모두 같은 raw/ 에. 구분은 origin 태그로만.
    out_dir = RAW_DIR
    os.makedirs(out_dir, exist_ok=True)
    # 파일명 = 세션ID 키 → 같은 세션을 주기적으로 재증류해도 덮어씀(중복 노트 방지).
    key = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:16] or datetime.datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )
    fp = os.path.join(out_dir, f"session-{key}.md")
    origin = "company" if is_company else "personal"
    phase = "종료" if is_final else "진행중"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(f"# 세션 노트 — {datetime.date.today().isoformat()}\n")
        f.write(f"> 자동 증류 (Claude Code · {phase}) · origin: {origin} · cwd: {cwd}\n\n")
        f.write(body + "\n")
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
