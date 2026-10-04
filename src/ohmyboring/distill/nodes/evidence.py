"""LLM 이 버린 구체 증거 토큰을 원문 발췌로 되살린다."""

from __future__ import annotations

import re

from ohmyboring.distill import settings
from ohmyboring.distill.prompts.sections import localized_section_headers
from ohmyboring.distill.resolution import verify_note_resolution


def ensure_required_evidence_tokens(note, transcript, resolution):
    """Preserve short transcript excerpts when the LLM drops required concrete evidence tokens."""
    report = verify_note_resolution(
        {"title": note["title"], "body": note["body"], "claims": note["claims"]},
        transcript=transcript,
        resolution=resolution,
    )
    missing_token_rule = next(
        (item for item in report.missing if item.startswith("evidence-tokens:min:")), ""
    )
    if not missing_token_rule:
        return note
    required = int(missing_token_rule.rsplit(":", 1)[1])
    missing_tokens = [
        token for token in report.evidence_tokens_seen if token not in report.evidence_tokens_kept
    ]
    if not missing_tokens:
        return note
    needed = max(0, required - len(report.evidence_tokens_kept))
    snippets = _snippets_for(transcript, missing_tokens, needed)
    if not snippets:
        return note
    note["body"] = _append_evidence_snippets(note["body"], snippets)
    return note


def _snippets_for(transcript, missing_tokens, needed):
    snippets = []
    for token in sorted(missing_tokens, key=_evidence_token_rank):
        snippet = _transcript_excerpt_for_token(transcript, token)
        if snippet and snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= needed:
            break
    return snippets


def _evidence_token_rank(token):
    if not token.isdigit():
        return (0, token)
    try:
        value = int(token)
    except ValueError:
        return (1, token)
    if value >= 10:
        return (1, token)
    return (2, token)


def _transcript_excerpt_for_token(transcript, token):
    match = re.search(_evidence_token_pattern(token), transcript, re.IGNORECASE)
    if match:
        idx = match.start()
        return _transcript_excerpt_at(transcript, idx)
    haystack = transcript.lower()
    needle = token.lower()
    idx = haystack.find(needle)
    if idx < 0:
        return ""
    return _transcript_excerpt_at(transcript, idx)


def _evidence_token_pattern(token):
    pr_match = re.fullmatch(r"pr#(\d+)", token, re.IGNORECASE)
    if pr_match:
        return r"\bPR\s*#?\s*" + re.escape(pr_match.group(1)) + r"\b"
    issue_match = re.fullmatch(r"#(\d+)", token)
    if issue_match:
        return r"\B#\s*" + re.escape(issue_match.group(1)) + r"\b"
    return re.escape(token)


def _transcript_excerpt_at(transcript, idx):
    start = max(0, idx - 120)
    end = min(len(transcript), idx + 180)
    excerpt = re.sub(r"\s+", " ", transcript[start:end]).strip()
    return excerpt[:260].strip()


def _append_evidence_snippets(body, snippets):
    label = {
        "ko": "원문 근거 발췌",
        "ja": "原文根拠抜粋",
        "en": "Original evidence excerpt",
    }.get(settings.NOTE_LANG, "Original evidence excerpt")
    addition = "\n".join(f"- {label}: {snippet}" for snippet in snippets)
    if re.search(r"(?im)^## .*(evidence|basis|근거|검증|根拠|検証)", body):
        return body.rstrip() + "\n" + addition + "\n"
    header = localized_section_headers(settings.NOTE_LANG)["evidence"]
    return body.rstrip() + f"\n\n## {header}\n" + addition + "\n"
