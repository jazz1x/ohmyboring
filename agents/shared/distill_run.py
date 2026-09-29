#!/usr/bin/env python3
"""Distillation graph entry — runs only where the engine dependencies live (hermes).

Host hooks stay stdlib-only and queue sessions (`distill_queue`); the hermes ingest-worker
drains the queue through `distill_and_remember` here. Helpers stay in `distill_core` and are
called as module attributes so a patch on `distill_core` reaches the nodes.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))
import distill_core  # noqa: E402
import transcript  # noqa: E402
from resolution_quality import verify_note_resolution  # noqa: E402

from ohmyboring.distill import graph as distill_graph  # noqa: E402


def _draft(state):
    resolution = distill_core._distill_resolution()
    prompt = distill_core._build_prompt(state["text"], state["origin"], state["repo"], resolution=resolution)
    parsed = distill_core._call_llm(prompt)
    # Language retry: note_lang=ko but the title came back with no Korean → the model ignored the
    # language instruction (gemma is weak at language control). Re-ask ONCE with a corrective nudge;
    # keep the retry only if it actually came back in Korean, else fall back to the original.
    wants_language_retry = (
        parsed is not None
        and distill_core.NOTE_LANG == "ko"
        and bool(parsed.get("title", ""))
        and not re.search(r"[가-힣]", parsed["title"])
    )
    return {
        "resolution": resolution,
        "prompt": prompt,
        "parsed": parsed,
        "wants_language_retry": wants_language_retry,
    }


def _skip(state):
    print("[distill-session] LLM decided SKIP", file=sys.stderr)
    distill_core.log_skip_event(
        state["session_id"], state["origin"], state["repo"], state["resolution"], "llm_skip"
    )
    return {"ok": True}  # intentional skip → mark as done so we don't retry forever


def _retry_language(state):
    retry = distill_core._call_llm(
        state["prompt"]
        + "\n\n=== CORRECTION ===\nYour previous output was in English — that is WRONG. Re-emit the "
        "SAME JSON object but with title, body, tags, and concepts ALL in Korean (한국어). Keep code, "
        "IDs, and proper nouns verbatim."
    )
    if retry and re.search(r"[가-힣]", retry.get("title", "")):
        print("[distill-session] language retry → Korean OK", file=sys.stderr)
        return {"parsed": retry}
    print("[distill-session] language retry failed — keeping original", file=sys.stderr)
    return {}


def _prepare(state):
    return {"note": distill_core._prepare_note(state["parsed"])}


def _verified(note, state):
    note = distill_core._ensure_required_claim_kinds(note, state["resolution"], state["repo"])
    note = distill_core._ensure_required_evidence_tokens(note, state["text"], state["resolution"])
    report = verify_note_resolution(
        {"title": note["title"], "body": note["body"], "claims": note["claims"]},
        transcript=state["text"],
        resolution=state["resolution"],
    )
    return note, report


def _verify(state):
    note, report = _verified(state["note"], state)
    return {"note": note, "report": report, "verified": report.ok}


def _repair_call(state):
    report = state["report"]
    print(
        "[distill-session] resolution gate failed "
        f"({report.resolution}): {', '.join(report.missing)}; "
        f"claims={report.claim_count}; "
        f"evidence={len(report.evidence_tokens_kept)}/{len(report.evidence_tokens_seen)}",
        file=sys.stderr,
    )
    prompt = distill_core._build_repair_prompt(
        state["text"], state["origin"], state["repo"], state["note"], report, state["resolution"]
    )
    return {"repaired": distill_core._call_llm(prompt)}


def _repair_prepare(state):
    return {"repaired_note": distill_core._prepare_note(state["repaired"])}


def _repair_verify(state):
    note, report = _verified(state["repaired_note"], state)
    return {"repaired_note": note, "repaired_report": report, "repaired_verified": report.ok}


def _repair_failed(state):
    report = state["repaired_report"]
    print(
        "[distill-session] resolution repair failed "
        f"({report.resolution}): {', '.join(report.missing)}; "
        f"claims={report.claim_count}; "
        f"evidence={len(report.evidence_tokens_kept)}/{len(report.evidence_tokens_seen)}",
        file=sys.stderr,
    )
    distill_core._log_resolution_event(
        state["session_id"], state["origin"], state["repo"], report, "failed", "not_called"
    )
    return {"ok": False}


def _repair_passed(state):
    print("[distill-session] resolution repair passed", file=sys.stderr)
    return {
        "note": state["repaired_note"],
        "report": state["repaired_report"],
        "verifier_status": "repaired",
    }


def _give_up(state):
    distill_core._log_resolution_event(
        state["session_id"], state["origin"], state["repo"], state["report"], "failed", "not_called"
    )
    return {"ok": False}


def _remember(state):
    note = state["note"]
    remember = distill_core._call_remember(
        note["title"],
        note["body"],
        state["origin"],
        state["repo"],
        note["tags"],
        note["tools"],
        note["concepts"],
        note["claims"],
        state["session_id"],
    )
    distill_core._log_resolution_event(
        state["session_id"],
        state["origin"],
        state["repo"],
        state["report"],
        state["verifier_status"],
        remember.status,
    )
    return {"ok": remember.ok}


_GRAPH = distill_graph.build(
    distill_graph.Steps(
        draft=_draft,
        skip=_skip,
        retry_language=_retry_language,
        prepare=_prepare,
        verify=_verify,
        repair_call=_repair_call,
        repair_prepare=_repair_prepare,
        repair_verify=_repair_verify,
        repair_passed=_repair_passed,
        repair_failed=_repair_failed,
        give_up=_give_up,
        remember=_remember,
    )
)


def distill_and_remember(text, origin, repo, session_id=""):
    """Distill the transcript text via local LLM and write it through ohmyboring's remember tool."""
    backstop = distill_core.BACKSTOP_CLAMP
    if len(text) > backstop:
        text, _ = transcript.clamp_text(text, backstop)
        print(
            f"[distill-session] caller passed unclamped text; cut to {len(text)} chars by the "
            f"{backstop}-char backstop. Raise the caller's own clamp, not this one.",
            file=sys.stderr,
        )

    final = _GRAPH.invoke(
        {
            "text": text,
            "origin": origin,
            "repo": repo,
            "session_id": session_id,
            "verifier_status": "pass",
            "ok": False,
        }
    )
    return final["ok"]
