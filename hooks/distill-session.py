#!/usr/bin/env python3
"""Claude Code SessionEnd/Stop hook — distill a session into memory via the local LLM.

Kernel: distillation now happens on the host, directly against the OpenAI-compatible
LLM endpoint (Ollama/LM Studio/…). The engine (drudge) remains the deterministic write
gate: this hook only generates the curated note and calls the `remember` MCP tool.

This removes the hermes-agent dependency for the write door and makes the core
self-augmentation loop work in `OMB_CORE_ONLY=1` mode.

Install (persistence) — ~/.claude/settings.json:
  {"type":"command","command":"python3 ~/oh-my-boring/hooks/distill-session.py",
   "timeout":130,"async":true}
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import boring_config

# OMB_HOME: repo clone location (default ~/oh-my-boring).
OMB_HOME = os.environ.get("OMB_HOME") or os.path.expanduser("~/oh-my-boring")
DRUDGE_URL = os.environ.get("DRUDGE_URL", "http://localhost:7700")
LLM_BASE_URL = os.environ.get("DRUDGE_LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = os.environ.get("DRUDGE_LLM_MODEL", "gemma4:12b")
LLM_API_KEY = os.environ.get("DRUDGE_LLM_API_KEY") or ""
NOTE_LANG = boring_config.note_lang()
# Minimum interval (minutes) before re-distilling an in-progress session (Stop hook).
# SessionEnd (final) ignores the throttle.
THROTTLE_MIN = int(os.environ.get("DISTILL_THROTTLE_MIN") or "25")
MARK_DIR = os.path.expanduser("~/.cache/boring-distill")


def _mark_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "nosession"
    return os.path.join(MARK_DIR, f"{safe}.ts")


def _throttled(session_id):
    """True (skip) if this session was already distilled within the last THROTTLE_MIN minutes."""
    if not session_id:
        return False
    try:
        age = time.time() - os.path.getmtime(_mark_path(session_id))
        return age < THROTTLE_MIN * 60
    except OSError:
        return False


def _mark(session_id):
    if not session_id:
        return
    try:
        os.makedirs(MARK_DIR, exist_ok=True)
        with open(_mark_path(session_id), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def extract(path):
    """Extract user/assistant text from a Claude Code JSONL transcript."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            msg = obj.get("message") or {}
            role = msg.get("role") or obj.get("type") or ""
            if role not in ("user", "assistant"):
                continue
            c = msg.get("content")
            if isinstance(c, str):
                t = c
            elif isinstance(c, list):
                t = " ".join(
                    b.get("text", "")
                    for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                t = ""
            t = t.strip()
            if t:
                out.append(f"[{role}] {t}")
    return "\n".join(out)


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
    """Category axis: repo slug (`org/name`) from the git remote, falling back to folder name."""
    url = git_remote_url(cwd)
    if url:
        slug = re.sub(r"^.*[:/]([^/]+/[^/]+?)(?:\.git)?$", r"\1", url)
        if slug and slug != url:
            return slug
    if cwd:
        return os.path.basename(cwd.rstrip("/")) or ""
    return ""


def _extract_json(text):
    """Best-effort JSON extraction from an LLM response that may wrap it in markdown."""
    text = text.strip()
    # Remove markdown code fences if present.
    if text.startswith("```"):
        text = text[text.find("\n") + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    text = text.strip()
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _build_prompt(text, origin, repo):
    """Build the distillation prompt, honouring note_lang and repo metadata."""
    lang_instruction = {
        "ko": "Write the note in Korean.",
        "en": "Write the note in English.",
    }.get(NOTE_LANG, "Write the note in the same language as the source transcript.")

    repo_hint = f" repo='{repo}'." if repo else ""
    origin_hint = f" origin='{origin}'."

    return (
        "You are a distillation engine. Summarize the following session transcript into ONE "
        "curated markdown note. "
        f"{lang_instruction}{origin_hint}{repo_hint}\n\n"
        "Output ONLY a single JSON object with no markdown commentary:\n"
        "{\n"
        '  "title": "concise, specific title",\n'
        '  "body": "markdown body with problem / attempted solution / result sections",\n'
        '  "tags": ["tag1", "tag2", ...],\n'
        '  "claims": [{"subject":"...", "predicate":"...", "value":"..."}, ...]\n'
        "}\n\n"
        "Rules:\n"
        "- title and body are required.\n"
        "- tags: up to 5 strings, no hashtags.\n"
        "- claims: optional triples; skip if none are clear.\n"
        "- If the transcript is pure chit-chat with no real work, output only: {\"skip\": true}\n"
        "- Do NOT include any text outside the JSON object.\n\n"
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


def _call_remember(title, body, origin, repo, tags, claims):
    """Call drudge's remember MCP tool. Return True if the note was written."""
    arguments = {
        "title": title,
        "body": body,
        "origin": origin,
        "repo": repo,
        "tags": tags,
        "claims": claims,
    }
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "remember", "arguments": arguments},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{DRUDGE_URL.rstrip('/')}/mcp",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
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


def distill_and_remember(transcript_path, origin, repo):
    """Distill the transcript via local LLM and write it through drudge's remember tool."""
    text = extract(transcript_path)
    if len(text) > 12000:
        text = text[:5000] + "\n…(truncated)…\n" + text[-7000:]

    parsed = _call_llm(_build_prompt(text, origin, repo))
    if parsed is None:
        return False
    if parsed.get("skip"):
        print("[distill-session] LLM decided SKIP", file=sys.stderr)
        return True  # intentional skip → mark as done so we don't retry forever

    title = parsed.get("title", "").strip()
    body = parsed.get("body", "").strip()
    if not title or not body:
        print("[distill-session] missing title/body in LLM output", file=sys.stderr)
        return False

    tags = [t.strip() for t in parsed.get("tags", []) if isinstance(t, str) and t.strip()][:6]
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

    return _call_remember(title, body, origin, repo, tags, claims)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    transcript_path = data.get("transcript_path") or ""
    if not transcript_path or not os.path.exists(transcript_path):
        return

    session_id = data.get("session_id") or ""
    is_final = (data.get("hook_event_name") or "") == "SessionEnd"
    if not is_final and _throttled(session_id):
        return

    cwd = data.get("cwd") or ""
    remote_url = git_remote_url(cwd)
    origin, _rule = boring_config.classify(cwd, remote_url or None)
    text = extract(transcript_path)
    if len(text) < 500:
        return

    repo = repo_slug(cwd)
    if distill_and_remember(transcript_path, origin, repo):
        _mark(session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block the session
    sys.exit(0)
