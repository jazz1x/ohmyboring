#!/usr/bin/env python3
"""card.py's graph, dry-run, live-collaborator wiring, and single-instance lock.

The graph test drives build_graph with stub collaborators through MemorySaver + thread_id:
first invoke runs read_repairs → read_proposed → read_registers → cross_check →
read_history → advise → resolve → post_card and pauses at
the interrupt with no verdicts, each Command(resume=…) records one verdict, and the run ends
after the last proposal is judged. The resolve stub resolves a fixed map of subjects,
mirroring the live collaborator's rule that a source already shaped like a note path resolves
to itself. The search/read_note stubs stand in for the door's /search and the host vault —
each candidate subject gets one canned hit whose note has known text, so parse_advised's
quote check has something real to check against.

Run: python3 agents/slack/test_card_graph.py
"""

import contextlib
import io
import json
import os
import sys
import threading
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card  # noqa: E402
import card_live  # noqa: E402
import card_types as cc  # noqa: E402
import card_verdicts  # noqa: E402
from langgraph.types import Command  # noqa: E402

OWNER = "U_OWNER"
CARD_TS = "1.0"
CARD_CH = "C1"
SESSION = f"slack:{CARD_CH}:{CARD_TS}"

FETCH_DATA = {
    "/next_actions": {"answer": "next: 정리하고 재작성할 것", "sources": ["next_action", "loop"]},
    "/risks": {"answer": "risk: 엔진 문 응답 지연", "sources": ["f64_risk", "essential_mode"]},
    "/stalled": {"answer": "stalled: 대기 중인 병합", "sources": ["draft_wiring"]},
    "/recurrences": {
        "rows": [
            {
                "newer": {
                    "source_path": "/vault/wiki/wiki-0576.md",
                    "subject": "relay sync",
                    "predicate": "incident",
                    "value": "relay synchronization issue",
                },
                "older": [{"source_path": "/vault/wiki/wiki-0536.md"}],
            }
        ],
        "days": 30,
        "max_distance": 0.2,
        "min_days_apart": 3,
    },
}

# One hit per candidate subject — just enough for build_advice_prompt and note_texts.
CANDIDATE_HITS = {
    "f64_risk": [{"source_path": "/vault/wiki/wiki-0900.md", "snippet": "문 응답 지연", "claims": []}],
    "/vault/wiki/wiki-0576.md": [
        {"source_path": "/vault/wiki/wiki-0576.md", "snippet": "relay sync", "claims": []}
    ],
    "/vault/wiki/wiki-0536.md": [
        {"source_path": "/vault/wiki/wiki-0536.md", "snippet": "relay sync older", "claims": []}
    ],
    "essential_mode": [{"source_path": "/vault/wiki/wiki-0101.md", "snippet": "필수 모드", "claims": []}],
    "draft_wiring": [{"source_path": "/vault/wiki/wiki-0202.md", "snippet": "배선 초안", "claims": []}],
}

# Each note's own text — line 2 is what a valid quote must verify against.
NOTE_TEXTS = {
    "/vault/wiki/wiki-0900.md": "머리말\n문 응답 지연이 반복되는 원인은 타임아웃 설정이다\n끝",
    "/vault/wiki/wiki-0576.md": "머리말\nrelay synchronization issue recurred twice\n끝",
    "/vault/wiki/wiki-0536.md": "머리말\nearlier relay sync incident logged here\n끝",
    "/vault/wiki/wiki-0101.md": "머리말\nessential mode trims non essential paths\n끝",
    "/vault/wiki/wiki-0202.md": "머리말\ndraft wiring merge is still pending review\n끝",
    "/vault/wiki/note-alpha.md": "머리말\n알파 병목이 쉬는 노트인지 가리는 근거 줄입니다\n끝",
    "/vault/wiki/note-beta.md": "머리말\n베타 병목이 쉬는 노트인지 가리는 근거 줄입니다\n끝",
    "/vault/wiki/note-gamma.md": "머리말\n감마 병목이 살아 있는 제안인 근거 줄입니다\n끝",
}

# The resting-skip tests' world: three subjects, one per bottleneck register slot,
# each resolving to its own note, no recurrences, no past approvals.
SKIP_FETCH_DATA = {
    "/next_actions": {"answer": "next: 없음", "sources": []},
    "/risks": {"answer": "risk: 알파와 베타", "sources": ["alpha_subj", "beta_subj"]},
    "/stalled": {"answer": "stalled: 감마", "sources": ["gamma_subj"]},
    "/recurrences": {"rows": [], "days": 30, "max_distance": 0.2, "min_days_apart": 3},
}
SKIP_RESOLUTIONS = {
    "alpha_subj": "/vault/wiki/note-alpha.md",
    "beta_subj": "/vault/wiki/note-beta.md",
    "gamma_subj": "/vault/wiki/note-gamma.md",
}
SKIP_HITS = {
    "alpha_subj": [{"source_path": "/vault/wiki/note-alpha.md", "snippet": "알파", "claims": []}],
    "beta_subj": [{"source_path": "/vault/wiki/note-beta.md", "snippet": "베타", "claims": []}],
    "gamma_subj": [{"source_path": "/vault/wiki/note-gamma.md", "snippet": "감마", "claims": []}],
}


def _advice_json(bottleneck: str, advice: str, note: str, quote: str) -> str:
    return json.dumps(
        {
            "result": {
                "kind": "proposal",
                "bottleneck": bottleneck,
                "advice": advice,
                "evidence": [{"note": note, "quote": quote}],
            }
        },
        ensure_ascii=False,
    )


NOT_WORTH_JSON = json.dumps({"result": {"kind": "not_worth", "reason": "근거가 약하다"}}, ensure_ascii=False)


def _advice_for(subject: str) -> str:
    """A grounded Advice response for `subject`, citing exactly the note its own search hit
    carries — so it verifies regardless of which position in the candidate queue it lands on."""
    note = CANDIDATE_HITS[subject][0]["source_path"]
    quote = NOTE_TEXTS[note].splitlines()[1]
    return _advice_json(f"{subject} 병목 한 문장 열자 이상", f"{subject} 오늘 할 일 열자 이상", note, quote)


def _propose_by_subject(prompt: str) -> str:
    """A propose stub that answers whichever candidate it was actually asked about —
    positional response queues break the moment a candidate is skipped before its call."""
    for subject in CANDIDATE_QUEUE:
        if repr(subject) in prompt:
            return ADVICE[subject]
    return NOT_WORTH_JSON


def _approve_everything(prompt: str) -> str:
    """The SKIP fixture's counterpart: a grounded proposal for whichever skip-world subject
    is named in the prompt, so any call that happens returns a valid proposal."""
    for subject, hits in SKIP_HITS.items():
        if repr(subject) in prompt:
            note = hits[0]["source_path"]
            quote = NOTE_TEXTS[note].splitlines()[1]
            return _advice_json(
                f"{subject} 병목 한 문장 열자 이상", f"{subject} 오늘 할 일 열자 이상", note, quote
            )
    return NOT_WORTH_JSON


# candidate_order's order for priority=["f64_risk"]: f64_risk → wiki-0536 (recurrences
# sorts before wiki-0576) → wiki-0576 → essential_mode → draft_wiring.
CANDIDATE_QUEUE = [
    "f64_risk",
    "/vault/wiki/wiki-0536.md",
    "/vault/wiki/wiki-0576.md",
    "essential_mode",
    "draft_wiring",
]
ADVICE = {subject: _advice_for(subject) for subject in CANDIDATE_QUEUE}

# subject → note path; subjects absent from the map are Unresolved. Recurrence-style
# paths resolve to themselves, like the live collaborator.
RESOLUTIONS = {
    "f64_risk": "/vault/wiki/wiki-0900.md",
    "loop": "/vault/wiki/wiki-0101.md",
    "essential_mode": "/vault/wiki/wiki-0101.md",
    "draft_wiring": "/vault/wiki/wiki-0202.md",
}

# The happy path's candidate order is: f64_risk (priority) → wiki-0536 → wiki-0576 →
# essential_mode → draft_wiring (recurrence sources are sorted, so 0536 precedes 0576).
# Three successes stop the loop after the first three.
EXPECTED_NOTES = ["/vault/wiki/wiki-0900.md", "/vault/wiki/wiki-0536.md", "/vault/wiki/wiki-0576.md"]

