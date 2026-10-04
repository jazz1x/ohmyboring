"""검증에 걸린 노트를 한 번 고쳐 쓰게 하고, 그 결과로 갈라지는 노드들."""

from __future__ import annotations

import sys

from ohmyboring.adapters import llm
from ohmyboring.distill import resolution_event
from ohmyboring.distill.prompts.repair import build_repair_prompt


def repair_call(state):
    report = state["report"]
    print(
        "[distill-session] resolution gate failed "
        f"({report.resolution}): {', '.join(report.missing)}; "
        f"claims={report.claim_count}; "
        f"evidence={len(report.evidence_tokens_kept)}/{len(report.evidence_tokens_seen)}",
        file=sys.stderr,
    )
    prompt = build_repair_prompt(
        state["text"], state["origin"], state["repo"], state["note"], report, state["resolution"]
    )
    return {"repaired": llm.call_llm(prompt)}


def repair_failed(state):
    report = state["repaired_report"]
    print(
        "[distill-session] resolution repair failed "
        f"({report.resolution}): {', '.join(report.missing)}; "
        f"claims={report.claim_count}; "
        f"evidence={len(report.evidence_tokens_kept)}/{len(report.evidence_tokens_seen)}",
        file=sys.stderr,
    )
    resolution_event.log_resolution_event(
        state["session_id"], state["origin"], state["repo"], report, "failed", "not_called"
    )
    return {"ok": False}


def repair_passed(state):
    print("[distill-session] resolution repair passed", file=sys.stderr)
    return {
        "note": state["repaired_note"],
        "report": state["repaired_report"],
        "verifier_status": "repaired",
    }


def give_up(state):
    resolution_event.log_resolution_event(
        state["session_id"], state["origin"], state["repo"], state["report"], "failed", "not_called"
    )
    return {"ok": False}
