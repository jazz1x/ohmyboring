"""본문 절에서 빠진 필수 주장 종류를 되살린다 — LLM 이 결정·사실 주장을 빼먹었을 때."""

from __future__ import annotations

from ohmyboring.distill.resolution import RESOLUTION_RULES, normalize_resolution


def ensure_required_claim_kinds(note, resolution, repo):
    """Derive required claim kinds from already-generated body sections when the LLM omitted them."""
    level = normalize_resolution(resolution)
    required = RESOLUTION_RULES[level]["claim_kinds"]
    claims = note["claims"]
    kinds = {claim["kind"] for claim in claims}
    subject = repo or note["title"] or "distilled-note"
    if "decision" in required and "decision" not in kinds:
        decision = _section_excerpt(note["body"], ("decision", "결정", "선택", "決定", "判断"))
        if decision:
            claims.append(
                {
                    "subject": subject,
                    "predicate": "decision",
                    "value": decision,
                    "kind": "decision",
                    "confidence": "likely",
                }
            )
            kinds.add("decision")
    if "fact" in required and "fact" not in kinds:
        fact = _section_excerpt(
            note["body"], ("evidence", "근거", "검증", "根拠", "検証", "result", "결과", "結果")
        )
        if fact:
            claims.append(
                {
                    "subject": subject,
                    "predicate": "fact",
                    "value": fact,
                    "kind": "fact",
                    "confidence": "likely",
                }
            )
    return note


def _section_excerpt(body, section_signals):
    current = None
    chunks = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].lower()
            if current is not None:
                break
            if any(signal.lower() in heading for signal in section_signals):
                current = heading
            continue
        if current is not None and stripped:
            chunks.append(stripped)
    excerpt = " ".join(chunks)
    return excerpt[:240].strip()