# Past approvals, newest first: the first still resolves from today's registers (아직),
# the other two no longer do (했다 2 · 아직 1 — asymmetric, so a done/pending swap shows).
PAST_APPROVED = [
    cc.PastApproved(session="slack:C1:1759000000.000001", note="/vault/wiki/wiki-0900.md", at="t-1"),
    cc.PastApproved(session="slack:C1:1758000000.000002", note="/vault/wiki/wiki-9999.md", at="t-2"),
    cc.PastApproved(session="slack:C1:1757000000.000003", note="/vault/wiki/wiki-9998.md", at="t-3"),
]
PAST_SESSION = "slack:C1:1759000000.000001"

# The door's GET /repairs/split-subjects shape — one group is enough to exercise the lane.
REPAIR_GROUPS = [
    {
        "subject": "foodspring-front",
        "variants": ["foodspring front", "foodspring-front"],
        "rows": 3218,
        "notes": 212,
    }
]


def _fetch(path: str, project: str = "") -> dict:
    return FETCH_DATA[path]


class Stubs:
    """The stub collaborators, recording every call. `failures` maps a subject to the
    Unresolved reason the door would have returned (5xx, unreachable). `responses` is the
    queue of advised JSON the propose stub hands out, one per call, in order — the caller
    picks it to match whichever candidates it wants to succeed, refuse, or go unworth."""

    def __init__(
        self,
        resolutions: dict[str, str] | None = None,
        approved=None,
        paths_resolve: bool = True,
        failures: dict[str, str] | None = None,
        responses: list[str] | None = None,
        active_project_names: list[str] | None = None,
        past_verdict_pairs: list[cc.PastVerdictPair] | None = None,
        past_unanswered_pairs: list[cc.PastUnansweredPair] | None = None,
        repairs_payload: dict | None = None,
        merged_yesterday_rows: int | None = None,
        execute_repair_result: dict | None = None,
        proposed_items: list[cc.ProposedVerdict] | None = None,
    ):
        self.resolutions = resolutions if resolutions is not None else RESOLUTIONS
        self.approved_items = approved if approved is not None else PAST_APPROVED
        self.paths_resolve = paths_resolve
        self.failures = failures or {}
        self.responses = (
            list(responses) if responses is not None else [ADVICE[s] for s in CANDIDATE_QUEUE[:3]]
        )
        self.active_project_names = active_project_names if active_project_names is not None else []
        self.past_verdict_pairs = past_verdict_pairs if past_verdict_pairs is not None else []
        self.past_unanswered_pairs = past_unanswered_pairs if past_unanswered_pairs is not None else []
        self.repairs_payload = (
            repairs_payload if repairs_payload is not None else {"groups": [], "total_groups": 0}
        )
        self.merged_yesterday_rows = merged_yesterday_rows
        self.execute_repair_result = execute_repair_result if execute_repair_result is not None else {}
        self.proposed_items = proposed_items if proposed_items is not None else []
        self.propose_calls: list[str] = []
        self.search_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.past_verdicts_calls: list[int] = []
        self.records: list[tuple[str, dict]] = []
        self.execute_repair_calls: list[str] = []
        self.repairs_calls: list[int] = []

    def search(self, subject: str) -> list[dict]:
        self.search_calls.append(subject)
        return CANDIDATE_HITS.get(subject, [])

    def read_note(self, note: str) -> str | None:
        return NOTE_TEXTS.get(note)

    def propose(self, prompt: str) -> str:
        self.propose_calls.append(prompt)
        idx = len(self.propose_calls) - 1
        return self.responses[idx] if idx < len(self.responses) else NOT_WORTH_JSON

    def resolve(self, subject: str, register: str):
        self.resolve_calls.append((subject, register))
        reason = self.failures.get(subject)
        if reason is not None:
            return cc.Unresolved(subject=subject, register=register, reason=reason)
        if self.paths_resolve and subject.startswith("/"):
            return cc.ResolvedNote(subject=subject, note=subject)
        note = self.resolutions.get(subject)
        if note is None:
            return cc.Unresolved(subject=subject, register=register, reason=cc.NO_CURRENT_CLAIM)
        return cc.ResolvedNote(subject=subject, note=note)

    def approved(self, since_hours: int):
        assert since_hours == 48
        return self.approved_items

    def record(self, event: str, fields: dict) -> None:
        self.records.append((event, fields))

    def active_projects(self, active_days: int) -> list[str]:
        assert active_days == card.PROJECT_ACTIVE_DAYS
        return self.active_project_names

    def past_verdicts(self, since_hours: int) -> cc.PastCardHistory:
        self.past_verdicts_calls.append(since_hours)
        assert since_hours == card_verdicts.SUPPRESS_WINDOW_HOURS
        return cc.PastCardHistory(judged=self.past_verdict_pairs, unanswered=self.past_unanswered_pairs)

    def repairs(self, limit: int) -> dict:
        self.repairs_calls.append(limit)
        return self.repairs_payload

    def merged_yesterday(self) -> int | None:
        return self.merged_yesterday_rows

    def execute_repair(self, subject: str) -> dict:
        self.execute_repair_calls.append(subject)
        return self.execute_repair_result

    def proposed(self, since_hours: int) -> list[cc.ProposedVerdict]:
        assert since_hours == card.REVIEW_SINCE_HOURS
        return self.proposed_items


