#!/usr/bin/env python3
"""The morning card's advice prompt and evidence-grounding parser — no Slack, no engine.

build_advice_prompt shapes one candidate's task for the local model; parse_advised is the
boundary that turns its structured JSON into a grounded Advice, a legitimate NotWorth
refusal, or an Ungrounded value — never an exception, since one weak candidate must not stop
the advise loop over the rest of the queue. A quote that does not appear in its cited note's
own text is dropped, never trusted."""

from __future__ import annotations

import json

from card_types import (
    DISPLAY_LANGS,
    MIN_ADVICE_CHARS,
    MIN_BOTTLENECK_CHARS,
    MIN_QUOTE_CHARS,
    Advice,
    AdvisedInput,
    Evidence,
    NotWorth,
    NotWorthInput,
    RegisterName,
    Ungrounded,
)
from pydantic import ValidationError

#: The advise loop's per-candidate model prompt gets one language-instruction line appended,
#: attached the same way distill_core._build_prompt attaches its lang_instruction — the rest
#: of the prompt (JSON schema, field-format rules) is not a display string and stays as-is.
ADVICE_LANG_INSTRUCTION: dict[str, str] = {
    "ko": "bottleneck 과 advice 는 반드시 한국어 문장으로 써라.",
    "ja": "bottleneck と advice は必ず日本語の文で書け。",
    "en": "Write bottleneck and advice as English sentences.",
}


def resolve_lang(raw: str) -> str:
    """boring.json's note_lang, folded to one of the three languages the card's display
    table (card_i18n.STRINGS) knows. An explicit ko/ja/en passes straight through; anything
    else — 'auto' included — falls back to English, the same way distill_core's own
    lang_instruction dict falls back for an unmapped lang, except a card's title and
    buttons have no transcript to auto-detect a language from, so the fallback here is a
    fixed value rather than distill's 'write in whatever language the transcript is'."""
    return raw if raw in DISPLAY_LANGS else "en"


def _advice_lang_instruction(lang: str) -> str:
    return ADVICE_LANG_INSTRUCTION.get(
        lang, "Write bottleneck and advice in the same language as the past record above."
    )


#: Per-register cap on the text that enters the prompt. The engine answers are already capped,
#: but three of them plus the sources lists still outgrow a small local model's context.
PROMPT_SECTION_BUDGET = 3500


def build_advice_prompt(subject: str, register: RegisterName, hits: list[dict], lang: str) -> str:
    """One candidate's whole task: a subject, its register, and up to three past hits
    (`/search` with `claims`) to ground advice in. The model only quotes and reasons —
    parse_advised is the boundary that checks a quote actually appears in its note."""
    sections = []
    for hit in hits[:3]:
        note = hit.get("source_path", "")
        snippet = str(hit.get("snippet") or "")[:600]
        claims = "\n".join(
            f"  - {c.get('subject', '')} — {c.get('predicate', '')}: {c.get('value', '')}"
            for c in (hit.get("claims") or [])
        )
        # No brackets or other punctuation around the path — a candidate that displayed
        # `[note]` had gemma4 copy the brackets into evidence.note, so every quote from that
        # hit failed to verify (the real note path never carries them). The label is its own
        # line so nothing but the path itself sits after "노트 경로:".
        sections.append(f"노트 경로: {note}\n{snippet}\n{claims}".rstrip())
    hits_text = "\n\n".join(sections) if sections else "(과거 기록 없음)"
    return (
        f"오늘의 후보 주어: {subject!r} (레지스터: {register})\n\n"
        f"과거 기록(검색 결과):\n{hits_text}\n\n"
        "이 주어가 오늘 조언할 가치가 있으면 아래 JSON 하나만 출력해라:\n"
        '{"result": {"kind": "proposal", '
        f'"bottleneck": "병목 한 문장({MIN_BOTTLENECK_CHARS}자 이상)", '
        f'"advice": "오늘 할 것 한 문장({MIN_ADVICE_CHARS}자 이상)", '
        '"evidence": [{"note": "위 노트 경로 줄의 경로 문자열", '
        f'"quote": "그 노트 본문에서 글자 그대로 옮긴 인용({MIN_QUOTE_CHARS}자 이상)"'
        "}]}}\n\n"
        "조언할 가치가 없으면:\n"
        '{"result": {"kind": "not_worth", "reason": "한 문장 이유"}}\n\n'
        "규칙:\n"
        "- 새로운 사실을 지어내지 마라. quote 는 위 과거 기록 본문에서 그대로 옮겨라 — "
        "지어내면 그 근거는 버려진다.\n"
        '- note 는 "노트 경로: " 뒤에 오는 문자열 그대로다. 대괄호나 다른 기호를 붙이지 마라. '
        '예: 노트 경로: /vault/wiki/wiki-0900.md 이면 "note": "/vault/wiki/wiki-0900.md" 다.\n'
        "- 근거가 하나도 검증되지 않을 것 같으면 처음부터 not_worth 를 골라라.\n"
        f"- {_advice_lang_instruction(lang)}\n"
    )


def _find_quote_line(text: str, quote: str) -> int | None:
    """The 1-based line where `quote` verbatim-appears in `text`, whitespace runs collapsed
    (the model retypes rather than copy-pastes, so a newline-for-space difference is not a
    fabrication) — or None when no line contains it."""

    def norm(s: str) -> str:
        return " ".join(s.split())

    target = norm(quote)
    if not target:
        return None
    for i, line in enumerate(text.splitlines(), start=1):
        if target in norm(line):
            return i
    return None


def parse_advised(llm_json: str, note_texts: dict[str, str]) -> Advice | NotWorth | Ungrounded:
    """One candidate's structured JSON in, a grounded value out — never an exception: a weak
    candidate is a fact about that candidate, not a reason to stop the advise loop over the
    rest of the queue. Evidence whose quote does not verify against its own note's text
    (looked up in `note_texts`, keyed by note path) is dropped; a proposal left with none is
    Ungrounded (실험 1, wiki-1064: 좌표 없는 조언은 타입이 거부). NotWorth passes through."""
    try:
        data = json.loads(llm_json)
    except json.JSONDecodeError as e:
        return Ungrounded(reason=f"not JSON: {e}")
    try:
        advised = AdvisedInput.model_validate(data).result
    except ValidationError as e:
        return Ungrounded(reason=f"schema: {e}")
    if isinstance(advised, NotWorthInput):
        return NotWorth(reason=advised.reason)
    grounded: list[Evidence] = []
    for item in advised.evidence:
        text = note_texts.get(item.note)
        if text is None:
            continue
        line = _find_quote_line(text, item.quote)
        if line is None:
            continue
        grounded.append(Evidence(note=item.note, quote=item.quote, line=line))
    if not grounded:
        return Ungrounded(reason="no evidence verified against note text")
    return Advice(bottleneck=advised.bottleneck, advice=advised.advice, evidence=grounded)
