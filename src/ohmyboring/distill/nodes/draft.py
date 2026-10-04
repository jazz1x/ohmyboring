"""첫 증류 호출 — 프롬프트를 짜서 LLM 에 묻고, 한국어 재시도가 필요한지 가른다."""

from __future__ import annotations

import re

from ohmyboring.adapters import llm
from ohmyboring.distill import settings
from ohmyboring.distill.prompts.draft import build_prompt


def draft(state):
    resolution = settings.distill_resolution()
    prompt = build_prompt(state["text"], state["origin"], state["repo"], resolution=resolution)
    parsed = llm.call_llm(prompt)
    # Language retry: note_lang=ko but the title came back with no Korean → the model ignored the
    # language instruction (gemma is weak at language control). Re-ask ONCE with a corrective nudge;
    # keep the retry only if it actually came back in Korean, else fall back to the original.
    wants_language_retry = (
        parsed is not None
        and settings.NOTE_LANG == "ko"
        and bool(parsed.get("title", ""))
        and not re.search(r"[가-힣]", parsed["title"])
    )
    return {
        "resolution": resolution,
        "prompt": prompt,
        "parsed": parsed,
        "wants_language_retry": wants_language_retry,
    }