def _blocks_text(blocks) -> str:
    return json.dumps(blocks, ensure_ascii=False)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.handovers = []
        self.consumptions = []
        self.stubs = Stubs()
        self.graph = self._build(self.stubs)
        self.cfg = {"configurable": {"thread_id": "test-card"}}

    def _build(self, stubs: Stubs, lang: str = "ko"):
        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang=lang,
            repairs=stubs.repairs,
            execute_repair=stubs.execute_repair,
            merged_yesterday=stubs.merged_yesterday,
            proposed=stubs.proposed,
        )
        return card.build_graph(collabs)

    def _send(self, blocks):
        self.sends.append(blocks)
        return cc.PostedCard(channel=CARD_CH, ts=CARD_TS)

    def _handover(self, session, at, paths):
        self.handovers.append((session, at, paths))
        return {}

    def _consumption(self, session, kind, paths):
        self.consumptions.append((session, kind, paths))
        return {"edges": 1}

    def _resume(self, idx, choice):
        verdict = cc.ButtonVerdict(idx=idx, choice=choice, user=OWNER, at="t")
        return self.graph.invoke(Command(resume=verdict), self.cfg)

    def test_graph_has_the_ten_nodes(self):
        names = set(self.graph.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(
            names,
            {
                "read_repairs",
                "read_proposed",
                "read_registers",
                "cross_check",
                "read_history",
                "advise",
                "resolve",
                "post_card",
                "await_verdict",
                "record_verdict",
            },
        )

    def test_advise_stops_after_three_successful_candidates(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertIn("__interrupt__", out)
        self.assertEqual(len(self.stubs.propose_calls), 3)
        self.assertIn("f64_risk", self.stubs.propose_calls[0])
        self.assertIn("wiki-0536", self.stubs.propose_calls[1])
        self.assertIn("wiki-0576", self.stubs.propose_calls[2])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([p.note for p in out["proposals"]], EXPECTED_NOTES)
        for note in EXPECTED_NOTES:
            self.assertTrue(note.startswith("/vault/wiki/"))

    def test_a_superseded_hit_reaches_the_posted_card_as_a_label_on_its_own_evidence(self):
        old = "/vault/wiki/wiki-0536.md"
        hit = dict(CANDIDATE_HITS[old][0], superseded_by=["/vault/wiki/wiki-0576.md"])
        with mock.patch.dict(CANDIDATE_HITS, {old: [hit]}):
            self.graph.invoke({"verdicts": []}, self.cfg)
        code_pieces = [
            el["text"]
            for b in self.sends[-1]
            if b["type"] == "rich_text"
            for el in b["elements"][0]["elements"]
            if el.get("style") == {"code": True}
        ]
        marked = [p for p in code_pieces if "대체됨" in p]
        self.assertEqual(marked, ["\nwiki-0536 L2 · 대체됨 → wiki-0576"])
        self.assertEqual(len(code_pieces), 3)

    def test_notworth_candidates_are_skipped_not_counted_as_proposals(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON, NOT_WORTH_JSON] + [ADVICE[s] for s in CANDIDATE_QUEUE[2:]])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-notworth"}})
        self.assertEqual(len(stubs.propose_calls), 5)  # all five candidates tried
        self.assertEqual(len(out["proposals"]), 3)
        self.assertEqual(
            {p.subject for p in out["proposals"]},
            {"/vault/wiki/wiki-0576.md", "essential_mode", "draft_wiring"},
        )

    def test_advise_call_cap_stops_the_loop(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        original_cap = card.ADVISE_CALL_CAP
        card.ADVISE_CALL_CAP = 2
        try:
            graph = self._build(stubs)
            out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-cap"}})
        finally:
            card.ADVISE_CALL_CAP = original_cap
        self.assertEqual(len(stubs.propose_calls), 2)
        self.assertEqual(out["proposals"], [])

    def test_zero_proposals_still_sends_a_card(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-zero"}})
        self.assertEqual(out["proposals"], [])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.sends[0][0]["text"]["text"], "☀️ 오늘 제안 0")
        self.assertEqual(self.handovers[0][2], [])

    def test_resolve_drops_an_unresolved_subject_without_reproposing(self):
        # f64_risk is absent from `resolutions` — its Advice is grounded but the door has no
        # current claim for it, so cross_check also fails to resolve it, wiki-0900.md counts
        # as 했다 (not 아직), and the candidate queue has no priority section: recurrences
        # first (wiki-0536, wiki-0576), then risks (f64_risk, essential_mode), then stalled.
        stubs = Stubs(
            resolutions={
                "essential_mode": "/vault/wiki/wiki-0101.md",
                "draft_wiring": "/vault/wiki/wiki-0202.md",
            },
            responses=[
                ADVICE["/vault/wiki/wiki-0536.md"],
                ADVICE["/vault/wiki/wiki-0576.md"],
                ADVICE["f64_risk"],
            ],
        )
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-drop"}})
        # three candidates were tried and all three grounded — resolve, not advise, drops
        # f64_risk, so the loop never spends a fourth call to backfill it.
        self.assertEqual(len(stubs.propose_calls), 3)
        self.assertEqual(
            [p.subject for p in out["proposals"]], ["/vault/wiki/wiki-0536.md", "/vault/wiki/wiki-0576.md"]
        )

    def test_all_unresolved_still_sends_an_empty_card(self):
        # A dead-door world: every candidate's Advice is grounded, but none resolves to a
        # note. The card ships with 「제안 0」, never a refusal.
        stubs = Stubs(resolutions={}, paths_resolve=False)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-all-unresolved"}})
        self.assertEqual(out["proposals"], [])
        self.assertEqual(len(self.sends), 1)

    def test_handover_and_consumption_cite_note_paths(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertEqual(len(self.handovers), 1)
        self.assertEqual(self.handovers[0][0], SESSION)
        # AC4: handover must carry the union of proposal notes and evidence notes. In this
        # fixture each proposal's evidence cites its own resolved note, so the union equals
        # EXPECTED_NOTES exactly — test_handover_cites_evidence_notes_even_when_they_differ_
        # from_the_resolved_note below exercises the case where they diverge.
        self.assertEqual(self.handovers[0][2], card_verdicts.handover_paths(out["proposals"]))
        self.assertEqual(self.handovers[0][2], EXPECTED_NOTES)
        for path in self.handovers[0][2]:
            self.assertTrue(path.startswith("/vault/wiki/"))

        self._resume(0, "do")
        self.assertEqual(self.consumptions, [(SESSION, "used", [EXPECTED_NOTES[0]])])
        self._resume(1, "drop")
        self.assertEqual(
            self.consumptions,
            [(SESSION, "used", [EXPECTED_NOTES[0]]), (SESSION, "contested", [EXPECTED_NOTES[1]])],
        )
        out = self._resume(2, "defer")
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(len(self.consumptions), 2)  # defer adds no engine call
        self.assertEqual(len(self.sends), 1)  # the card went out once

    def test_handover_cites_evidence_notes_even_when_they_differ_from_the_resolved_note(self):
        # f64_risk's own past hit (and so its Advice evidence) cites wiki-0900.md, but the
        # door resolves f64_risk's *current* claim to a different note (wiki-7777.md) — a
        # proposal's evidence is not guaranteed to be the same note its subject resolves to
        # today. handover must still cite both: the owner saw the wiki-0900.md quote. The
        # propose stub here matches by the subject named in the prompt (not call position),
        # so it stays correct even though this resolutions override changes candidate order.
        def propose_by_subject(prompt: str) -> str:
            for subject in CANDIDATE_QUEUE:
                if repr(subject) in prompt:
                    return ADVICE[subject]
            return NOT_WORTH_JSON

        resolutions = dict(RESOLUTIONS)
        resolutions["f64_risk"] = "/vault/wiki/wiki-7777.md"
        stubs = Stubs(resolutions=resolutions)
        stubs.propose = propose_by_subject
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-union-diverge"}})
        f64_proposal = next(p for p in out["proposals"] if p.subject == "f64_risk")
        self.assertEqual(f64_proposal.note, "/vault/wiki/wiki-7777.md")
        self.assertEqual(f64_proposal.evidence[0].note, "/vault/wiki/wiki-0900.md")
        paths = self.handovers[0][2]
        self.assertIn("/vault/wiki/wiki-7777.md", paths)
        self.assertIn("/vault/wiki/wiki-0900.md", paths)
        self.assertEqual(paths, card_verdicts.handover_paths(out["proposals"]))

    def test_advise_stats_counts_not_worth_and_ungrounded_with_their_reasons(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON, "not json"] + [ADVICE[s] for s in CANDIDATE_QUEUE[2:]])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-stats"}})
        stats = out["advise_stats"]
        self.assertEqual(stats.calls, 5)
        self.assertEqual(stats.proposals_passed, 3)
        self.assertEqual(stats.not_worth, 1)
        self.assertEqual(stats.ungrounded, 1)
        self.assertEqual(stats.not_worth_reasons, ["근거가 약하다"])
        self.assertIn("not JSON", stats.ungrounded_reasons[0])

    def test_missing_note_bodies_speak_the_vault_dir_on_stderr(self):
        class BlindStubs(Stubs):
            def read_note(self, note: str) -> str | None:
                return None

        stubs = BlindStubs(responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-blind-vault"}})
        err = buf.getvalue()
        self.assertIn("BORING_VAULT_DIR", err)
        self.assertEqual(err.count("BORING_VAULT_DIR"), 1)  # one line, not once per candidate

    def test_confirmation_line_and_event(self):
        out = self.graph.invoke({"verdicts": []}, self.cfg)
        confirmation = out["confirmation"]
        self.assertIsNotNone(confirmation)
        self.assertEqual(confirmation.total, 3)
        self.assertEqual(confirmation.done, ["/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"])
        self.assertEqual(confirmation.pending, ["/vault/wiki/wiki-0900.md"])
        self.assertEqual(confirmation.unknown, [])
        blocks = self.sends[0]
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 2 · 아직 1")
        confirmation_events = [r for r in self.stubs.records if r[0] == "card_confirmation"]
        self.assertEqual(
            confirmation_events,
            [("card_confirmation", {"done": 2, "pending": 1, "session": PAST_SESSION})],
        )

    def test_priority_subject_leads_the_first_advice_call(self):
        self.graph.invoke({"verdicts": []}, self.cfg)
        self.assertIn("f64_risk", self.stubs.propose_calls[0])

    def test_door_failure_marks_unknown_instead_of_done(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8, failures={"f64_risk": "claim-source answered 500"})
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-500"}})
        confirmation = out["confirmation"]
        self.assertEqual(confirmation.done, [])
        self.assertEqual(confirmation.pending, [])
        self.assertEqual(
            [note for note, _ in confirmation.unknown],
            ["/vault/wiki/wiki-0900.md", "/vault/wiki/wiki-9999.md", "/vault/wiki/wiki-9998.md"],
        )
        blocks = self.sends[-1]
        self.assertEqual(blocks[1]["elements"][0]["text"], "지난 승인 3 · 했다 0 · 아직 0 · 확인불가 3")
        confirmation_events = [r for r in stubs.records if r[0] == "card_confirmation"]
        self.assertEqual(
            confirmation_events,
            [
                (
                    "card_confirmation",
                    {"done": 0, "pending": 0, "session": PAST_SESSION, "unknown": 3},
                )
            ],
        )

    def test_approved_failure_stops_the_run_before_any_send(self):
        stubs = Stubs()

        def dead_door(since_hours: int):
            raise OSError("door unreachable")

        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=dead_door,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-dead"}})
        self.assertEqual(self.sends, [])
        self.assertEqual(stubs.records, [])

    def test_send_failure_leaves_no_confirmation_event(self):
        stubs = Stubs()

        def dead_slack(blocks):
            raise OSError("slack unreachable")

        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=dead_slack,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-nosend"}})
        self.assertEqual(stubs.records, [])  # the card never went out — no confirmation log

    def test_no_past_approvals_means_no_line_no_confirmation_event(self):
        stubs = Stubs(approved=[])
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-empty"}})
        self.assertIsNone(out["confirmation"])
        self.assertNotIn("지난 승인", _blocks_text(self.sends[-1]))
        self.assertNotIn("card_confirmation", [r[0] for r in stubs.records])

    def test_card_proposal_and_card_verdict_events_fire_for_every_row(self):
        # AC4: 3 proposals, 3 presses (해·빼·미뤄) → 3 card_proposal + 3 card_verdict events,
        # plus the existing card_confirmation — 7 record calls, none dropped or duplicated.
        self.graph.invoke({"verdicts": []}, self.cfg)
        self._resume(0, "do")
        self._resume(1, "drop")
        self._resume(2, "defer")
        names = [name for name, _ in self.stubs.records]
        self.assertEqual(names.count("card_proposal"), 3)
        self.assertEqual(names.count("card_verdict"), 3)
        self.assertEqual(names.count("card_confirmation"), 1)
        self.assertEqual(len(self.stubs.records), 7)
        verdict_fields = [fields for name, fields in self.stubs.records if name == "card_verdict"]
        self.assertEqual([v["choice"] for v in verdict_fields], ["do", "drop", "defer"])
        self.assertEqual({v["card_ts"] for v in verdict_fields}, {CARD_TS})
        proposal_fields = [fields for name, fields in self.stubs.records if name == "card_proposal"]
        self.assertEqual([p["idx"] for p in proposal_fields], [0, 1, 2])

    def test_a_recently_judged_note_is_skipped_before_any_model_call(self):
        past = [
            cc.PastVerdictPair(
                note=EXPECTED_NOTES[0],
                evidence_note=EXPECTED_NOTES[0],
                evidence_line=2,
                choice="do",
                at=datetime.now(UTC).isoformat(),
            )
        ]
        stubs = Stubs(past_verdict_pairs=past)

        def propose(prompt: str) -> str:
            stubs.propose_calls.append(prompt)
            return _propose_by_subject(prompt)

        stubs.propose = propose
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-suppress"}})
        # the pair is still kept off the card, but now by advise's pre-skip: f64_risk's
        # note (wiki-0900) carries the 해 verdict, so no search and no call is spent on
        # it — the queue advances and the next three candidates fill the card.
        self.assertEqual(
            [p.note for p in out["proposals"]],
            ["/vault/wiki/wiki-0536.md", "/vault/wiki/wiki-0576.md", "/vault/wiki/wiki-0101.md"],
        )
        self.assertEqual(len(stubs.propose_calls), 3)
        self.assertIn("wiki-0536", stubs.propose_calls[0])
        self.assertEqual(out["advise_stats"].skipped_resting, 1)
        self.assertEqual(out["suppressed_count"], 0)
        self.assertEqual(stubs.past_verdicts_calls, [card_verdicts.SUPPRESS_WINDOW_HOURS])

    def test_past_verdicts_failure_stops_the_run_before_any_send(self):
        # AC5: a card that cannot read its own judged history must not ship — mirrors
        # test_approved_failure_stops_the_run_before_any_send for the other read the resolve
        # node depends on.
        stubs = Stubs()

        def dead_events(since_hours: int):
            raise OSError("events unreachable")

        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=dead_events,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-card-dead-events"}})
        self.assertEqual(self.sends, [])
        self.assertEqual(stubs.records, [])

    def test_active_projects_are_dialed_and_shared_subjects_are_deduplicated(self):
        # Every fixture project answers identical registers (the _fetch stub ignores its
        # project argument), so the global subject dedup in merge_project_candidates means
        # only the first-called project is credited with any candidates.
        stubs = Stubs(active_project_names=["proj-x", "proj-y"], responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-multi-project"}})
        self.assertEqual(out["projects"], ["proj-x", "proj-y", ""])
        self.assertEqual(set(out["per_project_candidates"]), {"proj-x"})
        self.assertEqual(out["per_project_candidates"]["proj-x"], 5)

    def test_read_repairs_populates_state_once_and_a_dead_door_refuses_the_card(self):
        # AC4: the door's GET is called exactly once, its groups land in state, and a
        # 5xx/unreachable door — same principle as /approved — refuses the card outright.
        stubs = Stubs(repairs_payload={"groups": REPAIR_GROUPS, "total_groups": 5}, merged_yesterday_rows=12)
        graph = self._build(stubs)
        out = graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-read-repairs"}})
        self.assertEqual(stubs.repairs_calls, [card_live.REPAIRS_LIMIT])
        self.assertEqual(out["repairs_total_groups"], 5)
        self.assertEqual(out["merged_yesterday_rows"], 12)
        self.assertEqual([r.subject for r in out["repairs"]], ["foodspring-front"])

        def dead_repairs(limit: int) -> dict:
            raise OSError("door unreachable")

        dead_stubs = Stubs()
        collabs = card.Collaborators(
            fetch=_fetch,
            search=dead_stubs.search,
            read_note=dead_stubs.read_note,
            propose=dead_stubs.propose,
            send=self._send,
            handover=self._handover,
            consumption=self._consumption,
            resolve=dead_stubs.resolve,
            approved=dead_stubs.approved,
            record=dead_stubs.record,
            active_projects=dead_stubs.active_projects,
            past_verdicts=dead_stubs.past_verdicts,
            lang="ko",
            repairs=dead_repairs,
            execute_repair=dead_stubs.execute_repair,
            merged_yesterday=dead_stubs.merged_yesterday,
        )
        graph = card.build_graph(collabs)
        sends_before = len(self.sends)
        with self.assertRaises(OSError):
            graph.invoke({"verdicts": []}, {"configurable": {"thread_id": "test-read-repairs-dead"}})
        self.assertEqual(len(self.sends), sends_before)

    def test_execute_repair_branch_dispatch(self):
        # AC5: a repair row's adopt calls the door's own merge and never consumption; an
        # advice row's adopt/reject does the reverse. One test, both branches.
        stubs = Stubs(
            repairs_payload={"groups": REPAIR_GROUPS, "total_groups": 1},
            execute_repair_result={"deleted_rows": 5, "reread_notes": 2, "remaining_variants": 1},
        )
        graph = self._build(stubs)
        cfg = {"configurable": {"thread_id": "test-execute-repair"}}
        out = graph.invoke({"verdicts": []}, cfg)
        self.assertEqual(len(out["repairs"]), 1)

        verdict = cc.ButtonVerdict(idx=0, choice="do", user=OWNER, at="t")
        out = graph.invoke(Command(resume=verdict), cfg)
        self.assertEqual(stubs.execute_repair_calls, ["foodspring-front"])
        self.assertEqual(self.consumptions, [])
        self.assertEqual(out["repair_results"], {0: stubs.execute_repair_result})

        n_repairs = len(out["repairs"])
        advice_verdict = cc.ButtonVerdict(idx=n_repairs, choice="do", user=OWNER, at="t")
        out = graph.invoke(Command(resume=advice_verdict), cfg)
        self.assertEqual(stubs.execute_repair_calls, ["foodspring-front"])  # unchanged
        self.assertEqual(len(self.consumptions), 1)
        self.assertEqual(self.consumptions[0][0], SESSION)
        self.assertEqual(self.consumptions[0][1], "used")

    def test_three_lanes_share_one_idx_space_and_the_run_ends_at_their_sum(self):
        # The idx space runs repairs → advice → reviews; await_verdict's `of` and the END
        # condition both count all three lanes — a review-less total is the mutant the
        # verifier names, and a run that kept waiting after every row was judged is the other.
        reviews = [
            cc.ProposedVerdict(
                session_id="sess-agent-1", note="/vault/wiki/wiki-0701.md", kind="used", at="t-1"
            ),
            cc.ProposedVerdict(
                session_id="sess-agent-2", note="/vault/wiki/wiki-0702.md", kind="contested", at="t-2"
            ),
        ]
        stubs = Stubs(repairs_payload={"groups": REPAIR_GROUPS, "total_groups": 1}, proposed_items=reviews)
        graph = self._build(stubs)
        cfg = {"configurable": {"thread_id": "test-three-lanes"}}
        out = graph.invoke({"verdicts": []}, cfg)
        total = len(out["repairs"]) + len(out["proposals"]) + len(out["reviews"])
        self.assertEqual(total, 1 + 3 + 2)
        self.assertEqual(out["__interrupt__"][0].value["of"], total)
        for idx in range(total):
            verdict = cc.ButtonVerdict(idx=idx, choice="do", user=OWNER, at="t")
            out = graph.invoke(Command(resume=verdict), cfg)
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(len(out["verdicts"]), total)

    def test_review_lane_flip_judges_the_proposing_session_with_the_opposite_kind(self):
        reviews = [
            cc.ProposedVerdict(
                session_id="sess-agent-1", note="/vault/wiki/wiki-0700.md", kind="used", at="t-1"
            )
        ]
        stubs = Stubs(proposed_items=reviews)
        graph = self._build(stubs)
        cfg = {"configurable": {"thread_id": "test-review-flip"}}
        out = graph.invoke({"verdicts": []}, cfg)
        n_slots = len(out["repairs"]) + len(out["proposals"])
        verdict = cc.ButtonVerdict(idx=n_slots, choice="drop", user=OWNER, at="t")
        graph.invoke(Command(resume=verdict), cfg)
        self.assertEqual(self.consumptions[-1], ("sess-agent-1", "contested", ["/vault/wiki/wiki-0700.md"]))
        reviewed = [fields for name, fields in stubs.records if name == "verdict_reviewed"]
        self.assertEqual(
            reviewed,
            [
                {
                    "session_id": "sess-agent-1",
                    "note": "/vault/wiki/wiki-0700.md",
                    "proposed_kind": "used",
                    "card_ts": CARD_TS,
                    "choice": "flip",
                }
            ],
        )

    def test_review_lane_agree_records_only_the_review_event(self):
        reviews = [
            cc.ProposedVerdict(
                session_id="sess-agent-2", note="/vault/wiki/wiki-0800.md", kind="contested", at="t-2"
            )
        ]
        stubs = Stubs(proposed_items=reviews)
        graph = self._build(stubs)
        cfg = {"configurable": {"thread_id": "test-review-agree"}}
        out = graph.invoke({"verdicts": []}, cfg)
        n_slots = len(out["repairs"]) + len(out["proposals"])
        consumption_calls = len(self.consumptions)
        verdict = cc.ButtonVerdict(idx=n_slots, choice="do", user=OWNER, at="t")
        graph.invoke(Command(resume=verdict), cfg)
        self.assertEqual(len(self.consumptions), consumption_calls)  # agree never touches the graph
        reviewed = [fields for name, fields in stubs.records if name == "verdict_reviewed"]
        self.assertEqual(
            [(r["choice"], r["proposed_kind"], r["session_id"], r["note"]) for r in reviewed],
            [("agree", "contested", "sess-agent-2", "/vault/wiki/wiki-0800.md")],
        )

    def test_card_verdict_event_fires_only_for_an_advice_lane_press(self):
        # r3.1: card_proposal events exist only for advice rows, and card_live's
        # _live_past_verdicts joins card_verdict back to them by (card_ts, idx) — raising
        # on a verdict with no proposal. A press in the repair or review lane must leave
        # no card_verdict at all: one orphan is a next-morning card that refuses to ship.
        reviews = [
            cc.ProposedVerdict(
                session_id="sess-agree", note="/vault/wiki/wiki-0700.md", kind="used", at="t-1"
            ),
            cc.ProposedVerdict(
                session_id="sess-flip", note="/vault/wiki/wiki-0701.md", kind="contested", at="t-2"
            ),
        ]
        stubs = Stubs(
            repairs_payload={"groups": REPAIR_GROUPS, "total_groups": 1},
            proposed_items=reviews,
            execute_repair_result={"deleted_rows": 5, "reread_notes": 2, "remaining_variants": 1},
        )
        graph = self._build(stubs)
        cfg = {"configurable": {"thread_id": "test-verdict-event-lanes"}}
        out = graph.invoke({"verdicts": []}, cfg)
        self.assertEqual((len(out["repairs"]), len(out["proposals"]), len(out["reviews"])), (1, 3, 2))

        presses = [
            (0, "do"),  # execute lane: adopt → the door's own merge, no card_verdict
            (1, "do"),  # advice lane: the only lane whose press records a card_verdict
            (4, "do"),  # review lane: agree → the verdict_reviewed event only
            (5, "drop"),  # review lane: flip → verdict_reviewed + the opposite-kind edge
        ]
        for idx, choice in presses:
            verdict = cc.ButtonVerdict(idx=idx, choice=choice, user=OWNER, at="t")
            graph.invoke(Command(resume=verdict), cfg)

        verdict_events = [fields for name, fields in stubs.records if name == "card_verdict"]
        self.assertEqual(verdict_events, [{"card_ts": CARD_TS, "idx": 1, "choice": "do"}])
        reviewed = [fields for name, fields in stubs.records if name == "verdict_reviewed"]
        self.assertEqual(
            [(r["session_id"], r["choice"]) for r in reviewed],
            [("sess-agree", "agree"), ("sess-flip", "flip")],
        )
        self.assertEqual(stubs.execute_repair_calls, ["foodspring-front"])
        self.assertEqual(
            self.consumptions,
            [
                (SESSION, "used", [EXPECTED_NOTES[0]]),
                ("sess-flip", "used", ["/vault/wiki/wiki-0701.md"]),
            ],
        )
        names = [name for name, _ in stubs.records]
        self.assertEqual(
            names,
            ["card_proposal"] * 3 + ["card_confirmation"] + ["card_verdict"] + ["verdict_reviewed"] * 2,
        )


class SkipStubs(Stubs):
    """Stubs over the three-subject skip world — search answers from SKIP_HITS."""

    def search(self, subject: str) -> list[dict]:
        self.search_calls.append(subject)
        return SKIP_HITS.get(subject, [])


class RestingNoteSkipTests(unittest.TestCase):
    """advise's pre-skip: a candidate whose note is resting must not cost a search or a
    model call — recurrences carry their own path, everything else resolves through the
    door, and an unresolvable subject is never skipped on this rule (today's behaviour)."""

    def setUp(self):
        self.sends = []

    def _build(self, stubs: Stubs):
        collabs = card.Collaborators(
            fetch=lambda path, project="": SKIP_FETCH_DATA[path],
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=lambda blocks: (self.sends.append(blocks), cc.PostedCard(channel=CARD_CH, ts=CARD_TS))[1],
            handover=lambda session, at, paths: {},
            consumption=lambda session, kind, paths: {},
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
            repairs=stubs.repairs,
            execute_repair=stubs.execute_repair,
            merged_yesterday=stubs.merged_yesterday,
            proposed=stubs.proposed,
        )
        return card.build_graph(collabs)

    def _stubs(self, **kwargs) -> SkipStubs:
        stubs = SkipStubs(resolutions=SKIP_RESOLUTIONS, approved=[], **kwargs)

        def propose(prompt: str) -> str:
            stubs.propose_calls.append(prompt)
            return _approve_everything(prompt)

        stubs.propose = propose
        return stubs

    def test_resting_notes_are_skipped_before_any_model_call(self):
        # alpha's note is 해/빼-judged within 7 days, beta's shown-but-unanswered within
        # 사흘 — both are skipped, the model is asked only about gamma.
        now = datetime.now(UTC)
        stubs = self._stubs(
            past_verdict_pairs=[
                cc.PastVerdictPair(
                    note="/vault/wiki/note-alpha.md",
                    evidence_note="/vault/wiki/note-alpha.md",
                    evidence_line=2,
                    choice="drop",
                    at=now.isoformat(),
                )
            ],
            past_unanswered_pairs=[
                cc.PastUnansweredPair(
                    note="/vault/wiki/note-beta.md",
                    evidence_note="/vault/wiki/note-beta.md",
                    evidence_line=2,
                    at=now.isoformat(),
                )
            ],
        )
        out = self._build(stubs).invoke({"verdicts": []}, {"configurable": {"thread_id": "test-rest-skip"}})
        self.assertEqual(len(stubs.propose_calls), 1)
        self.assertIn("gamma_subj", stubs.propose_calls[0])
        self.assertEqual([p.note for p in out["proposals"]], ["/vault/wiki/note-gamma.md"])
        self.assertEqual(out["advise_stats"].skipped_resting, 2)
        self.assertEqual(stubs.past_verdicts_calls, [card_verdicts.SUPPRESS_WINDOW_HOURS])

    def test_a_deferred_note_is_not_skipped(self):
        # 미뤄 판정이 있는 노트는 쉬지 않는다 — alpha 는 그대로 호출된다.
        now = datetime.now(UTC)
        stubs = self._stubs(
            past_verdict_pairs=[
                cc.PastVerdictPair(
                    note="/vault/wiki/note-alpha.md",
                    evidence_note="/vault/wiki/note-alpha.md",
                    evidence_line=2,
                    choice="defer",
                    at=now.isoformat(),
                )
            ]
        )
        out = self._build(stubs).invoke({"verdicts": []}, {"configurable": {"thread_id": "test-rest-defer"}})
        self.assertEqual(len(stubs.propose_calls), 3)
        self.assertIn("alpha_subj", stubs.propose_calls[0])
        self.assertEqual(out["advise_stats"].skipped_resting, 0)
        self.assertEqual(len(out["proposals"]), 3)

    def test_an_unanswered_pair_older_than_the_rest_window_is_not_skipped(self):
        # 사흘을 넘긴 무응답 짝은 쉬는 것이 아니다 — alpha 는 그대로 호출된다.
        at = (datetime.now(UTC) - timedelta(hours=card_verdicts.REST_HOURS + 1)).isoformat()
        stubs = self._stubs(
            past_unanswered_pairs=[
                cc.PastUnansweredPair(
                    note="/vault/wiki/note-alpha.md",
                    evidence_note="/vault/wiki/note-alpha.md",
                    evidence_line=2,
                    at=at,
                )
            ]
        )
        out = self._build(stubs).invoke({"verdicts": []}, {"configurable": {"thread_id": "test-rest-old"}})
        self.assertEqual(len(stubs.propose_calls), 3)
        self.assertIn("alpha_subj", stubs.propose_calls[0])
        self.assertEqual(out["advise_stats"].skipped_resting, 0)
        self.assertEqual(len(out["proposals"]), 3)


class ListenerTests(unittest.TestCase):
    """r3.1: the socket listener bounds a press by n_total = repairs + proposals + reviews —
    one shared idx space across all three lanes. A listener that drops the review lane from
    its total rejects the review rows' own buttons as 'no proposal', and one that drops the
    repair lane over-accepts past the card's end."""

    class _FakeClient:
        def __init__(self):
            self.acks = 0

        def send_socket_mode_response(self, response):
            self.acks += 1

    class _Req:
        def __init__(self, payload):
            self.envelope_id = "e-1"
            self.type = "interactive"
            self.payload = payload

    @staticmethod
    def _press(idx: int, choice: str = "do", user: str = OWNER) -> dict:
        return {
            "type": "block_actions",
            "message": {"ts": CARD_TS},
            "user": {"id": user},
            "actions": [{"action_id": f"card:{idx}:{choice}"}],
        }

    def test_n_total_covers_all_three_lanes(self):
        holder = card._Holder()
        holder.message = cc.PostedCard(channel=CARD_CH, ts=CARD_TS)
        holder.repairs = [object()]
        holder.proposals = [object(), object(), object()]
        holder.reviews = [object(), object()]
        listener = card._listener(holder, OWNER)
        client = self._FakeClient()
        buf = io.StringIO()

        with contextlib.redirect_stderr(buf):
            listener(client, self._Req(self._press(5)))  # the last review slot → queued
            listener(client, self._Req(self._press(6)))  # one past the end → rejected

        self.assertEqual(client.acks, 2)
        self.assertEqual(holder.queue.qsize(), 1)
        verdict = holder.queue.get_nowait()
        self.assertEqual((verdict.idx, verdict.choice, verdict.user), (5, "do", OWNER))
        self.assertIn("no proposal 6", buf.getvalue())


class AwaitVerdictsTests(unittest.TestCase):
    """AC6: CARD_WAIT_HOURS=0 (or expired) ends the wait immediately, touching Slack 0 times,
    leaving whatever was unanswered exactly as it is."""

    class _FakeWeb:
        def __init__(self):
            self.chat_update_calls = 0

        def chat_update(self, **kwargs):
            self.chat_update_calls += 1

    def test_zero_wait_hours_returns_immediately_without_a_chat_update(self):
        holder = card._Holder()
        holder.message = cc.PostedCard(channel="C1", ts=CARD_TS)
        state = {
            "proposals": [],
            "verdicts": [],
            "lang": "ko",
            "confirmation": None,
            "repairs": [],
            "repairs_total_groups": 0,
            "merged_yesterday_rows": None,
            "repair_results": {},
            "reviews": [],
        }
        web = self._FakeWeb()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = card._await_verdicts(
                holder, graph=None, config={}, state=state, web_client=web, wait_hours=0
            )
        self.assertEqual(web.chat_update_calls, 0)
        self.assertIs(out, state)

    def test_positive_wait_with_empty_queue_returns_promptly_not_hangs(self):
        # M6: a mutant that drops `timeout=remaining` from `holder.queue.get(...)` blocks
        # forever on an empty queue. wait_hours=0 (the test above) never reaches that call at
        # all — remaining<=0 breaks first — so it cannot see that mutant. A small positive
        # wait does reach queue.get, and running it on a thread with a bounded join means a
        # hung mutant fails this test instead of hanging the whole suite.
        holder = card._Holder()
        holder.message = cc.PostedCard(channel="C1", ts=CARD_TS)
        proposal = cc.Proposal(
            subject="주어",
            note="/n1.md",
            register="stalled",
            bottleneck="병목 문장 열자 이상입니다",
            advice="조언 문장 열자 이상입니다",
            evidence=[cc.Evidence(note="/n1.md", quote="근거 인용문 열두자 이상", line=1)],
        )
        state = {
            "proposals": [proposal],
            "verdicts": [],
            "lang": "ko",
            "confirmation": None,
            "repairs": [],
            "repairs_total_groups": 0,
            "merged_yesterday_rows": None,
            "repair_results": {},
            "reviews": [],
        }
        web = self._FakeWeb()
        buf = io.StringIO()
        result: dict = {}

        def target():
            with contextlib.redirect_stderr(buf):
                result["state"] = card._await_verdicts(
                    holder, graph=None, config={}, state=state, web_client=web, wait_hours=1e-4
                )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive(), "_await_verdicts hung — queue.get's timeout is missing")
        self.assertIn("state", result)
        self.assertEqual(web.chat_update_calls, 0)
        self.assertIn("1건", buf.getvalue())


class DryRunWiringTests(unittest.TestCase):
    """AC5: CARD_DRY_RUN must run the graph with the dry collaborators — never the live Slack
    send, the live handover, or the live event log. A mutant that wired `_run_dry`'s
    Collaborators to `send=_env_send, handover=card_live._live_handover` survived all existing
    tests because nothing exercised `_run_dry` directly; this drives it end to end with every
    live network/model seam stubbed and asserts which functions were actually called."""

    def test_dry_run_never_calls_live_send_handover_or_record(self):
        calls = {"live_send": 0, "live_handover": 0, "live_record": 0}
        dry_calls = {"send": 0, "handover": 0, "record": 0}

        def fake_env_send(blocks):
            calls["live_send"] += 1
            raise AssertionError("dry run must not call the live Slack send")

        def fake_live_handover(session, at, paths):
            calls["live_handover"] += 1
            raise AssertionError("dry run must not call the live handover")

        def fake_live_record(event, fields):
            calls["live_record"] += 1
            raise AssertionError("dry run must not call the live event log")

        real_dry_send, real_dry_handover, real_dry_record = (
            card._dry_send,
            card._dry_handover,
            card._dry_record,
        )

        def counting_dry_send(blocks):
            dry_calls["send"] += 1
            return real_dry_send(blocks)

        def counting_dry_handover(session, at, paths):
            dry_calls["handover"] += 1
            return real_dry_handover(session, at, paths)

        def counting_dry_record(event, fields):
            dry_calls["record"] += 1
            return real_dry_record(event, fields)

        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        buf = io.StringIO()

        with (
            mock.patch.object(card, "_env_send", side_effect=fake_env_send),
            mock.patch.object(card_live, "_live_handover", side_effect=fake_live_handover),
            mock.patch.object(card_live, "_live_record", side_effect=fake_live_record),
            mock.patch.object(card, "_dry_send", side_effect=counting_dry_send),
            mock.patch.object(card, "_dry_handover", side_effect=counting_dry_handover),
            mock.patch.object(card, "_dry_record", side_effect=counting_dry_record),
            mock.patch.object(card_live, "_live_fetch", side_effect=_fetch),
            mock.patch.object(card_live, "_live_search", side_effect=stubs.search),
            mock.patch.object(card_live, "_live_read_note", side_effect=stubs.read_note),
            mock.patch.object(card_live, "_live_resolve", side_effect=stubs.resolve),
            mock.patch.object(card_live, "_live_approved", side_effect=stubs.approved),
            mock.patch.object(card_live, "_live_active_projects", side_effect=stubs.active_projects),
            mock.patch.object(card_live, "_live_past_verdicts", side_effect=stubs.past_verdicts),
            mock.patch.object(card_live, "_live_proposed", side_effect=stubs.proposed),
            mock.patch.object(
                card_live, "_live_repairs", side_effect=lambda limit: {"groups": [], "total_groups": 0}
            ),
            mock.patch.object(card_live, "_live_merged_yesterday", side_effect=lambda: None),
            mock.patch.object(card, "make_propose", return_value=stubs.propose),
            contextlib.redirect_stdout(buf),
        ):
            rc = card._run_dry()

        self.assertEqual(rc, 0)
        self.assertEqual(calls, {"live_send": 0, "live_handover": 0, "live_record": 0})
        self.assertEqual(dry_calls["send"], 1)
        self.assertEqual(dry_calls["handover"], 1)
        self.assertEqual(dry_calls["record"], 1)  # PAST_APPROVED is non-empty → one confirmation event

        # AC5 telemetry: the dry-run JSON also carries the counters the contract asks the
        # executor to be able to quote back.
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["calls"], 5)
        self.assertEqual(payload["proposals_passed"], 0)
        self.assertEqual(payload["not_worth"], 5)
        self.assertEqual(payload["ungrounded"], 0)
        self.assertEqual(len(payload["not_worth_reasons"]), 5)


class LiveFetchTests(unittest.TestCase):
    """M1: _live_fetch must send `project` in the POST body — a mutant that calls
    `_retry("POST", path, {})` (project dropped) makes the whole project axis silently
    degrade to identical unfiltered reads. Patches DrudgeClient._retry itself, not
    _live_fetch, so it observes exactly what goes over the wire."""

    def test_project_is_always_sent_in_the_post_body(self):
        calls: list[tuple[str, str, dict]] = []

        def fake_retry(self, method, path, payload=None, timeout=None):
            calls.append((method, path, payload))
            return {"rows": []} if path == "/recurrences" else {"answer": "", "sources": []}

        with mock.patch.object(card_live.DrudgeClient, "_retry", fake_retry):
            card_live._live_fetch("/risks", "proj-a")
            card_live._live_fetch("/recurrences", "")
        self.assertIn(("POST", "/risks", {"project": "proj-a"}), calls)
        self.assertIn(("POST", "/recurrences", {"project": ""}), calls)


class EnvSendTests(unittest.TestCase):
    """_env_send passes a non-empty top-level text (Slack's push/notification fallback),
    read from the card's own first block rather than rebuilt."""

    def test_env_send_passes_the_header_text_as_top_level_text(self):
        env = mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "tok", "SLACK_CARD_CHANNEL": CARD_CH})
        web = mock.MagicMock()
        web.chat_postMessage.return_value = {"ts": CARD_TS}
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "오늘의 카드 3"}}]
        with env, mock.patch("slack_sdk.web.WebClient", return_value=web):
            card._env_send(blocks)
        self.assertEqual(web.chat_postMessage.call_args.kwargs["text"], "오늘의 카드 3")


