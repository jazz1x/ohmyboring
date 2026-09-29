"""LLM 이 돌려준 JSON 을 노트 한 벌로 다듬는다 — 본문 정리, 목록 상한, 주장 종류 정규화."""

from __future__ import annotations

import re
import sys

from ohmyboring.distill import settings
from ohmyboring.distill.resolution import ALLOWED_CLAIM_KINDS, body_survives_storage_normalize


def strip_trailing_metadata(body):
    """Remove tags/tools/concepts blocks that some LLMs append at the end of the body.

    Even with strict prompts, gemma occasionally emits a trailing block like:

        tags: [...]
        tools: [...]
        concepts: [...]

    This sanitizes the body so metadata lives only in the frontmatter.
    """
    lines = body.splitlines(keepends=True)
    i = len(lines)
    saw_metadata = False
    while i > 0:
        line = lines[i - 1]
        stripped = line.strip()
        if stripped.startswith(("tags:", "tools:", "concepts:")):
            saw_metadata = True
            i -= 1
            continue
        if stripped == "" and saw_metadata:
            i -= 1
            continue
        break
    return "".join(lines[:i]).rstrip()


def normalize_claim_kind(kind, subject, predicate, value):
    """Normalize obvious semantic kind labels before the verifier checks required kinds."""
    raw = (kind or "fact").strip().lower()
    if raw not in ALLOWED_CLAIM_KINDS:
        raw = "fact"
    haystack = " ".join((subject, predicate, value)).lower()
    if raw == "fact":
        semantic_kinds = {
            "decision": ("decision", "decided", "choose", "chosen", "결정", "선택", "판단", "採用", "決定"),
            "next": ("next-step", "next step", "follow-up", "todo", "다음", "남은", "후속", "残件", "次"),
            "risk": ("risk", "리스크", "위험", "懸念", "リスク"),
            "blocked": ("blocked", "blocker", "blocked-by", "막힘", "차단", "ブロック"),
        }
        for semantic_kind, signals in semantic_kinds.items():
            if any(signal in haystack for signal in signals):
                return semantic_kind
    return raw


def _normalized_body(body):
    # gemma sometimes double-escapes newlines (emits "\\n" in the JSON), so json.loads yields a literal
    # backslash-n in the body instead of a real line break → markdown renders as one run-on line. It often
    # MIXES literal "\\n" with a few real breaks, so normalize whenever any literal "\\n" is present.
    n_lit = body.count("\\n")
    if "\\n" in body:
        body = body.replace("\\n", "\n").replace("\\t", "\t")
    # Instrumentation (not a cleanup cycle): a rising count here = the prompt is regressing into
    # double-escaped output. Steady 0 means distillation is healthy; investigate if it grows.
    body_meta = strip_trailing_metadata(body)
    n_meta = body != body_meta  # True if a trailing tags/tools/concepts block had to be stripped
    if n_lit or n_meta:
        print(
            f"[distill-session] body normalized: {n_lit} literal newlines"
            f"{', trailing-metadata stripped' if n_meta else ''} — watch for prompt regression",
            file=sys.stderr,
        )
    return body_meta


def _warn_title_language(title):
    # Language regression signal: note_lang=ko but the title came back with no Korean at all → the
    # model copied an all-English title (usually triggered by [TICKET-ID] prefixes). Logged, not
    # auto-fixed — a rising rate means the title prompt needs another nudge.
    if settings.NOTE_LANG == "ko" and title and not re.search(r"[가-힣]", title):
        print(
            f"[distill-session] title not Korean despite note_lang=ko: {title!r} — watch for prompt regression",
            file=sys.stderr,
        )


def _warn_storage_collapse(body):
    # Precondition, not normalization: predicts whether drudge's normalize_body (SSOT —
    # drudge/src/vault/remember.rs) will collapse this body to "" at the write gate, e.g. every
    # heading present but none with content beneath it. Flagged here, early, so the resolution
    # gate that runs right after prepare_note sees the same failure and the existing repair pass
    # gets a chance to fix it — instead of the note passing the gate and dying silently inside
    # the remember() HTTP call. This does NOT rewrite body; only drudge normalizes for storage.
    if not body_survives_storage_normalize(body):
        print(
            "[distill-session] body would collapse to empty at storage normalize "
            "(headings with no content) — resolution gate will flag this for repair",
            file=sys.stderr,
        )


def _strings(parsed, key, limit):
    return [t.strip() for t in parsed.get(key, []) if isinstance(t, str) and t.strip()][:limit]


def _claims(parsed):
    claims = []
    for c in parsed.get("claims", []):
        if isinstance(c, dict) and c.get("subject") and c.get("predicate") and c.get("value"):
            subject = str(c["subject"]).strip()
            predicate = str(c["predicate"]).strip()
            value = str(c["value"]).strip()
            claims.append(
                {
                    "subject": subject,
                    "predicate": predicate,
                    "value": value,
                    "kind": normalize_claim_kind(
                        str(c.get("kind", "fact")).strip(), subject, predicate, value
                    ),
                    "confidence": str(c.get("confidence", "certain")).strip() or "certain",
                }
            )
    return claims


def prepare_note(parsed):
    title = parsed.get("title", "").strip()
    body = _normalized_body(parsed.get("body", "").strip())
    _warn_title_language(title)
    if not title or not body:
        print("[distill-session] missing title/body in LLM output", file=sys.stderr)
        return None
    _warn_storage_collapse(body)
    return {
        "title": title,
        "body": body,
        "tags": _strings(parsed, "tags", 6),
        "tools": _strings(parsed, "tools", 8),
        "concepts": _strings(parsed, "concepts", 8),
        "claims": _claims(parsed),
    }


def prepare(state):
    return {"note": prepare_note(state["parsed"])}


def repair_prepare(state):
    return {"repaired_note": prepare_note(state["repaired"])}
