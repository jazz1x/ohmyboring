#!/usr/bin/env python3
"""「이름 맞추기」의 판정 자리 — 아침 카드가 그리기 전에 에이전트가 하루 한 번 스스로
후보 묶음을 판정하는 일을 준비한다.

build_prompt shapes the one question a judgment call asks: the group's canon name, its
spellings, its row/note counts, and the owner's past 보류·거절 on this subject. parse is the
boundary that turns the completion into a RepairJudgeAnswer — never an exception, since a
model that cannot answer is a fact about the group (a repair_judge_failed 사건), not a
crash: 판정이 없는 묶음이 날것으로 카드에 오르는 변이를 막는 문지방이 parse 가 혼자 선다.
pick is the day's queue: 문이 주는 행 수 순 그대로, 이미 같은 (subject, 정렬된 variants)
판정이 있는 묶음은 걷어내고, 하루 상한(DAILY_JUDGE_CAP)만 남긴다. variants 가 바뀐 묶음은
다른 묶음이라 다시 판정한다.

The run itself (main) goes through the door like every other consumer: GET
/repairs/split-subjects?limit=50 (the door's own 상한), reads repair_judged · repair_reviewed
from /events, judges at most 20 groups, and writes one 사건 per group — repair_judged
(subject, variants, verdict, reason, judge='agent:repair-judge') or repair_judge_failed
(subject, variants, reason) — through card_effects._live_record, the one 사건-write path.
It never touches POST /repairs/split-subjects: judging and merging stay separate runs, and
the owner keeps the merge buttons. This module must stay importable inside the hermes venv,
so its live reads are local urllib copies (the same reason card_delegate carries its own
_live_events) — only the model seam imports langchain, lazily, exactly like card.py's
make_propose.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))

import card_types  # noqa: E402
from card_effects import _live_record  # noqa: E402
from card_types import RepairJudgeFailed, RepairJudgment  # noqa: E402

from ohmyboring import config as omb_env  # noqa: E402

#: 하루 판정 상한 — 판정 세션 결정(2026-10-03): 한 번의 실행이 모델을 부를 수 있는 최대
#: 횟수. 문이 주는 순서(행 수 내림차순)대로 훑고 상한에 걸리면 나머지는 다음 날.
DAILY_JUDGE_CAP = 20

#: 문 한 번에 주는 묶음 상한 — 문의 _MAX_REPAIR_LIMIT(50) 그 자체.
DOOR_GROUP_LIMIT = 50

#: 판정을 읽는 창 — 한 달. 카드가 판정을 쓰는 창(card_live.REPAIR_JUDGED_WINDOW_HOURS)과
#: 같다: 쓰는 쪽과 걸러 넘기는 쪽이 같은 창을 봐야 낡은 판정을 다시 내는 일이 없다.
JUDGED_WINDOW_HOURS = 24 * 30

#: 오너의 보류·거절 기록을 읽는 창 — 이레, 판정 세션이 정한 묶음 보류 창과 같은
#: card_live.REVIEW_DEFER_WINDOW_HOURS. 기록은 프롬프트 입력이지 판정 스킵 규칙이 아니다.
REVIEW_WINDOW_HOURS = 168

#: 판정 이유 한 줄의 상한 — DELEGATE_REASON_MAX 와 같은 400. 사건 한 줄이 카드 줄이 되는
#: 길이라 위임 판정과 같은 캡을 쓴다.
JUDGE_REASON_MAX = 400

#: The judge seam's model — the morning card's own CARD_MODEL vocabulary, unchanged.
DEFAULT_MODEL = os.environ.get("CARD_MODEL") or "gemma4:12b"

_EVENTS_TIMEOUT = float(os.environ.get("CARD_ENGINE_TIMEOUT") or "30")

_EVENTS_LIMIT = 1000

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

#: The key both the judge and the card compute for one group — (subject, 정렬된 variants).
#: 정렬이 키의 일부다: variants 가 바뀐 묶음은 다른 묶음이어서 다시 판정한다.
GroupKey = tuple[str, tuple[str, ...]]


def build_prompt(group: dict[str, Any], past_reviews: list[dict[str, Any]]) -> str:
    """One judgment call's whole question: the group's canon name, every spelling, its
    row/note counts, and the owner's past calls on this subject (보류·거절 — a rejection the
    model should weigh, never override). The prompt is Korean, the same language
    card_delegate builds its prompt in — the local model answers with one of three fixed
    verdicts, never inventing a fourth."""
    variants = ", ".join(str(v) for v in group["variants"])
    lines = [
        "이름 맞추기 후보 하나를 판정한다. 이 묶음이 같은 이름의 갈라진 철자인지, 아니면 이름이 아닌 흔한 말인지 고른다.",
        "",
        f"정규화 이름: {group['subject']}",
        f"철자 목록: {variants}",
        f"행 수: {group.get('rows')} · 노트 수: {group.get('notes')}",
    ]
    if past_reviews:
        lines.append("")
        lines.append("소유자가 이 묶음에 남긴 기록 (보류·거절):")
        for review in past_reviews:
            label = {"defer": "보류", "drop": "거절"}.get(
                str(review.get("choice")), str(review.get("choice"))
            )
            lines.append(f"- {label} (카드 {review.get('card_ts')}, 줄 {review.get('idx')})")
    lines += [
        "",
        "판정 셋:",
        "- same_name: 갈라진 철자가 전부 같은 이름이다 — 합쳐도 된다.",
        "- generic: 이름이 아니라 흔한 말이다 (예: 다음 단계, 메인 페이지, 확인) — 합치면 안 된다.",
        "- unsure: 위 둘 중 무엇인지 못 가른다.",
        "",
        "아래 JSON 하나만 출력하라:",
        '{"verdict": "same_name", "reason": "왜 그렇게 판정하는지 한 문장"}',
        "",
        "규칙:",
        "- 새로운 사실을 지어내지 마라. reason 은 철자 목록과 행 수에 근거해 써라.",
        f"- reason 은 한 문장, {JUDGE_REASON_MAX}자 이내로 써라.",
        '- verdict 는 "' + " · ".join(card_types.REPAIR_JUDGE_VERDICTS) + '" 셋 중 하나만 써라.',
    ]
    return "\n".join(lines)


def parse(raw: str, subject: str, variants: list[str]) -> card_types.RepairJudgeAnswer:
    """One model completion in, the judgment — or its recorded absence — out. JSON이 아니거나
    verdict 어휘가 아니거나 이유가 없는 답은 전부 RepairJudgeFailed: 모델이 못 답한 사실을
    사건으로 남길 뿐 판정을 찍지 않는다(조용히 같은 이름으로 도장 찍는 변이의 문지방)."""
    text = (raw or "").strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return RepairJudgeFailed(subject=subject, variants=variants, reason="모델 답이 JSON 이 아니다")
    if not isinstance(data, dict):
        return RepairJudgeFailed(subject=subject, variants=variants, reason="모델 답이 JSON 객체가 아니다")
    verdict = data.get("verdict")
    if verdict not in card_types.REPAIR_JUDGE_VERDICTS:
        return RepairJudgeFailed(
            subject=subject, variants=variants, reason=f"모델 판정을 알아듣지 못했다: {verdict!r}"
        )
    reason = " ".join(str(data.get("reason") or "").split())
    if not reason:
        return RepairJudgeFailed(subject=subject, variants=variants, reason="모델이 이유를 남기지 않았다")
    if len(reason) > JUDGE_REASON_MAX:
        reason = f"{reason[: JUDGE_REASON_MAX - 1]}…"
    return RepairJudgment(subject=subject, variants=variants, verdict=verdict, reason=reason)


def pick(
    groups: list[dict[str, Any]],
    judged: Mapping[GroupKey, RepairJudgment],
    cap: int = DAILY_JUDGE_CAP,
) -> list[dict[str, Any]]:
    """The day's queue: 문이 주는 순서(행 수 내림차순) 그대로, 이미 같은 (subject, 정렬된
    variants) 판정이 있는 묶음은 걷어내고 cap개까지만 남긴다. variants 가 바뀐 묶음은 다른
    묶음이므로 다시 판정한다 — 판정은 그 묶음의 철자 목록에 대해 남긴 말이다."""
    out: list[dict[str, Any]] = []
    for group in groups:
        if len(out) >= cap:
            break
        key = (str(group["subject"]), tuple(sorted(str(v) for v in group["variants"])))
        if key in judged:
            continue
        out.append(group)
    return out


def judged_map(entries: list[dict[str, Any]]) -> dict[GroupKey, RepairJudgment]:
    """repair_judged 사건 rows → (subject, 정렬된 variants) 가리키는 최신 판정 하나씩.
    같은 묶음의 판정이 창 안에 여러 개면 observed_at 최신 것 하나만 산다. 모양이 틀린 행은
    ValueError(F5/ROP), 조용히 걸러내지 않는다: 빠진 판정 하나가 generic 묶음이나 중복
    모델 호출로 되돌아오는 문지방이다."""
    out: dict[GroupKey, RepairJudgment] = {}
    newest: dict[GroupKey, str] = {}
    for entry in entries:
        attrs = entry.get("attributes") or {}
        subject = attrs.get("subject")
        variants = attrs.get("variants")
        verdict = attrs.get("verdict")
        reason = attrs.get("reason")
        observed_at = entry.get("observed_at")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(variants, list)
            or not variants
            or not all(isinstance(v, str) for v in variants)
            or verdict not in card_types.REPAIR_JUDGE_VERDICTS
            or not isinstance(reason, str)
            or not reason
            or not isinstance(observed_at, str)
        ):
            raise ValueError(f"malformed repair_judged row: {attrs!r}")
        key = (subject, tuple(sorted(variants)))
        if key not in newest or observed_at >= newest[key]:
            newest[key] = observed_at
            out[key] = RepairJudgment(
                subject=subject, variants=sorted(variants), verdict=verdict, reason=reason
            )
    return out


def reviews_by_subject(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """repair_reviewed 사건 rows → subject 가리키는 보류·거절 기록들. 판정 프롬프트의 입력.
    subject 가 없는 행은 ValueError — _held_repair_subjects 가 세우던 문지방 그대로."""
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        attrs = entry.get("attributes") or {}
        subject = attrs.get("subject")
        if not isinstance(subject, str) or not subject:
            raise ValueError(f"malformed repair_reviewed row: {entry!r}")
        out.setdefault(subject, []).append(attrs)
    return out


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def run(
    groups: list[dict[str, Any]],
    judged: Mapping[GroupKey, RepairJudgment],
    reviews: Mapping[str, list[dict[str, Any]]],
    invoke: Callable[[str], str],
    record: Callable[[str, dict[str, Any]], None],
    cap: int = DAILY_JUDGE_CAP,
) -> tuple[int, int]:
    """One judge run's whole skeleton: 고르기 → 한 묶음씩 프롬프트·호출·접기 → 사건 한 줄씩.
    (judged_n, failed_n) — 판정 세기와 실패 세기는 나뉜다: 실패는 판정이 아니니 다음 날 그
    묶음을 다시 본다. 모델 호출이 죽어도 그 묶음만 사건으로 남고 줄은 이어진다 — 실패가
    기록되는 한 조용하지 않다."""
    judged_n = 0
    failed_n = 0
    for group in pick(groups, judged, cap):
        subject = str(group["subject"])
        variants = sorted(str(v) for v in group["variants"])
        try:
            raw = invoke(build_prompt(group, reviews.get(subject, [])))
        except Exception as e:  # noqa: BLE001 — 사건이 기록이니 조용한 삼키기가 아니다
            answer: card_types.RepairJudgeAnswer = RepairJudgeFailed(
                subject=subject,
                variants=variants,
                reason=_one_line(f"모델 호출 실패: {e}")[:JUDGE_REASON_MAX],
            )
        else:
            answer = parse(raw, subject, variants)
        if isinstance(answer, RepairJudgeFailed):
            record("repair_judge_failed", answer.model_dump())
            failed_n += 1
        else:
            record("repair_judged", answer.model_dump())
            judged_n += 1
    return judged_n, failed_n


def _live_events(event_name: str, since_hours: float) -> list[dict[str, Any]]:
    """GET /events one name at a time — card_live._live_events 와 같은 문지방(죽은 엔진·
    maybe_truncated 는 OSError, 빈 목록이 아니다)을 이 모듈이 제자로 가져온 사본: 판정
    실행은 카드처럼 자기 역사를 못 읽으면 돌아서는 게 맞다(읽지 못한 판정을 없는 판정으로
    다시 모델을 부르는 일이 없어야 하니). 이 모듈은 hermes venv 에서도 import 되어야 해서
    card_live 대신 urllib 사본을 둔다."""
    url = (
        f"{omb_env.door_url()}/events?event={urllib.parse.quote(event_name)}"
        f"&since_hours={since_hours}&limit={_EVENTS_LIMIT}"
    )
    try:
        with urllib.request.urlopen(url, timeout=_EVENTS_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise OSError(f"{url}: {e}") from e
    if payload.get("maybe_truncated"):
        raise OSError(
            f"/events?event={event_name}&since_hours={since_hours} maybe_truncated=true — "
            "a clipped judgment window cannot tell judged groups from fresh ones"
        )
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise OSError(f"{url}: entries 가 없다: {payload!r}")
    return entries


def _live_groups(limit: int = DOOR_GROUP_LIMIT) -> list[dict[str, Any]]:
    """문의 GET /repairs/split-subjects — 상한 50개(문 _MAX_REPAIR_LIMIT), 행 수 내림차순.
    죽은 문은 OSError: 판정할 목록을 못 받은 채 아무 묶음도 안 보고 끝나는 것보다 크게
    말하는 편이 낫다."""
    url = f"{omb_env.door_url()}/repairs/split-subjects?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=_EVENTS_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise OSError(f"{url}: {e}") from e
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise OSError(f"{url}: groups 가 없다: {payload!r}")
    return [g for g in groups if isinstance(g, dict)]


def make_judge(model: str = DEFAULT_MODEL) -> Callable[[str], str]:
    """The JSON-mode seam, card.py::make_propose 그대로의 쌍: 한 묶음의 프롬프트를 넣으면
    원문 답이 나온다. format='json' 이 문법을, parse 가 어휘를 지킨다 — 스키마 강제는
    langchain 에 맡기지 않고 실패 값으로 접는 경계(parse)에 둔다. reasoning=False — 생각은
    지연시간일 뿐 고를 것도 보여줄 것도 없다."""
    from langchain_core.runnables import Runnable
    from langchain_ollama import ChatOllama

    llm: Runnable = ChatOllama(model=model, format="json", temperature=0, reasoning=False, num_ctx=16384)

    def invoke(prompt: str) -> str:
        return str(llm.invoke(prompt).content)

    return invoke


def main() -> int:
    if not os.environ.get("BORING_DOOR_URL"):
        print("[repair-judge] BORING_DOOR_URL must be set — 판정은 문을 통해 읽고 쓴다", file=sys.stderr)
        return 2
    try:
        groups = _live_groups()
        judged = judged_map(_live_events("repair_judged", JUDGED_WINDOW_HOURS))
        reviews = reviews_by_subject(_live_events("repair_reviewed", REVIEW_WINDOW_HOURS))
        judged_n, failed_n = run(groups, judged, reviews, make_judge(), _live_record)
    except (ValueError, OSError) as e:
        # A dead door, a clipped events window, a malformed 사건 row — say why in one line
        # and stop: 판정 실행은 합치기를 손대지 않으니, 여기서 멈추는 것은 되돌릴 일이 없다.
        print(f"[repair-judge] 판정 거부: {_one_line(e)}", file=sys.stderr)
        return 3
    print(f"[repair-judge] judged={judged_n} failed={failed_n}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
