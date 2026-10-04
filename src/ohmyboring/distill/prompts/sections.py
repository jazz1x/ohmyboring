"""노트 본문의 절 뼈대 — 검증기가 찾는 머리글과 같은 이름을 언어별로 낸다."""

from __future__ import annotations

from ohmyboring.distill.resolution import normalize_resolution


def body_format_contract(lang, resolution):
    """Return the markdown section skeleton that matches the resolution verifier."""
    level = normalize_resolution(resolution)
    headers = localized_section_headers(lang)
    sections_by_level = {
        "compact": ("problem", "result"),
        "standard": ("problem", "decision", "result"),
        "evidence": ("problem", "as_is", "to_be", "decision", "evidence", "result", "next"),
        "forensic": (
            "problem",
            "as_is",
            "to_be",
            "timeline",
            "root_cause",
            "decision",
            "evidence",
            "result",
            "regression",
            "next",
        ),
    }
    descriptions = {
        "problem": "what was being solved and why it mattered",
        "as_is": "the previous or current state before the change",
        "to_be": "the intended target state after the change",
        "timeline": "ordered events, commands, or attempts",
        "root_cause": "the verified cause, or say evidence is absent",
        "decision": "what was decided, with the reason",
        "evidence": "commands, PRs, ids, counts, timings, model names, and status evidence; quote symbol-heavy or code excerpts verbatim in a code block rather than mangling them",
        "result": "what changed and what was verified",
        "regression": "repro, fixture, or guard that prevents recurrence",
        "next": "unfinished work or next action; write '없음'/'none' if truly none",
    }
    lines = [
        "BODY FORMAT — the body is a markdown string with these exact section headings.",
        "Do not rename required headings; the readiness verifier searches for these signals.",
    ]
    for section in sections_by_level[level]:
        lines.append(f"  ## {headers[section]} — {descriptions[section]}")
    return "\n".join(lines)


def localized_section_headers(lang):
    localized = {
        "ko": {
            "problem": "배경 / 문제",
            "as_is": "현재 상태",
            "to_be": "목표 상태",
            "timeline": "타임라인",
            "root_cause": "근본원인",
            "decision": "결정",
            "evidence": "근거 / 검증",
            "result": "결과",
            "regression": "회귀 / 재현",
            "next": "남은 일",
        },
        "ja": {
            "problem": "背景 / 問題",
            "as_is": "現状",
            "to_be": "あるべき姿",
            "timeline": "タイムライン",
            "root_cause": "根本原因",
            "decision": "決定",
            "evidence": "根拠 / 検証",
            "result": "結果",
            "regression": "回帰 / 再現",
            "next": "残件",
        },
        "en": {
            "problem": "Background / Problem",
            "as_is": "As-Is",
            "to_be": "To-Be",
            "timeline": "Timeline",
            "root_cause": "Root Cause",
            "decision": "Decision",
            "evidence": "Evidence",
            "result": "Result",
            "regression": "Regression / Repro",
            "next": "Next",
        },
    }
    return localized.get(lang, localized["en"])
