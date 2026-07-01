#!/usr/bin/env python3
"""Shared distillation core for session-end hooks (Claude Code, Kimi, etc.).

Hosts the pure, agent-agnostic pieces of the self-augmentation loop:
  - marker throttle/retry bookkeeping
  - repo/origin classification
  - LLM distillation prompt + OpenAI-compatible caller
  - `remember` MCP tool caller

Agent-specific transcript extraction and hook I/O live in the per-agent modules.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Allow import of shared agent policy library regardless of how this script is invoked.
# realpath resolves symlinks (e.g. hooks/distill-session.py → agents/claude-code/…) so the
# sibling agents/shared dir is found from the real file location, not the symlink's dir.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import boring_config  # noqa: E402
import event_log  # noqa: E402
import markers  # noqa: E402
import omb_env  # noqa: E402
import workflow_contract  # noqa: E402
from resolution_quality import (  # noqa: E402
    ALLOWED_CLAIM_KINDS,
    ALLOWED_RESOLUTIONS,
    RESOLUTION_RULES,
    normalize_resolution,
    resolution_prompt_contract,
    verify_note_resolution,
)

# BORING_HOME: repo clone location (default ~/oh-my-boring).
BORING_HOME = os.environ.get("BORING_HOME") or omb_env.omb_home()
BORING_URL = omb_env.drudge_url()  # BORING_URL canonical, BORING_URL deprecated alias
# LLM connection resolves through omb_env (SSOT): env override (BORING_LLM_*) → boring.json
# llm block → default, with host.docker.internal → localhost rewrite on the host.
LLM_BASE_URL = omb_env.llm_base_url()
LLM_MODEL = omb_env.llm_model()
LLM_API_KEY = omb_env.llm_api_key()
NOTE_LANG = boring_config.note_lang()
# Minimum interval (minutes) before re-distilling an in-progress session (Stop hook).
# SessionEnd (final) ignores the throttle.
THROTTLE_MIN = int(os.environ.get("DISTILL_THROTTLE_MIN") or "25")


class RememberOutcome:
    def __init__(self, ok, status):
        self.ok = ok
        self.status = status


def _distill_resolution():
    raw = os.environ.get("BORING_DISTILL_RESOLUTION")
    level = normalize_resolution(raw or "evidence", default="evidence")
    if raw and raw.strip().lower() not in ALLOWED_RESOLUTIONS:
        print(
            f"[distill-session] invalid BORING_DISTILL_RESOLUTION={raw!r}; using 'evidence'",
            file=sys.stderr,
        )
    return level

def _throttled(session_id):
    """True (skip) if this session was already distilled within the last THROTTLE_MIN minutes."""
    if not session_id:
        return False
    done_time = markers.done_time(session_id)
    if done_time is None:
        return False
    return (time.time() - done_time) < THROTTLE_MIN * 60


def _mark(session_id, retry=False):
    """Write a done marker (.ts) or a retry marker (.retry).

    A .retry marker tells collect-sessions.py (the backfill scheduler) that this
    SessionEnd/Stop hook failed transiently and the session should be retried later.
    It is distinct from hermes-agent's .pending markers so the two queues don't collide.
    """
    # `make distill-now` sets this so an on-demand mid-session distill leaves no done-marker:
    # the session stays eligible for the normal SessionEnd capture and is re-distillable on demand.
    if os.environ.get("BORING_DISTILL_NO_MARK"):
        return
    if not session_id:
        return
    if retry:
        markers.mark_retry(session_id)
    else:
        markers.mark_done(session_id)


def git_remote_url(cwd):
    """Return the git remote.origin.url of cwd (or its nearest git ancestor), or ''."""
    if not cwd:
        return ""
    try:
        # Walk up to the git root first so subdirectories resolve to the same
        # project name as the repository root.
        root = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not root:
            return ""
        return subprocess.run(
            ["git", "-C", root, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def repo_slug(cwd):
    """Category axis: canonical repo slug from the git remote, falling back to folder name."""
    url = git_remote_url(cwd)
    if url:
        slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(?:\.git)?$", r"\1", url)
        if slug and slug != url:
            return boring_config.canonical_repo(slug)
    if cwd:
        return boring_config.canonical_repo(os.path.basename(cwd.rstrip("/")))
    return ""


def _strip_trailing_metadata(body):
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


def _extract_json(text):
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


def _build_prompt(text, origin, repo, note_lang=None, resolution=None):
    """Build the distillation prompt, honouring note_lang and repo metadata."""
    lang = note_lang or NOTE_LANG
    resolution = normalize_resolution(resolution or _distill_resolution())
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
    body_format = _body_format_contract(lang, resolution)

    return (
        "You are a distillation engine. Summarize the session transcript into ONE curated note as a "
        f"problem-solving narrative. {lang_instruction}{origin_hint}{repo_hint}\n\n"
        "Output ONLY a single JSON object, no text before or after it:\n"
        '{"title": "...", "body": "...", "tags": ["..."], "tools": ["..."], "concepts": ["..."], '
        "\"claims\": [{\"subject\":\"...\",\"predicate\":\"...\",\"value\":\"...\",\"kind\":\"...\",\"confidence\":\"...\"}]}\\n\\n"
        f"{body_format}\n\n"
        "CRITICAL — body content rules (format-breaking bugs happen when you ignore these):\n"
        "- The body MUST contain ONLY markdown prose. NEVER put tags, tools, concepts, claims, or any metadata inside the body.\n"
        "- All metadata MUST go in the JSON fields above, not in the body. A trailing 'tags:' or 'tools:' block in the body is a bug.\n"
        "- Use REAL line breaks inside the JSON string, never the two characters backslash-n.\n\n"
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
        "  Prefer project-scoped subjects. Examples:\n"
        '  {\"subject\":\"kb-rag-bot\",\"predicate\":\"model-interface\",\"value\":\"bedrock-converse\",\"kind\":\"decision\",\"confidence\":\"certain\"}\n'
        '  {\"subject\":\"qa-tests\",\"predicate\":\"rtk-status\",\"value\":\"removed\",\"kind\":\"fact\",\"confidence\":\"certain\"}\n'
        '  {\"subject\":\"omb\",\"predicate\":\"release-version\",\"value\":\"0.1.3\",\"kind\":\"fact\",\"confidence\":\"certain\"}\n'
        '  {\"subject\":\"kb-rag-bot\",\"predicate\":\"auth-flow\",\"value\":\"oauth-redirect-unverified\",\"kind\":\"risk\",\"confidence\":\"likely\"}\n'
        '  {\"subject\":\"omb\",\"predicate\":\"next-step\",\"value\":\"add /next_actions endpoint\",\"kind\":\"next\",\"confidence\":\"certain\"}\n'
        '- Pure chit-chat with no real work → output only: {"skip": true}\n\n'
        "=== SESSION TRANSCRIPT ===\n" + text
    )


def _body_format_contract(lang, resolution):
    """Return the markdown section skeleton that matches the resolution verifier."""
    level = normalize_resolution(resolution)
    headers = _localized_section_headers(lang)
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
        "evidence": "commands, PRs, ids, counts, timings, model names, and status evidence",
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


def _localized_section_headers(lang):
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


def _build_repair_prompt(text, origin, repo, note, report, resolution):
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
        f"{_body_format_contract(NOTE_LANG, resolution)}\n"
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


def _call_llm(prompt):
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

    parsed = _extract_json(message)
    if parsed is None:
        print(
            f"[distill-session] failed to parse LLM output as JSON:\n{message[:500]}",
            file=sys.stderr,
        )
    return parsed


def _call_remember(title, body, origin, repo, tags, tools, concepts, claims, session_id="", sources=None):
    """Call ohmyboring's remember MCP tool.

    Retries transient failures (5xx, connection errors, timeouts) a bounded number of times
    so a momentary engine hiccup does not drop the session. Permanent failures (4xx, MCP
    error, missing "remembered" ack) are not retried.
    """
    arguments = {
        "title": title,
        "body": body,
        "origin": origin,
        "repo": repo,
        "tags": tags,
        "tools": tools,
        "concepts": concepts,
        "claims": claims,
    }
    if sources:
        arguments["sources"] = sources
    if session_id:
        arguments["omb_session_id"] = session_id
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "remember", "arguments": arguments},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BORING_URL.rstrip('/')}/mcp",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )

    max_retries = int(os.environ.get("DISTILL_REMEMBER_RETRIES") or "2")
    timeout = int(os.environ.get("DISTILL_REMEMBER_TIMEOUT") or "45")

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and attempt < max_retries:
                print(
                    f"[distill-session] remember attempt {attempt + 1} got HTTP {e.code}, retrying...",
                    file=sys.stderr,
                )
                time.sleep(1 << attempt)
                continue
            print(f"[distill-session] remember call failed: {e}", file=sys.stderr)
            return RememberOutcome(False, "failed")
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            if attempt < max_retries:
                print(
                    f"[distill-session] remember attempt {attempt + 1} failed transiently ({e}), retrying...",
                    file=sys.stderr,
                )
                time.sleep(1 << attempt)
                continue
            print(
                f"[distill-session] remember call failed after {max_retries + 1} attempts: {e}",
                file=sys.stderr,
            )
            return RememberOutcome(False, "failed")
        except Exception as e:
            print(f"[distill-session] remember call failed: {e}", file=sys.stderr)
            return RememberOutcome(False, "failed")

        if data.get("error"):
            print(f"[distill-session] remember error: {data['error']}", file=sys.stderr)
            return RememberOutcome(False, "failed")

        result = data.get("result", {})
        content = result.get("content", [])
        text = ""
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "text":
                text += item.get("text", "")
        print(f"[distill-session] {text}", file=sys.stderr)
        # A duplicate skip is a successful deterministic outcome, not a transient failure.
        if "skipped — duplicate" in text:
            return RememberOutcome(True, "duplicate")
        if "remembered" in text:
            return RememberOutcome(True, "remembered")
        return RememberOutcome(False, "failed")

    return RememberOutcome(False, "failed")


def _prepare_note(parsed):
    title = parsed.get("title", "").strip()
    body = parsed.get("body", "").strip()
    # gemma sometimes double-escapes newlines (emits "\\n" in the JSON), so json.loads yields a literal
    # backslash-n in the body instead of a real line break → markdown renders as one run-on line. It often
    # MIXES literal "\\n" with a few real breaks, so normalize whenever any literal "\\n" is present.
    n_lit = body.count("\\n")
    if "\\n" in body:
        body = body.replace("\\n", "\n").replace("\\t", "\t")
    # Instrumentation (not a cleanup cycle): a rising count here = the prompt is regressing into
    # double-escaped output. Steady 0 means distillation is healthy; investigate if it grows.
    body_meta = _strip_trailing_metadata(body)
    n_meta = body != body_meta  # True if a trailing tags/tools/concepts block had to be stripped
    body = body_meta
    if n_lit or n_meta:
        print(
            f"[distill-session] body normalized: {n_lit} literal newlines"
            f"{', trailing-metadata stripped' if n_meta else ''} — watch for prompt regression",
            file=sys.stderr,
        )
    # Language regression signal: note_lang=ko but the title came back with no Korean at all → the
    # model copied an all-English title (usually triggered by [TICKET-ID] prefixes). Logged, not
    # auto-fixed — a rising rate means the title prompt needs another nudge.
    if NOTE_LANG == "ko" and title and not re.search(r"[가-힣]", title):
        print(
            f"[distill-session] title not Korean despite note_lang=ko: {title!r} — watch for prompt regression",
            file=sys.stderr,
        )
    if not title or not body:
        print("[distill-session] missing title/body in LLM output", file=sys.stderr)
        return None

    tags = [t.strip() for t in parsed.get("tags", []) if isinstance(t, str) and t.strip()][:6]
    tools = [t.strip() for t in parsed.get("tools", []) if isinstance(t, str) and t.strip()][:8]
    concepts = [t.strip() for t in parsed.get("concepts", []) if isinstance(t, str) and t.strip()][:8]
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
                    "kind": _normalize_claim_kind(str(c.get("kind", "fact")).strip(), subject, predicate, value),
                    "confidence": str(c.get("confidence", "certain")).strip() or "certain",
                }
            )
    return {
        "title": title,
        "body": body,
        "tags": tags,
        "tools": tools,
        "concepts": concepts,
        "claims": claims,
    }


def _normalize_claim_kind(kind, subject, predicate, value):
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


def _ensure_required_claim_kinds(note, resolution, repo):
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
        fact = _section_excerpt(note["body"], ("evidence", "근거", "검증", "根拠", "検証", "result", "결과", "結果"))
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


def _ensure_required_evidence_tokens(note, transcript, resolution):
    """Preserve short transcript excerpts when the LLM drops required concrete evidence tokens."""
    report = verify_note_resolution(
        {"title": note["title"], "body": note["body"], "claims": note["claims"]},
        transcript=transcript,
        resolution=resolution,
    )
    missing_token_rule = next((item for item in report.missing if item.startswith("evidence-tokens:min:")), "")
    if not missing_token_rule:
        return note
    required = int(missing_token_rule.rsplit(":", 1)[1])
    missing_tokens = [token for token in report.evidence_tokens_seen if token not in report.evidence_tokens_kept]
    if not missing_tokens:
        return note
    needed = max(0, required - len(report.evidence_tokens_kept))
    snippets = []
    for token in sorted(missing_tokens, key=_evidence_token_rank):
        snippet = _transcript_excerpt_for_token(transcript, token)
        if snippet and snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= needed:
            break
    if not snippets:
        return note
    note["body"] = _append_evidence_snippets(note["body"], snippets)
    return note


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
    }.get(NOTE_LANG, "Original evidence excerpt")
    addition = "\n".join(f"- {label}: {snippet}" for snippet in snippets)
    if re.search(r"(?im)^## .*(evidence|basis|근거|검증|根拠|検証)", body):
        return body.rstrip() + "\n" + addition + "\n"
    header = _localized_section_headers(NOTE_LANG)["evidence"]
    return body.rstrip() + f"\n\n## {header}\n" + addition + "\n"


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


def _log_resolution_event(session_id, origin, repo, report, verifier_status, remember_status):
    ok = verifier_status in {"pass", "repaired"} and remember_status in {"remembered", "duplicate"}
    try:
        event_log.append_event(
            "distill-session",
            "distill_resolution",
            "ok" if ok else "failed",
            session_id=session_id,
            origin=origin,
            repo=repo,
            resolution=report.resolution,
            verifier_status=verifier_status,
            missing_fields=list(report.missing),
            claim_count=report.claim_count,
            numbers_seen=len(report.evidence_tokens_seen),
            numbers_kept=len(report.evidence_tokens_kept),
            remember_status=remember_status,
            **workflow_contract.resolution_fields(verifier_status, remember_status),
        )
    except OSError as e:
        print(f"[distill-session] event log write failed: {e}", file=sys.stderr)


def log_skip_event(session_id, origin, repo, resolution, reason):
    event_log.try_append_event(
        "distill-session",
        "distill_resolution",
        "ok",
        session_id=session_id,
        origin=origin,
        repo=repo,
        resolution=resolution,
        verifier_status="skipped",
        missing_fields=[],
        claim_count=0,
        numbers_seen=0,
        numbers_kept=0,
        remember_status="skipped",
        reason=reason,
        **workflow_contract.skip_fields(),
    )


def _log_skip_event(session_id, origin, repo, resolution):
    log_skip_event(session_id, origin, repo, resolution, "llm_skip")


def distill_and_remember(text, origin, repo, session_id=""):
    """Distill the transcript text via local LLM and write it through ohmyboring's remember tool."""
    if len(text) > 12000:
        text = text[:5000] + "\n…(truncated)…\n" + text[-7000:]

    resolution = _distill_resolution()
    prompt = _build_prompt(text, origin, repo, resolution=resolution)
    parsed = _call_llm(prompt)
    if parsed is None:
        return False
    if parsed.get("skip"):
        print("[distill-session] LLM decided SKIP", file=sys.stderr)
        _log_skip_event(session_id, origin, repo, resolution)
        return True  # intentional skip → mark as done so we don't retry forever

    # Language retry: note_lang=ko but the title came back with no Korean → the model ignored the
    # language instruction (gemma is weak at language control). Re-ask ONCE with a corrective nudge;
    # keep the retry only if it actually came back in Korean, else fall back to the original.
    if NOTE_LANG == "ko" and parsed.get("title", "") and not re.search(r"[가-힣]", parsed["title"]):
        retry = _call_llm(
            prompt
            + "\n\n=== CORRECTION ===\nYour previous output was in English — that is WRONG. Re-emit the "
            "SAME JSON object but with title, body, tags, and concepts ALL in Korean (한국어). Keep code, "
            "IDs, and proper nouns verbatim."
        )
        if retry and re.search(r"[가-힣]", retry.get("title", "")):
            parsed = retry
            print("[distill-session] language retry → Korean OK", file=sys.stderr)
        else:
            print("[distill-session] language retry failed — keeping original", file=sys.stderr)

    note = _prepare_note(parsed)
    if note is None:
        return False
    note = _ensure_required_claim_kinds(note, resolution, repo)
    note = _ensure_required_evidence_tokens(note, text, resolution)

    report = verify_note_resolution(
        {"title": note["title"], "body": note["body"], "claims": note["claims"]},
        transcript=text,
        resolution=resolution,
    )
    verifier_status = "pass"
    if not report.ok:
        print(
            "[distill-session] resolution gate failed "
            f"({report.resolution}): {', '.join(report.missing)}; "
            f"claims={report.claim_count}; "
            f"evidence={len(report.evidence_tokens_kept)}/{len(report.evidence_tokens_seen)}",
            file=sys.stderr,
        )
        repaired = _call_llm(_build_repair_prompt(text, origin, repo, note, report, resolution))
        if repaired is None or repaired.get("skip"):
            _log_resolution_event(session_id, origin, repo, report, "failed", "not_called")
            return False
        repaired_note = _prepare_note(repaired)
        if repaired_note is None:
            _log_resolution_event(session_id, origin, repo, report, "failed", "not_called")
            return False
        repaired_note = _ensure_required_claim_kinds(repaired_note, resolution, repo)
        repaired_note = _ensure_required_evidence_tokens(repaired_note, text, resolution)
        repaired_report = verify_note_resolution(
            {"title": repaired_note["title"], "body": repaired_note["body"], "claims": repaired_note["claims"]},
            transcript=text,
            resolution=resolution,
        )
        if not repaired_report.ok:
            print(
                "[distill-session] resolution repair failed "
                f"({repaired_report.resolution}): {', '.join(repaired_report.missing)}; "
                f"claims={repaired_report.claim_count}; "
                f"evidence={len(repaired_report.evidence_tokens_kept)}/{len(repaired_report.evidence_tokens_seen)}",
                file=sys.stderr,
            )
            _log_resolution_event(session_id, origin, repo, repaired_report, "failed", "not_called")
            return False
        note = repaired_note
        report = repaired_report
        verifier_status = "repaired"
        print("[distill-session] resolution repair passed", file=sys.stderr)

    remember = _call_remember(
        note["title"],
        note["body"],
        origin,
        repo,
        note["tags"],
        note["tools"],
        note["concepts"],
        note["claims"],
        session_id,
    )
    _log_resolution_event(session_id, origin, repo, report, verifier_status, remember.status)
    return remember.ok
