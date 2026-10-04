"""해상도 검증에 걸린 노트를 한 번 다시 쓰게 하는 프롬프트."""

from __future__ import annotations

import json

from ohmyboring.distill import settings
from ohmyboring.distill.prompts.sections import body_format_contract
from ohmyboring.distill.resolution import resolution_prompt_contract


def build_repair_prompt(text, origin, repo, note, report, resolution):  # noqa: PLR0913
    """Ask for one repaired JSON note, using only evidence already present in transcript."""
    return (
        "Your previous distillation JSON failed the resolution verifier. "
        "Re-emit ONE complete JSON object with the same schema. No prose outside JSON.\n"
        f"Origin={origin!r}. Repo={repo!r}. Resolution={report.resolution!r}.\n"
        "The previous JSON is a draft, not evidence. The transcript is the only evidence source.\n"
        "Use ONLY evidence present in the transcript. Do not invent commands, numbers, PRs, "
        "models, statuses, root causes, or next actions. If evidence is absent, say it is absent "
        "as a claim or body sentence instead of fabricating it.\n\n"
        f"{resolution_prompt_contract(resolution)}\n"
        f"{body_format_contract(settings.NOTE_LANG, resolution)}\n"
        f"Missing verifier fields: {', '.join(report.missing)}\n"
        f"Evidence tokens seen: {', '.join(report.evidence_tokens_seen) or '(none)'}\n"
        f"Evidence tokens kept: {', '.join(report.evidence_tokens_kept) or '(none)'}\n\n"
        "If Missing verifier fields includes evidence-tokens, copy the required number of exact tokens "
        "from Evidence tokens seen into the Evidence section or fact claims. Prefer meaningful ids, "
        "durations, counts, units, model names, and statuses over list numbering. Never add a token that "
        "is absent from the transcript.\n\n"
        "Previous JSON note:\n"
        + json.dumps(note, ensure_ascii=False)
        + "\n\n=== SESSION TRANSCRIPT ===\n"
        + text
    )
