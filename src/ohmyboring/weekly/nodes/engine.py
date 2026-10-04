"""엔진 /weekly 합성 — 일간이 모자랄 때만 부른다. 실패는 값으로 돌려 stdout 알림이 된다."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ohmyboring.briefing.weekly_render import EMPTY_MESSAGE
from ohmyboring.weekly.stamp import header


def engine(state):
    req = urllib.request.Request(
        f"{state['door_url']}/weekly",
        data=b"{}",
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"stdout": header(state["now"], f"⚠️ ohmyboring(RAG) 응답 없음 — 엔진 가동 확인 필요. ({e})")}
    except json.JSONDecodeError:
        return {"stdout": header(state["now"], "⚠️ 응답 파싱 실패 — ohmyboring 점검 필요.")}

    answer = (data.get("answer") or "").strip()
    if not answer:
        return {"stdout": header(state["now"], EMPTY_MESSAGE)}
    return {"answer": answer, "sources": data.get("sources") or []}
