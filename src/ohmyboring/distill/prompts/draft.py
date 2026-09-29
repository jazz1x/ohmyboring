"""세션 기록을 노트 하나로 증류시키는 프롬프트."""

from __future__ import annotations

from ohmyboring.distill import settings
from ohmyboring.distill.prompts.sections import body_format_contract
from ohmyboring.distill.resolution import normalize_resolution, resolution_prompt_contract


def build_prompt(text, origin, repo, note_lang=None, resolution=None):
    """Build the distillation prompt, honouring note_lang and repo metadata."""
    lang = note_lang or settings.NOTE_LANG
    resolution = normalize_resolution(resolution or settings.distill_resolution())
    lang_instruction = {
        "ko": "ALL fields MUST be in Korean (한국어), regardless of the transcript's language. "
        "The TITLE especially must be a Korean sentence — even if the session is full of English "
        "ticket IDs (e.g. [FEDEV-97]) or English error names, write the title in Korean and keep "
        "only the proper nouns/IDs/code verbatim. e.g. title → '[FEDEV-97] 하이드레이션 에러 및 "
        "Relay 동기화 해결'. Never copy an all-English title from the transcript.",
        "ja": "ALL fields MUST be in Japanese (日本語), regardless of the transcript's language. "
        "The TITLE especially must be a Japanese sentence — even if the session is full of English "
        "ticket IDs (e.g. [FEDEV-97]) or English error names, write the title in Japanese and keep "
        "only the proper nouns/IDs/code verbatim. e.g. title → '[FEDEV-97] ハイドレーションエラーと "
        "Relay同期の解決'. Never copy an all-English title from the transcript.",
        "en": "ALL fields MUST be in English, regardless of the transcript's language. "
        "The TITLE especially must be an English sentence — even if the session is full of Korean "
        "or Japanese text, write the title in English and keep only proper nouns/IDs/code verbatim. "
        "e.g. title → '[FEDEV-97] Fixing hydration error and Relay sync'. Never copy a non-English "
        "title from the transcript.",
    }.get(lang, "Write in the same language as the transcript.")

    repo_hint = f" repo='{repo}'." if repo else ""
    origin_hint = f" origin='{origin}'." if origin else ""
    resolution_contract = resolution_prompt_contract(resolution)
    body_format = body_format_contract(lang, resolution)

    return (
        "You are a distillation engine. Summarize the session transcript into ONE curated note as a "
        f"problem-solving narrative. {lang_instruction}{origin_hint}{repo_hint}\n\n"
        "Output ONLY a single JSON object, no text before or after it:\n"
        '{"title": "...", "body": "...", "tags": ["..."], "tools": ["..."], "concepts": ["..."], '
        '"claims": [{"subject":"...","predicate":"...","value":"...","kind":"...","confidence":"..."}]}\\n\\n'
        f"{body_format}\n\n"
        "CRITICAL — body content rules (format-breaking bugs happen when you ignore these):\n"
        "- The body MUST contain ONLY markdown prose. NEVER put tags, tools, concepts, claims, or any metadata inside the body.\n"
        "- All metadata MUST go in the JSON fields above, not in the body. A trailing 'tags:' or 'tools:' block in the body is a bug.\n"
        "- Use REAL line breaks inside the JSON string, never the two characters backslash-n.\n"
        "- In the Evidence section, if the transcript contains a table, code snippet, or symbol-heavy excerpt that is hard to summarize, preserve it verbatim inside a markdown code block instead of paraphrasing. Never truncate code mid-token; include the full line or mark it omitted.\n\n"
        f"{resolution_contract}\n"
        "WRITING (proven principles — apply, don't just summarize):\n"
        "- BLUF / 要約先出し: each section's first sentence is the conclusion; details follow.\n"
        "- Omit needless words: no filler/repetition, no '·'-joined noun piles, cut hedging.\n"
        "- Plain words, active voice; spell out an acronym on first use.\n\n"
        "Rules:\n"
        "- title: project + concrete action + scope/date. Must be distinguishable from previous notes. "
        "e.g. 'omb: retrieval 필터 추가 (phase-2)', 'kb-rag-bot: MCP 인증 백엔드 구현 (2026-06-28)'. "
        "Never use generic titles like '기능 개선', '작업 정리', '코드 수정'.\n"
        "- tags: up to 6, lowercase, no hashtags.\n"
        "- tools: concrete tools/commands used (e.g., git, bun, terraform). [] if none.\n"
        "- concepts: recurring ideas/axes (e.g., code_parity, version_upgrade). [] if none.\n"
        "- claims: 3-5 durable facts/decisions/risks/next-steps as (subject, predicate, value, kind, confidence). [] only if none exist.\n"
        "  kind: one of fact, decision, assumption, risk, blocked, goal, next.\n"
        "  confidence: one of certain, likely, assumption, outdated.\n"
        "  Extract concrete decisions, status changes, version selections, open risks, and any explicit next action still pending.\n"
        "  Use kind='next' for concrete follow-up actions left undone at session end. Use kind='blocked' only when an active obstacle prevents progress.\n"
        "  Prefer project-scoped subjects.\n"
        "  The value must READ AS A STATEMENT that stands on its own months later, not as a tag:\n"
        "  it says what was chosen, what broke, or what is left to do, and enough of why that the\n"
        "  next reader does not have to open the note. A value under ~25 characters is almost\n"
        "  always a tag — write the sentence instead.\n"
        "  The predicate NAMES THE ASPECT the value is about — which knob, which flow, which file,\n"
        "  which decision. Ask: could this predicate sit on a completely different claim? If yes it\n"
        "  is a slot word, not a name. These are rejected for EVERY kind, not just the matching one:\n"
        "  status, state, result, outcome, info, detail, incident, decision, action, next-step,\n"
        "  상태, 결정, 결과. Write `path_resolution`, `retry-bound`, `auth-flow` instead.\n"
        "  Examples:\n"
        '  {"subject":"kb-rag-bot","predicate":"model-interface","value":"bedrock-converse, because the streaming API drops tool calls mid-turn","kind":"decision","confidence":"certain"}\n'
        '  {"subject":"qa-tests","predicate":"rtk-dependency","value":"removed — the store was only read in two dead components","kind":"fact","confidence":"certain"}\n'
        '  {"subject":"omb","predicate":"release-version","value":"0.1.3, the first build that ships the host CLI alongside the image","kind":"fact","confidence":"certain"}\n'
        '  {"subject":"kb-rag-bot","predicate":"auth-flow","value":"the oauth redirect is never verified, so any return URL is accepted","kind":"risk","confidence":"likely"}\n'
        '  {"subject":"omb","predicate":"register-endpoint","value":"add /next_actions so the card stops synthesising its own next steps","kind":"next","confidence":"certain"}\n'
        '  Counter-examples, all rejected: value "removed", value "completed", value "PASS",\n'
        '  predicate "incident", predicate "status".\n'
        '- Pure chit-chat with no real work → output only: {"skip": true}\n\n'
        "=== SESSION TRANSCRIPT ===\n" + text
    )