class LiveConsumptionTests(unittest.TestCase):
    """The card button's verdict is the owner's own judgement. _live_consumption must send
    judge="owner" on every consumption payload, with the owner token beside it — the engine
    refuses an owner judge that arrives without one."""

    def test_button_verdict_carries_judge_owner_and_the_token(self):
        calls: list[tuple[str, str, dict, dict]] = []

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            payload = json.loads(req.data)
            calls.append((req.get_method(), req.full_url, payload, dict(req.header_items())))
            return _Resp(
                json.dumps(
                    {
                        "session": payload["session_id"],
                        "used": 1,
                        "contested": 0,
                        "supersedes": 0,
                        "unknown": 0,
                    }
                ).encode()
            )

        with (
            mock.patch.dict(os.environ, {"BORING_OWNER_TOKEN": "tok-owner"}),
            mock.patch("urllib.request.urlopen", fake_urlopen),
        ):
            card_live._live_consumption("sess-1", "used", ["/vault/wiki/wiki-0001.md"])
            card_live._live_consumption("sess-1", "contested", ["/vault/wiki/wiki-0002.md"])
        (method, url, used_payload, used_headers) = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/consumption"))
        self.assertEqual(used_payload["judge"], "owner")
        self.assertEqual(used_payload["used"], ["/vault/wiki/wiki-0001.md"])
        self.assertNotIn("verdict", used_payload)
        self.assertEqual(used_headers.get("X-boring-owner-token"), "tok-owner")
        (_, _, contested_payload, contested_headers) = calls[1]
        self.assertEqual(contested_payload["judge"], "owner")
        self.assertEqual(contested_payload["contested"], ["/vault/wiki/wiki-0002.md"])
        self.assertEqual(contested_headers.get("X-boring-owner-token"), "tok-owner")


