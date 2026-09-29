"""로컬 OpenAI 호환 채팅 엔드포인트로 나가는 쪽 — 증류가 쓰는 호출 한 벌(urllib, 표준 라이브러리만)."""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

from ohmyboring import config as omb_env

# LLM connection resolves through omb_env (SSOT): env override (BORING_LLM_*) → boring.json
# llm block → default, with host.docker.internal → localhost rewrite on the host.
LLM_BASE_URL = omb_env.llm_base_url()
LLM_MODEL = omb_env.llm_model()
LLM_API_KEY = omb_env.llm_api_key()


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM response that may wrap it in markdown or append prose.

    Uses json.JSONDecoder.raw_decode so trailing garbage after the first valid JSON object is ignored.
    """
    text = text.strip()
    # Remove markdown code fences if present.
    if text.startswith("```"):
        text = text[text.find("\n") + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    text = text.strip()
    # Find the first JSON object start; raw_decode will find its matching end.
    start = text.find("{")
    if start == -1:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(text, start)
        return obj
    except json.JSONDecodeError:
        return None


def call_llm(prompt: str) -> Any:
    """Call the local OpenAI-compatible chat endpoint and return the parsed JSON, or None."""
    headers = {"content-type": "application/json"}
    if LLM_API_KEY:
        headers["authorization"] = f"Bearer {LLM_API_KEY}"
    payload = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You emit only compact, valid JSON. No prose outside JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "stream": False,
            # Force valid JSON (Ollama / OpenAI-compatible structured output) so the model can't wrap
            # the object in prose/markdown fences or emit unparseable JSON. Body newlines then come back
            # as proper \n escapes that json.loads decodes to real line breaks (not literal backslash-n).
            "response_format": {"type": "json_object"},
            # Disable the model's reasoning/thinking trace. gemma4:12b is a thinking variant — WITH it a
            # full distill takes ~188-262s, which blows past the 120s urlopen timeout below → the call
            # returns None and the session is SILENTLY dropped (no note). `reasoning_effort:"none"` is the
            # OpenAI-standard knob Ollama /v1 honors (≈0.6s vs 8s; same knob drudge/src/llm.rs uses).
            # Quality is unaffected — the reasoning lives in a separate field, never in the note body.
            "reasoning_effort": "none",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[distill-session] LLM call failed: {e}", file=sys.stderr)
        return None

    try:
        message = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"[distill-session] unexpected LLM response shape: {data}", file=sys.stderr)
        return None

    parsed = extract_json(message)
    if parsed is None:
        print(
            f"[distill-session] failed to parse LLM output as JSON:\n{message[:500]}",
            file=sys.stderr,
        )
    return parsed
