"""로컬 OpenAI 호환 채팅 엔드포인트로 나가는 쪽 — 증류가 쓰는 호출 한 벌(urllib, 표준 라이브러리만)."""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

from ohmyboring import config as omb_env
from ohmyboring.result import Err, Ok

# LLM connection resolves through omb_env (SSOT): env override (BORING_LLM_*) → boring.json
# llm block → default, with host.docker.internal → localhost rewrite on the host. Distillation
# alone may point elsewhere via BORING_DISTILL_{BASE_URL,MODEL,API_KEY}; empty → the values above.
LLM_BASE_URL = omb_env.distill_base_url()
LLM_MODEL = omb_env.distill_model()
LLM_API_KEY = omb_env.distill_api_key()


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


# Superset of what the distill callers read from call_llm: draft/repair → title, body, tags, tools,
# concepts, claims (nodes/note.py); the skip verdict {"skip": true} (prompts/draft.py); polish → body
# only (polish.py). No `required` at top level: polish and skip answers carry a subset.
DISTILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
        "concepts": {"type": "array", "items": {"type": "string"}},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "value": {"type": "string"},
                    "kind": {"type": "string"},
                    "confidence": {"type": "string"},
                },
                "required": ["subject", "predicate", "value", "kind", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

_SYSTEM_PROMPT = "You emit only compact, valid JSON. No prose outside JSON."

# Parsed once at import (boundary); an unknown BORING_DISTILL_PROVIDER stays an Err and call_llm
# reports it on every call instead of silently running the local path.
DISTILL_PROVIDER = omb_env.distill_provider()


# call_llm returns only the parsed JSON and is called from four nodes, so the run's spend is summed
# here instead of widening every return type. distill_and_remember resets it at the start of a run;
# the openai_compat path never adds to it, so its events carry 0 (no usage to report, none invented).
_run_usage = {"llm_calls": 0, "llm_input_tokens": 0, "llm_output_tokens": 0}


def reset_run_usage() -> None:
    for key in _run_usage:
        _run_usage[key] = 0


def run_usage_fields() -> dict[str, int]:
    return dict(_run_usage)


def _anthropic_text(prompt: str) -> str | None:
    """Anthropic Messages API (official SDK) → the reply text, or None (failure announced on stderr)."""
    import anthropic  # lazy: the local path does not need the SDK

    client_args: dict[str, Any] = {"api_key": LLM_API_KEY}
    explicit_base_url = omb_env.distill_base_url_explicit()
    if explicit_base_url:
        client_args["base_url"] = explicit_base_url
    try:
        client = anthropic.Anthropic(**client_args)
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {"type": "json_schema", "schema": DISTILL_SCHEMA},
                "effort": "low",
            },
        )
    except anthropic.BadRequestError as e:
        print(f"[distill-session] LLM call rejected (400): {e}", file=sys.stderr)
        return None
    except anthropic.RateLimitError as e:
        print(f"[distill-session] LLM rate limited (429): {e}", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"[distill-session] LLM call failed (HTTP {e.status_code}): {e}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        print(f"[distill-session] LLM connection failed: {e}", file=sys.stderr)
        return None

    _run_usage["llm_calls"] += 1
    _run_usage["llm_input_tokens"] += resp.usage.input_tokens
    _run_usage["llm_output_tokens"] += resp.usage.output_tokens
    print(
        f"[distill-session] usage input_tokens={resp.usage.input_tokens} "
        f"output_tokens={resp.usage.output_tokens}",
        file=sys.stderr,
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        print(f"[distill-session] LLM stopped without a usable answer: {resp.stop_reason}", file=sys.stderr)
        return None
    return next((b.text for b in resp.content if b.type == "text"), None)


def _openai_compat_text(prompt: str) -> str | None:
    """Local OpenAI-compatible chat endpoint → the reply text, or None (failure announced on stderr)."""
    headers = {"content-type": "application/json"}
    if LLM_API_KEY:
        headers["authorization"] = f"Bearer {LLM_API_KEY}"
    payload = json.dumps(
        {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
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
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"[distill-session] unexpected LLM response shape: {data}", file=sys.stderr)
        return None


def call_llm(prompt: str) -> Any:
    """Ask the configured distill provider and return the parsed JSON, or None."""
    match DISTILL_PROVIDER:
        case Err(reason):
            print(f"[distill-session] {reason}", file=sys.stderr)
            return None
        case Ok(omb_env.AnthropicProvider()):
            message = _anthropic_text(prompt)
        case Ok(omb_env.OpenAICompatProvider()):
            message = _openai_compat_text(prompt)
    if message is None:
        return None

    parsed = extract_json(message)
    if parsed is None:
        print(
            f"[distill-session] failed to parse LLM output as JSON:\n{message[:500]}",
            file=sys.stderr,
        )
    return parsed