class LiveExecuteRepairTests(unittest.TestCase):
    """F2 (2026-09-22): the door's own 502 (sync failed, but delete+update already
    committed) must become a RepairFailed value carrying those committed counts — never an
    exception that reaches main()'s `except OSError: return 3` and kills the whole card over
    one button."""

    def test_engine_502_becomes_a_repairfailed_value_with_the_committed_counts(self):
        body = json.dumps(
            {
                "subject": "foodspring-front",
                "deleted_rows": 5,
                "reread_notes": 2,
                "remaining_variants": None,
                "sync": {"error": "engine unreachable: connection refused"},
            }
        ).encode()

        def fake_urlopen(req, timeout=None):
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(body))

        with (
            mock.patch.dict(os.environ, {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            result = card_live._live_execute_repair("foodspring-front")

        self.assertIsInstance(result, cc.RepairFailed)
        self.assertEqual(result.deleted_rows, 5)
        self.assertEqual(result.reread_notes, 2)
        self.assertEqual(result.reason, "engine unreachable: connection refused")

        # the graph itself must keep going: a RepairFailed value flowing through
        # record_verdict raises nothing and the run reaches the next interrupt.
        stubs = Stubs(repairs_payload={"groups": REPAIR_GROUPS, "total_groups": 1})
        stubs.execute_repair = lambda subject: result  # type: ignore[method-assign]
        sends: list = []
        collabs = card.Collaborators(
            fetch=_fetch,
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=lambda blocks: (sends.append(blocks), cc.PostedCard(channel=CARD_CH, ts=CARD_TS))[1],
            handover=lambda session, at, paths: {},
            consumption=lambda session, kind, paths: {},
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
            repairs=stubs.repairs,
            execute_repair=stubs.execute_repair,
            merged_yesterday=stubs.merged_yesterday,
        )
        graph = card.build_graph(collabs)
        cfg = {"configurable": {"thread_id": "test-repair-failed-continues"}}
        graph.invoke({"verdicts": []}, cfg)
        verdict = cc.ButtonVerdict(idx=0, choice="do", user=OWNER, at="t")
        out = graph.invoke(Command(resume=verdict), cfg)
        self.assertIn("__interrupt__", out)  # still waiting on the advice rows — not dead
        self.assertEqual(out["repair_results"][0], result)

    def test_timeout_becomes_a_repairunanswered_value_with_no_counts(self):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        with (
            mock.patch.dict(os.environ, {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            result = card_live._live_execute_repair("foodspring-front")

        self.assertIsInstance(result, cc.RepairUnanswered)
        self.assertIn("door unreachable", result.reason)
        self.assertFalse(hasattr(result, "deleted_rows"))

    def test_engine_502_without_json_becomes_a_repairunanswered_value(self):
        def fake_urlopen(req, timeout=None):
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"Bad Gateway"))

        with (
            mock.patch.dict(os.environ, {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            result = card_live._live_execute_repair("foodspring-front")

        self.assertIsInstance(result, cc.RepairUnanswered)
        self.assertIn("door answered 502", result.reason)
        self.assertFalse(hasattr(result, "deleted_rows"))

    @staticmethod
    def _post(env: dict, answer: dict):
        seen: list = []

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return io.BytesIO(json.dumps(answer).encode())

        with (
            mock.patch.dict(os.environ, {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            os.environ.pop("BORING_OWNER_TOKEN", None)
            os.environ.update(env)
            result = card_live._live_execute_repair("foodspring-front")
        return result, seen[0]

    def test_the_merge_button_carries_the_owner_token_and_shows_what_the_door_held(self):
        answer = {"deleted_rows": 5, "reread_notes": 2, "owner_held": ["/vault/wiki/wiki-0001.md"]}
        result, req = self._post({"BORING_OWNER_TOKEN": "tok-owner"}, answer)
        self.assertEqual(req.get_header("X-boring-owner-token"), "tok-owner")
        self.assertEqual(result.owner_held, ["/vault/wiki/wiki-0001.md"])

    def test_without_a_token_or_a_held_field_the_merge_still_answers(self):
        result, req = self._post({}, {"deleted_rows": 5, "reread_notes": 2})
        self.assertIsNone(req.get_header("X-boring-owner-token"))
        self.assertEqual(result, cc.RepairDone(subject="foodspring-front", deleted_rows=5, reread_notes=2))


class LiveDoorFetchNamesTheUrl(unittest.TestCase):
    """card.py's refusal line folds every OSError into one `[card] 카드 거부:` row, and
    urllib's own message (`HTTP Error 404: Not Found`, `<urlopen error …>`) carries no
    URL — a dead door route was unanswerable from the log (the 09-24/25 404s). _door_json
    re-raises with the URL in front: same OSError kind, card.py's catching site untouched."""

    def test_a_door_404_names_the_url_it_refused(self):
        def fake_urlopen(url, timeout=None):
            import urllib.error

            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))

        with (
            mock.patch.dict(os.environ, {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            with self.assertRaises(OSError) as ctx:
                card_live._live_approved(24)

        self.assertIn("http://door.invalid/approved?since_hours=24", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    def test_a_good_answer_still_parses_through_the_helper(self):
        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(url, timeout=None):
            return _Resp(
                json.dumps(
                    {
                        "approved": [
                            {
                                "session": "s",
                                "note": "/vault/wiki/wiki-0001.md",
                                "at": "2026-09-25T00:00:00Z",
                            }
                        ]
                    }
                ).encode()
            )

        with (
            mock.patch.dict(os.environ, {"BORING_DOOR_URL": "http://door.invalid"}),
            mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            result = card_live._live_approved(24)

        self.assertEqual(
            result,
            [cc.PastApproved(session="s", note="/vault/wiki/wiki-0001.md", at="2026-09-25T00:00:00Z")],
        )


class LivePastVerdictsTests(unittest.TestCase):
    """F5: a malformed or unjoined card_verdict/card_proposal row is a visible failure
    (ValueError), never a silently skipped one, and the proposal window must be wider than
    the verdict window (CARD_WAIT_HOURS)."""

    @staticmethod
    def _events(proposals: list[dict], verdicts: list[dict]):
        def fake(event_name: str, since_hours: int):
            return proposals if event_name == "card_proposal" else verdicts

        return fake

    def test_unmatched_verdict_raises(self):
        verdicts = [{"attributes": {"card_ts": "1.0", "idx": 0, "choice": "do"}, "observed_at": "t"}]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events([], verdicts)):
            with self.assertRaises(ValueError):
                card_live._live_past_verdicts(168)

    def test_proposal_without_evidence_raises(self):
        proposals = [{"attributes": {"card_ts": "1.0", "idx": 0, "note": "/n.md", "evidence": []}}]
        verdicts = [{"attributes": {"card_ts": "1.0", "idx": 0, "choice": "do"}, "observed_at": "t"}]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(proposals, verdicts)):
            with self.assertRaises(ValueError):
                card_live._live_past_verdicts(168)

    def test_missing_choice_raises(self):
        proposals = [
            {
                "attributes": {
                    "card_ts": "1.0",
                    "idx": 0,
                    "note": "/n.md",
                    "evidence": [{"note": "/e.md", "line": 2}],
                }
            }
        ]
        verdicts = [{"attributes": {"card_ts": "1.0", "idx": 0}, "observed_at": "t"}]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(proposals, verdicts)):
            with self.assertRaises(ValueError):
                card_live._live_past_verdicts(168)

    def test_well_formed_pair_parses(self):
        proposals = [
            {
                "attributes": {
                    "card_ts": "1.0",
                    "idx": 0,
                    "note": "/n.md",
                    "evidence": [{"note": "/e.md", "line": 4}],
                }
            }
        ]
        verdicts = [
            {
                "attributes": {"card_ts": "1.0", "idx": 0, "choice": "drop"},
                "observed_at": "2026-09-22T00:00:00+00:00",
            }
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(proposals, verdicts)):
            out = card_live._live_past_verdicts(168)
        self.assertEqual(len(out.judged), 1)
        self.assertEqual(
            (
                out.judged[0].note,
                out.judged[0].evidence_note,
                out.judged[0].evidence_line,
                out.judged[0].choice,
            ),
            ("/n.md", "/e.md", 4, "drop"),
        )
        self.assertEqual(out.unanswered, [])

    def test_a_proposal_without_a_verdict_becomes_an_unanswered_pair(self):
        # the rest rule's read side: a shown-but-never-judged proposal (no card_verdict for
        # its card_ts+idx) comes back as an unanswered pair at the proposal's own time; a
        # proposal with a verdict — 미뤄 포함 — is judged, never unanswered.
        proposals = [
            {
                "attributes": {
                    "card_ts": "1.0",
                    "idx": 0,
                    "note": "/n.md",
                    "evidence": [{"note": "/e.md", "line": 4}],
                },
                "observed_at": "2026-09-25T08:23:00+00:00",
            },
            {
                "attributes": {
                    "card_ts": "1.0",
                    "idx": 1,
                    "note": "/n2.md",
                    "evidence": [{"note": "/e2.md", "line": 7}],
                },
                "observed_at": "2026-09-24T08:23:00+00:00",
            },
        ]
        verdicts = [
            {
                "attributes": {"card_ts": "1.0", "idx": 1, "choice": "defer"},
                "observed_at": "2026-09-24T08:24:00+00:00",
            }
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(proposals, verdicts)):
            out = card_live._live_past_verdicts(168)
        self.assertEqual(
            [(p.note, p.evidence_note, p.evidence_line, p.choice) for p in out.judged],
            [("/n2.md", "/e2.md", 7, "defer")],
        )
        self.assertEqual(
            [(p.note, p.evidence_note, p.evidence_line, p.at) for p in out.unanswered],
            [("/n.md", "/e.md", 4, "2026-09-25T08:23:00+00:00")],
        )

    def test_proposal_window_is_wider_than_the_verdict_window(self):
        seen: dict[str, int] = {}

        def fake(event_name: str, since_hours: int):
            seen[event_name] = since_hours
            return []

        with mock.patch.object(card_live, "_live_events", side_effect=fake):
            card_live._live_past_verdicts(168)
        self.assertEqual(seen["card_verdict"], 168)
        self.assertGreater(seen["card_proposal"], 168)


class LiveProposedTests(unittest.TestCase):
    """r3.1: the review lane's surviving mutations — the verdict_proposed rows come back
    contested first, newest first, capped at REVIEW_LIMIT. A mutant dropping the contested
    sort, reversing the newest-first order, or removing the cap must each kill this."""

    @staticmethod
    def _events(rows: list[dict]):
        def fake(event_name: str, since_hours: int):
            assert event_name == "verdict_proposed"
            return rows

        return fake

    def test_contested_first_then_newest_first_capped_at_review_limit(self):
        rows = [
            {
                "attributes": {"session_id": f"s-{kind}-{i}", "note": f"/n{i}.md", "kind": kind},
                "observed_at": f"2026-09-2{i}T00:00:00+00:00",
            }
            for i, kind in enumerate(["used", "contested", "used", "contested", "contested"], start=1)
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows)):
            out = card_live._live_proposed(24)
        self.assertEqual(
            [p.session_id for p in out],
            ["s-contested-5", "s-contested-4", "s-contested-2"],
        )
        self.assertEqual(len(out), card_live.REVIEW_LIMIT)


class SingleInstanceLockTests(unittest.TestCase):
    """F7: a second card.py on the same Slack app token must not open a second Socket Mode
    listener — refused with the first pid, not a silent double-listen."""

    def test_second_lock_attempt_is_refused_with_the_first_pid(self):
        token = f"xapp-test-{os.getpid()}-{id(self)}"
        fh1, holder1 = card._acquire_single_instance_lock(token)
        self.assertIsNotNone(fh1)
        self.assertIsNone(holder1)
        try:
            fh2, holder2 = card._acquire_single_instance_lock(token)
            self.assertIsNone(fh2)
            self.assertEqual(holder2, str(os.getpid()))
        finally:
            fh1.close()
            os.remove(card._lock_path(token))

    def test_lock_is_free_again_after_release(self):
        token = f"xapp-test-release-{os.getpid()}-{id(self)}"
        fh1, _ = card._acquire_single_instance_lock(token)
        fh1.close()
        try:
            fh2, holder2 = card._acquire_single_instance_lock(token)
            self.assertIsNotNone(fh2)
            self.assertIsNone(holder2)
            fh2.close()
        finally:
            os.remove(card._lock_path(token))


if __name__ == "__main__":
    unittest.main()
