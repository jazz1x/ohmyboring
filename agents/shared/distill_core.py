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
import markers  # noqa: E402
import omb_env  # noqa: E402

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
    """Return the git remote.origin.url of cwd, or ''."""
    if not cwd:
        return ""
    try:
        return subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
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


def _build_prompt(text, origin, repo, note_lang=None):
    """Build the distillation prompt, honouring note_lang and repo metadata."""
    lang = note_lang or NOTE_LANG
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

    # Section headers are emitted in the target language so the body is consistent end-to-end.
    section_headers = {
        "ko": ("배경 / 문제", "시도 / 결정", "결과 / 해결", "남은 일"),
        "ja": ("背景 / 問題", "試行 / 決定", "結果 / 解決", "残件"),
        "en": ("Background / Problem", "Attempt / Decision", "Result / Fix", "Remaining work"),
    }.get(lang, ("Background / Problem", "Attempt / Decision", "Result / Fix", "Remaining work"))
    h_bg, h_at, h_rs, h_rm = section_headers

    repo_hint = f" repo='{repo}'." if repo else ""
    origin_hint = f" origin='{origin}'." if origin else ""

    return (
        "You are a distillation engine. Summarize the session transcript into ONE curated note as a "
        f"problem-solving narrative. {lang_instruction}{origin_hint}{repo_hint}\n\n"
        "Output ONLY a single JSON object, no text before or after it:\n"
        '{"title": "...", "body": "...", "tags": ["..."], "tools": ["..."], "concepts": ["..."], '
        "\"claims\": [{\"subject\":\"...\",\"predicate\":\"...\",\"value\":\"...\"}]}\\n\\n"
        f"BODY FORMAT — the body is a markdown string with these sections (omit a section if it does not apply):\n"
        f"  ## {h_bg}   — what was being solved (1-2 lines)\n"
        f"  ## {h_at}    — what was tried, key decisions and WHY\n"
        f"  ## {h_rs}    — what worked: concrete commands, config, root cause\n"
        f"  ## {h_rm}        — unfinished or next steps (omit if none)\n\n"
        "CRITICAL — body content rules (format-breaking bugs happen when you ignore these):\n"
        "- The body MUST contain ONLY markdown prose. NEVER put tags, tools, concepts, claims, or any metadata inside the body.\n"
        "- All metadata MUST go in the JSON fields above, not in the body. A trailing 'tags:' or 'tools:' block in the body is a bug.\n"
        "- Use REAL line breaks inside the JSON string, never the two characters backslash-n.\n\n"
        "WRITING (proven principles — apply, don't just summarize):\n"
        "- BLUF / 要約先出し: each section's first sentence is the conclusion; details follow.\n"
        "- Omit needless words: no filler/repetition, no '·'-joined noun piles, cut hedging.\n"
        "- Plain words, active voice; spell out an acronym on first use.\n\n"
        "Rules:\n"
        "- title: concise, specific, in the target language.\n"
        "- tags: up to 6, lowercase, no hashtags.\n"
        "- tools: concrete tools/commands used (e.g., git, bun, terraform). [] if none.\n"
        "- concepts: recurring ideas/axes (e.g., code_parity, version_upgrade). [] if none.\n"
        "- claims: 3-5 durable facts as (subject, predicate, value) triples. [] only if none exist.\n"
        "  Extract concrete decisions, status changes, and version selections — not opinions or next steps.\n"
        "  Prefer project-scoped subjects. Examples:\n"
        '  {\"subject\":\"kb-rag-bot\",\"predicate\":\"model-interface\",\"value\":\"bedrock-converse\"}\n'
        '  {\"subject\":\"qa-tests\",\"predicate\":\"rtk-status\",\"value\":\"removed\"}\n'
        '  {\"subject\":\"omb\",\"predicate\":\"release-version\",\"value\":\"0.1.3\"}\n'
        '- Pure chit-chat with no real work → output only: {"skip": true}\n\n'
        "=== SESSION TRANSCRIPT ===\n" + text
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


def _call_remember(title, body, origin, repo, tags, tools, concepts, claims, session_id=""):
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
            return False
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
            return False
        except Exception as e:
            print(f"[distill-session] remember call failed: {e}", file=sys.stderr)
            return False

        if data.get("error"):
            print(f"[distill-session] remember error: {data['error']}", file=sys.stderr)
            return False

        result = data.get("result", {})
        content = result.get("content", [])
        text = ""
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "text":
                text += item.get("text", "")
        print(f"[distill-session] {text}", file=sys.stderr)
        return "remembered" in text

    return False


def distill_and_remember(text, origin, repo, session_id=""):
    """Distill the transcript text via local LLM and write it through ohmyboring's remember tool."""
    if len(text) > 12000:
        text = text[:5000] + "\n…(truncated)…\n" + text[-7000:]

    prompt = _build_prompt(text, origin, repo)
    parsed = _call_llm(prompt)
    if parsed is None:
        return False
    if parsed.get("skip"):
        print("[distill-session] LLM decided SKIP", file=sys.stderr)
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
        return False

    tags = [t.strip() for t in parsed.get("tags", []) if isinstance(t, str) and t.strip()][:6]
    tools = [t.strip() for t in parsed.get("tools", []) if isinstance(t, str) and t.strip()][:8]
    concepts = [t.strip() for t in parsed.get("concepts", []) if isinstance(t, str) and t.strip()][:8]
    claims = []
    for c in parsed.get("claims", []):
        if isinstance(c, dict) and c.get("subject") and c.get("predicate") and c.get("value"):
            claims.append(
                {
                    "subject": str(c["subject"]).strip(),
                    "predicate": str(c["predicate"]).strip(),
                    "value": str(c["value"]).strip(),
                }
            )

    return _call_remember(title, body, origin, repo, tags, tools, concepts, claims, session_id)
