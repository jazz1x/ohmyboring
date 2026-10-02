#!/usr/bin/env python3
"""card.py's graph, dry-run, live-collaborator wiring, and main().

The graph test drives build_graph with stub collaborators: one invoke runs
read_repairs → read_proposed → read_registers → cross_check → read_history → advise →
resolve → post_card and ends — the graph posts the card and stops; a button press never
reaches this process (hermes owns the only socket and answers the press with the same
card_press.effects decision table, tested in test_card_press and the plugin's
test_boring_card). The resolve stub resolves a fixed map of subjects, mirroring the live
collaborator's rule that a source already shaped like a note path resolves to itself. The
search/read_note stubs stand in for the door's /search and the host vault — each
candidate subject gets one canned hit whose note has known text, so parse_advised's
quote check has something real to check against.

Run: python3 agents/slack/test_card_graph.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import card  # noqa: E402
import card_live  # noqa: E402
import card_types as cc  # noqa: E402
import card_verdicts  # noqa: E402

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
        self.proposed_items = proposed_items if proposed_items is not None else []
        self.propose_calls: list[str] = []
        self.search_calls: list[str] = []
        self.resolve_calls: list[tuple[str, str]] = []
        self.past_verdicts_calls: list[int] = []
        self.records: list[tuple[str, dict]] = []
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

    def proposed(self, since_hours: int) -> list[cc.ProposedVerdict]:
        assert since_hours == card.REVIEW_SINCE_HOURS
        return self.proposed_items


def _blocks_text(blocks) -> str:
    return json.dumps(blocks, ensure_ascii=False)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.sends = []
        self.handovers = []
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
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang=lang,
            repairs=stubs.repairs,
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

    def test_graph_has_the_eight_nodes(self):
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
            },
        )

    def test_the_posted_card_carries_the_configured_note_links(self):
        links = (card.boring_config.ObsidianLink(vault="v", folder="vault/wiki"),)
        with mock.patch.object(card.boring_config, "note_links", return_value=links):
            self.graph.invoke({}, self.cfg)
        self.assertEqual(len(self.sends), 1)
        self.assertIn("obsidian://open?vault=v&file=vault%2Fwiki%2F", _blocks_text(self.sends[0]))

    def test_advise_stops_after_three_successful_candidates(self):
        out = self.graph.invoke({}, self.cfg)
        self.assertNotIn("__interrupt__", out)
        self.assertEqual(len(self.stubs.propose_calls), 3)
        self.assertIn("f64_risk", self.stubs.propose_calls[0])
        self.assertIn("wiki-0536", self.stubs.propose_calls[1])
        self.assertIn("wiki-0576", self.stubs.propose_calls[2])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual([p.note for p in out["proposals"]], EXPECTED_NOTES)
        for note in EXPECTED_NOTES:
            self.assertTrue(note.startswith("/vault/wiki/"))

    def test_a_superseded_hit_reaches_the_posted_card_as_a_label_on_its_own_evidence(self):
        # The marker rides a topic candidate's search hit — a recurrence's refetched
        # own-note hit is built without superseded_by.
        subject = "f64_risk"
        hit = dict(CANDIDATE_HITS[subject][0], superseded_by=["/vault/wiki/wiki-0576.md"])
        with mock.patch.dict(CANDIDATE_HITS, {subject: [hit]}):
            self.graph.invoke({}, self.cfg)
        code_pieces = [
            el["text"]
            for b in self.sends[-1]
            if b["type"] == "rich_text"
            for el in b["elements"][0]["elements"]
            if el.get("style") == {"code": True}
        ]
        marked = [p for p in code_pieces if "대체됨" in p]
        self.assertEqual(marked, ["\nwiki-0900 L2 · 대체됨 → wiki-0576"])
        self.assertEqual(len(code_pieces), 3)

    def test_notworth_candidates_are_skipped_not_counted_as_proposals(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON, NOT_WORTH_JSON] + [ADVICE[s] for s in CANDIDATE_QUEUE[2:]])
        graph = self._build(stubs)
        out = graph.invoke({}, {"configurable": {"thread_id": "test-notworth"}})
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
            out = graph.invoke({}, {"configurable": {"thread_id": "test-cap"}})
        finally:
            card.ADVISE_CALL_CAP = original_cap
        self.assertEqual(len(stubs.propose_calls), 2)
        self.assertEqual(out["proposals"], [])

    def test_zero_proposals_still_sends_a_card(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        out = graph.invoke({}, {"configurable": {"thread_id": "test-zero"}})
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
        out = graph.invoke({}, {"configurable": {"thread_id": "test-drop"}})
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
        out = graph.invoke({}, {"configurable": {"thread_id": "test-all-unresolved"}})
        self.assertEqual(out["proposals"], [])
        self.assertEqual(len(self.sends), 1)

    def test_handover_cites_note_paths(self):
        out = self.graph.invoke({}, self.cfg)
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
        out = graph.invoke({}, {"configurable": {"thread_id": "test-union-diverge"}})
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
        out = graph.invoke({}, {"configurable": {"thread_id": "test-stats"}})
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
            graph.invoke({}, {"configurable": {"thread_id": "test-blind-vault"}})
        err = buf.getvalue()
        self.assertIn("BORING_VAULT_DIR", err)
        self.assertEqual(err.count("BORING_VAULT_DIR"), 1)  # one line, not once per candidate

    def test_confirmation_line_and_event(self):
        out = self.graph.invoke({}, self.cfg)
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
            [("card_confirmation", {"done": 2, "pending": 1, "session": PAST_SESSION, "card_ts": CARD_TS})],
        )

    def test_priority_subject_leads_the_first_advice_call(self):
        self.graph.invoke({}, self.cfg)
        self.assertIn("f64_risk", self.stubs.propose_calls[0])

    def test_door_failure_marks_unknown_instead_of_done(self):
        stubs = Stubs(responses=[NOT_WORTH_JSON] * 8, failures={"f64_risk": "claim-source answered 500"})
        graph = self._build(stubs)
        out = graph.invoke({}, {"configurable": {"thread_id": "test-card-500"}})
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
                    {"done": 0, "pending": 0, "session": PAST_SESSION, "unknown": 3, "card_ts": CARD_TS},
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
            resolve=stubs.resolve,
            approved=dead_door,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({}, {"configurable": {"thread_id": "test-card-dead"}})
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
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({}, {"configurable": {"thread_id": "test-card-nosend"}})
        self.assertEqual(stubs.records, [])  # the card never went out — no confirmation log

    def test_no_past_approvals_means_no_line_no_confirmation_event(self):
        stubs = Stubs(approved=[])
        graph = self._build(stubs)
        out = graph.invoke({}, {"configurable": {"thread_id": "test-card-empty"}})
        self.assertIsNone(out["confirmation"])
        self.assertNotIn("지난 승인", _blocks_text(self.sends[-1]))
        self.assertNotIn("card_confirmation", [r[0] for r in stubs.records])

    def test_card_proposal_and_confirmation_events_fire_for_every_row(self):
        # AC4: 3 proposals ship → 3 card_proposal events plus the existing
        # card_confirmation — 4 record calls, none dropped or duplicated. The press-side
        # card_verdict event is the plugin's fold of card_press.effects, tested in
        # test_card_press (the decision table) and test_boring_card (the fold).
        self.graph.invoke({}, self.cfg)
        names = [name for name, _ in self.stubs.records]
        self.assertEqual(names.count("card_proposal"), 3)
        self.assertEqual(names.count("card_confirmation"), 1)
        self.assertEqual(len(self.stubs.records), 4)
        proposal_fields = [fields for name, fields in self.stubs.records if name == "card_proposal"]
        self.assertEqual([p["idx"] for p in proposal_fields], [0, 1, 2])
        self.assertEqual({p["card_ts"] for p in proposal_fields}, {CARD_TS})

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
        out = graph.invoke({}, {"configurable": {"thread_id": "test-suppress"}})
        # f64_risk's note (wiki-0900) carries the 해 verdict, so advise skips it with no
        # search and no call — the next three candidates fill the card.
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
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=dead_events,
            lang="ko",
        )
        graph = card.build_graph(collabs)
        with self.assertRaises(OSError):
            graph.invoke({}, {"configurable": {"thread_id": "test-card-dead-events"}})
        self.assertEqual(self.sends, [])
        self.assertEqual(stubs.records, [])

    def test_active_projects_are_dialed_and_shared_subjects_are_deduplicated(self):
        # Every fixture project answers identical registers (the _fetch stub ignores its
        # project argument), so the global subject dedup in merge_project_candidates means
        # only the first-called project is credited with any candidates.
        stubs = Stubs(active_project_names=["proj-x", "proj-y"], responses=[NOT_WORTH_JSON] * 8)
        graph = self._build(stubs)
        out = graph.invoke({}, {"configurable": {"thread_id": "test-multi-project"}})
        self.assertEqual(out["projects"], ["proj-x", "proj-y", ""])
        self.assertEqual(set(out["per_project_candidates"]), {"proj-x"})
        self.assertEqual(out["per_project_candidates"]["proj-x"], 5)

    def test_read_repairs_populates_state_once_and_a_dead_door_refuses_the_card(self):
        # AC4: the door's GET is called exactly once, its groups land in state, and a
        # 5xx/unreachable door — same principle as /approved — refuses the card outright.
        stubs = Stubs(repairs_payload={"groups": REPAIR_GROUPS, "total_groups": 5}, merged_yesterday_rows=12)
        graph = self._build(stubs)
        out = graph.invoke({}, {"configurable": {"thread_id": "test-read-repairs"}})
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
            resolve=dead_stubs.resolve,
            approved=dead_stubs.approved,
            record=dead_stubs.record,
            active_projects=dead_stubs.active_projects,
            past_verdicts=dead_stubs.past_verdicts,
            lang="ko",
            repairs=dead_repairs,
            merged_yesterday=dead_stubs.merged_yesterday,
        )
        graph = card.build_graph(collabs)
        sends_before = len(self.sends)
        with self.assertRaises(OSError):
            graph.invoke({}, {"configurable": {"thread_id": "test-read-repairs-dead"}})
        self.assertEqual(len(self.sends), sends_before)


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
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
            repairs=stubs.repairs,
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
        out = self._build(stubs).invoke({}, {"configurable": {"thread_id": "test-rest-skip"}})
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
        out = self._build(stubs).invoke({}, {"configurable": {"thread_id": "test-rest-defer"}})
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
        out = self._build(stubs).invoke({}, {"configurable": {"thread_id": "test-rest-old"}})
        self.assertEqual(len(stubs.propose_calls), 3)
        self.assertIn("alpha_subj", stubs.propose_calls[0])
        self.assertEqual(out["advise_stats"].skipped_resting, 0)
        self.assertEqual(len(out["proposals"]), 3)


# The sufficiency world: one recurrence candidate whose path search would answer unrelated
# notes; the note's own text sits behind read_note, with a frontmatter title the card may
# search on for at most two more hits.
RECUR_PATH = "/vault/wiki/wiki-0697.md"
RECUR_TEXT = (
    "---\nid: wiki-0697\ntitle: relay sync\n---\n"
    "relay sync recurred three times this week and nobody wrote it down\n"
)
RECUR_QUOTE = "relay sync recurred three times this week and nobody wrote it down"
TITLE_HIT = {
    "source_path": "/vault/wiki/wiki-0536.md",
    "snippet": "older relay sync incident",
    "claims": [],
}
PATH_SEARCH_HITS = [
    {"source_path": "/vault/wiki/wiki-0862.md", "snippet": "unrelated", "claims": []},
    {"source_path": "/vault/wiki/wiki-0896.md", "snippet": "unrelated", "claims": []},
]
SUFFICIENCY_FETCH_DATA = {
    "/next_actions": {"answer": "next: none", "sources": []},
    "/risks": {"answer": "risk: none", "sources": []},
    "/stalled": {"answer": "stalled: none", "sources": []},
    "/recurrences": {
        "rows": [
            {
                "newer": {
                    "source_path": "/vault/wiki/wiki-0697.md",
                    "subject": "relay sync",
                    "predicate": "incident",
                    "value": "relay sync recurred",
                },
                "older": [],
            }
        ],
        "days": 30,
        "max_distance": 0.2,
        "min_days_apart": 3,
    },
}


# The topic-sufficiency world: one risks candidate resolving to note-risk, whose search
# answers three other notes. build_advice_prompt keeps hits[:3], so an own note riding
# last would fall off the prompt entirely — the test below pins it first.
TOPIC_SUBJECT = "risk_subj"
TOPIC_RISK_NOTE = "/vault/wiki/note-risk.md"
TOPIC_RISK_QUOTE = "리스크 노트 본문에서 인용할 수 있는 근거 줄입니다"
TOPIC_RISK_TEXT = f"머리말\n{TOPIC_RISK_QUOTE}\n끝"
TOPIC_OTHER_HITS = [
    {"source_path": "/vault/wiki/note-a.md", "snippet": "첫 번째 다른 노트의 스니펫", "claims": []},
    {"source_path": "/vault/wiki/note-b.md", "snippet": "두 번째 다른 노트의 스니펫", "claims": []},
    {"source_path": "/vault/wiki/note-c.md", "snippet": "세 번째 다른 노트의 스니펫", "claims": []},
]
TOPIC_NOTE_TEXTS = {
    TOPIC_RISK_NOTE: TOPIC_RISK_TEXT,
    "/vault/wiki/note-a.md": "머리말\n첫 번째 다른 노트의 본문입니다\n끝",
    "/vault/wiki/note-b.md": "머리말\n두 번째 다른 노트의 본문입니다\n끝",
    "/vault/wiki/note-c.md": "머리말\n세 번째 다른 노트의 본문입니다\n끝",
}
TOPIC_FETCH_DATA = {
    "/next_actions": {"answer": "next: none", "sources": []},
    "/risks": {"answer": "risk: 하나", "sources": [TOPIC_SUBJECT]},
    "/stalled": {"answer": "stalled: none", "sources": []},
    "/recurrences": {"rows": [], "days": 30, "max_distance": 0.2, "min_days_apart": 3},
}


class SufficiencyStubs(Stubs):
    """Records read_note; the path search stands for the live defect (unrelated answers)."""

    def __init__(self, note_texts: dict[str, str], resolutions: dict[str, str] | None = None):
        super().__init__(approved=[], resolutions=resolutions if resolutions is not None else RESOLUTIONS)
        self.note_texts = note_texts
        self.read_note_calls: list[str] = []

    def search(self, subject: str) -> list[dict]:
        self.search_calls.append(subject)
        if subject == "relay sync":
            return [dict(TITLE_HIT)]
        if subject in SKIP_HITS:
            return [dict(h) for h in SKIP_HITS[subject]]
        return [dict(h) for h in PATH_SEARCH_HITS]

    def read_note(self, note: str) -> str | None:
        self.read_note_calls.append(note)
        return self.note_texts.get(note)

    def propose(self, prompt: str) -> str:
        self.propose_calls.append(prompt)
        if "wiki-0697" in prompt:
            return _advice_json(
                "recurrence bottleneck sentence",
                "recurrence today-do sentence",
                RECUR_PATH,
                RECUR_QUOTE,
            )
        return NOT_WORTH_JSON


class SufficiencyTests(unittest.TestCase):
    """The card checks it retrieved the candidate's own note before asking the model. A
    recurrence subject is a note path whose path search answers unrelated notes only —
    the card must skip it, read the note itself, and put its text first in the prompt's
    hits. A topic subject already carrying its own note costs no extra read; an unreadable
    own note and an unresolved subject are recorded reasons, never guesses."""

    def setUp(self):
        self.sends = []

    def _build(self, fetch_data, stubs):
        collabs = card.Collaborators(
            fetch=lambda path, project="": fetch_data[path],
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=lambda blocks: (self.sends.append(blocks), cc.PostedCard(channel=CARD_CH, ts=CARD_TS))[1],
            handover=lambda session, at, paths: {},
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
            repairs=stubs.repairs,
            merged_yesterday=stubs.merged_yesterday,
            proposed=stubs.proposed,
        )
        return card.build_graph(collabs)

    def test_a_recurrence_candidate_is_grounded_on_its_own_note(self):
        # The path search would answer unrelated notes (PATH_SEARCH_HITS). The card skips
        # it, reads the candidate note itself, puts its body first in the prompt, and the
        # frontmatter-title search adds the one related hit beside it — the quote
        # verifies against the note's own text and the proposal ships.
        stubs = SufficiencyStubs(
            {RECUR_PATH: RECUR_TEXT, "/vault/wiki/wiki-0536.md": NOTE_TEXTS["/vault/wiki/wiki-0536.md"]}
        )
        out = self._build(SUFFICIENCY_FETCH_DATA, stubs).invoke(
            {}, {"configurable": {"thread_id": "test-sufficient-recur"}}
        )
        self.assertEqual([p.note for p in out["proposals"]], [RECUR_PATH])
        prompt = stubs.propose_calls[0]
        self.assertIn("relay sync recurred three times this week", prompt)
        self.assertIn("older relay sync incident", prompt)
        note_line = "노트 경로: "
        self.assertLess(prompt.index(note_line + RECUR_PATH), prompt.index("wiki-0536"))
        self.assertEqual(stubs.search_calls, ["relay sync"])  # the path search is never asked
        self.assertEqual(stubs.read_note_calls, [RECUR_PATH, "/vault/wiki/wiki-0536.md"])
        evidence = out["proposals"][0].evidence[0]
        self.assertEqual((evidence.note, evidence.line), (RECUR_PATH, 5))
        stats = out["advise_stats"]
        self.assertEqual(stats.refetched_own_note, 1)
        self.assertEqual(stats.sufficiency_reasons, [])

    def test_a_topic_candidate_carrying_its_own_note_costs_no_refetch(self):
        stubs = SufficiencyStubs(dict(NOTE_TEXTS), resolutions=SKIP_RESOLUTIONS)
        stubs.propose = lambda prompt: (stubs.propose_calls.append(prompt), _approve_everything(prompt))[1]
        out = self._build(SKIP_FETCH_DATA, stubs).invoke(
            {}, {"configurable": {"thread_id": "test-sufficient-topic"}}
        )
        self.assertEqual(len(out["proposals"]), 3)
        stats = out["advise_stats"]
        self.assertEqual(stats.refetched_own_note, 0)
        self.assertEqual(stats.sufficiency_reasons, [])
        self.assertEqual(stubs.search_calls, ["alpha_subj", "beta_subj", "gamma_subj"])
        self.assertEqual(
            sorted(stubs.read_note_calls),
            ["/vault/wiki/note-alpha.md", "/vault/wiki/note-beta.md", "/vault/wiki/note-gamma.md"],
        )

    def test_an_unreadable_own_note_records_the_reason_and_carries_on(self):
        # Nothing is readable, the candidate's own note included: no crash, the reason
        # lands in advise_stats, and the quote cannot verify without the body — the
        # live rejection, as a value.
        stubs = SufficiencyStubs({})
        out = self._build(SUFFICIENCY_FETCH_DATA, stubs).invoke(
            {}, {"configurable": {"thread_id": "test-sufficient-blind"}}
        )
        self.assertEqual(out["proposals"], [])
        stats = out["advise_stats"]
        self.assertEqual(stats.refetched_own_note, 0)
        self.assertEqual(stats.ungrounded, 1)
        self.assertEqual(len(stats.sufficiency_reasons), 1)
        self.assertIn(RECUR_PATH, stats.sufficiency_reasons[0])
        self.assertEqual(stubs.search_calls, [])
        self.assertEqual(stubs.read_note_calls, [RECUR_PATH])  # one try, no retry

    def test_an_unresolved_subject_abstains_with_a_reason_instead_of_guessing(self):
        stubs = SufficiencyStubs(
            {k: NOTE_TEXTS[k] for k in ("/vault/wiki/note-beta.md", "/vault/wiki/note-gamma.md")},
            resolutions={"beta_subj": "/vault/wiki/note-beta.md", "gamma_subj": "/vault/wiki/note-gamma.md"},
        )
        stubs.propose = lambda prompt: (stubs.propose_calls.append(prompt), _approve_everything(prompt))[1]
        out = self._build(SKIP_FETCH_DATA, stubs).invoke(
            {}, {"configurable": {"thread_id": "test-sufficient-unknown"}}
        )
        self.assertEqual(
            [p.note for p in out["proposals"]],
            ["/vault/wiki/note-beta.md", "/vault/wiki/note-gamma.md"],
        )
        stats = out["advise_stats"]
        self.assertEqual(stats.refetched_own_note, 0)
        self.assertEqual(len(stats.sufficiency_reasons), 1)
        self.assertIn("unresolved", stats.sufficiency_reasons[0])
        # alpha's note was read once as an ordinary hit, never refetched.
        self.assertEqual(stubs.read_note_calls.count("/vault/wiki/note-alpha.md"), 1)


class TopicSufficiencyStubs(SufficiencyStubs):
    """The topic world: /search answers three notes, none of them the candidate's own."""

    def search(self, subject: str) -> list[dict]:
        self.search_calls.append(subject)
        return [dict(h) for h in TOPIC_OTHER_HITS]

    def propose(self, prompt: str) -> str:
        self.propose_calls.append(prompt)
        if TOPIC_SUBJECT in prompt:
            return _advice_json(
                "리스크 병목 한 문장 열자 이상입니다",
                "리스크 오늘 할 일 열자 이상입니다",
                TOPIC_RISK_NOTE,
                TOPIC_RISK_QUOTE,
            )
        return NOT_WORTH_JSON


class OwnNoteFirstTests(unittest.TestCase):
    """A topic candidate whose own note is missing from the search hits costs one read_note,
    and the refetched note must ride FIRST in the prompt's hits — build_advice_prompt keeps
    only hits[:3], so an own note appended last would fall off the prompt unseen. The quote
    from it must still verify, or the proposal cannot ship."""

    def setUp(self):
        self.sends = []

    def _build(self, stubs):
        collabs = card.Collaborators(
            fetch=lambda path, project="": TOPIC_FETCH_DATA[path],
            search=stubs.search,
            read_note=stubs.read_note,
            propose=stubs.propose,
            send=lambda blocks: (self.sends.append(blocks), cc.PostedCard(channel=CARD_CH, ts=CARD_TS))[1],
            handover=lambda session, at, paths: {},
            resolve=stubs.resolve,
            approved=stubs.approved,
            record=stubs.record,
            active_projects=stubs.active_projects,
            past_verdicts=stubs.past_verdicts,
            lang="ko",
            repairs=stubs.repairs,
            merged_yesterday=stubs.merged_yesterday,
            proposed=stubs.proposed,
        )
        return card.build_graph(collabs)

    def test_a_topic_candidates_refetched_own_note_rides_first_past_three_other_hits(self):
        stubs = TopicSufficiencyStubs(TOPIC_NOTE_TEXTS, resolutions={TOPIC_SUBJECT: TOPIC_RISK_NOTE})
        out = self._build(stubs).invoke({}, {"configurable": {"thread_id": "test-own-note-first"}})
        self.assertEqual([p.note for p in out["proposals"]], [TOPIC_RISK_NOTE])
        prompt = stubs.propose_calls[0]
        self.assertIn(TOPIC_RISK_QUOTE, prompt)
        own_at = prompt.index(TOPIC_RISK_QUOTE)
        for hit in TOPIC_OTHER_HITS[:2]:  # hits[:3] keeps the own note plus the first two
            self.assertLess(own_at, prompt.index(hit["snippet"]))
        evidence = out["proposals"][0].evidence[0]
        self.assertEqual((evidence.note, evidence.line), (TOPIC_RISK_NOTE, 2))
        stats = out["advise_stats"]
        self.assertEqual(stats.refetched_own_note, 1)
        self.assertEqual(stats.sufficiency_reasons, [])
        self.assertEqual(stubs.search_calls, [TOPIC_SUBJECT])
        self.assertEqual(
            stubs.read_note_calls,
            [TOPIC_RISK_NOTE] + [h["source_path"] for h in TOPIC_OTHER_HITS],
        )


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
    `request("POST", path, {})` (project dropped) makes the whole project axis silently
    degrade to identical unfiltered reads. Patches DrudgeClient.request itself, not
    _live_fetch, so it observes exactly what goes over the wire."""

    def test_project_is_always_sent_in_the_post_body(self):
        from ohmyboring.result import Ok

        calls: list[tuple[str, str, dict]] = []

        def fake_request(self, method, path, payload=None, timeout=None):
            calls.append((method, path, payload))
            return Ok({"rows": []} if path == "/recurrences" else {"answer": "", "sources": []})

        with mock.patch.object(card_live.DrudgeClient, "request", fake_request):
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
    exception: the plugin's fold returns the value to a handler that logs one line, and a
    failed merge must not take the morning card down with it."""

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
    the verdict window (CARD_ANSWERABLE_HOURS)."""

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
    sort, reversing the newest-first order, or removing the cap must each kill this. The
    rows now also carry the reason sentence, fold the same (note, kind) proposed by several
    sessions into one grouped row, and leave a pair the owner held (보류) within the 이레
    window off the card."""

    @staticmethod
    def _events(rows: list[dict], held: list[dict] | None = None, seen: dict | None = None):
        def fake(event_name: str, since_hours: int):
            if seen is not None:
                seen[event_name] = since_hours
            if event_name == "verdict_reviewed":
                return held or []
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

    def test_the_hold_window_read_uses_the_ire_window(self):
        seen: dict = {}
        rows = [
            {
                "attributes": {"session_id": "s-1", "note": "/n1.md", "kind": "used"},
                "observed_at": "2026-09-29T00:00:00+00:00",
            }
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows, seen=seen)):
            card_live._live_proposed(24)
        self.assertEqual(seen, {"verdict_proposed": 24, "verdict_reviewed": 168})

    def test_the_reason_sentence_rides_the_row_and_old_rows_say_none(self):
        rows = [
            {
                "attributes": {
                    "session_id": "s-1",
                    "note": "/vault/wiki/wiki-0700.md",
                    "kind": "contested",
                    "reason": "채점기가 잡은 문장입니다.",
                },
                "observed_at": "2026-09-29T00:00:00+00:00",
            },
            {
                "attributes": {"session_id": "s-2", "note": "/vault/wiki/wiki-0701.md", "kind": "used"},
                "observed_at": "2026-09-28T00:00:00+00:00",
            },
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows)):
            out = card_live._live_proposed(24)
        self.assertEqual(out[0].reason, "채점기가 잡은 문장입니다.")
        self.assertEqual(out[1].reason, "")

    def test_the_same_note_judged_the_same_way_folds_into_one_row(self):
        rows = [
            {
                "attributes": {
                    "session_id": "s-new",
                    "note": "/vault/wiki/wiki-2498.md",
                    "kind": "contested",
                    "reason": "첫째 근거 문장입니다.",
                },
                "observed_at": "2026-09-30T00:00:00+00:00",
            },
            {
                "attributes": {
                    "session_id": "s-old",
                    "note": "/vault/wiki/wiki-2498.md",
                    "kind": "contested",
                },
                "observed_at": "2026-09-29T00:00:00+00:00",
            },
            {
                "attributes": {"session_id": "s-other", "note": "/vault/wiki/wiki-2498.md", "kind": "used"},
                "observed_at": "2026-09-28T00:00:00+00:00",
            },
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows)):
            out = card_live._live_proposed(24)
        # contested가 머리고, 같은 (노트, 판정) 묶음이 한 줄 — 작업만 다륾던 두 줄이 사라졌다
        self.assertEqual([p.session_id for p in out], ["s-new", "s-other"])
        grouped = out[0]
        self.assertEqual(grouped.sessions, ["s-new", "s-old"])
        self.assertEqual(grouped.reason, "첫째 근거 문장입니다.")
        ungrouped = out[1]
        self.assertEqual(ungrouped.sessions, [])
        self.assertEqual(ungrouped.reason, "")

    def test_a_pair_held_within_the_ire_window_stays_off_the_card(self):
        rows = [
            {
                "attributes": {"session_id": "s-1", "note": "/vault/wiki/wiki-2498.md", "kind": "contested"},
                "observed_at": "2026-09-30T00:00:00+00:00",
            },
            {
                "attributes": {"session_id": "s-2", "note": "/vault/wiki/wiki-2498.md", "kind": "used"},
                "observed_at": "2026-09-29T00:00:00+00:00",
            },
        ]
        held = [
            {
                "attributes": {
                    "session_id": "s-1",
                    "note": "/vault/wiki/wiki-2498.md",
                    "proposed_kind": "contested",
                    "card_ts": "1.0",
                    "sessions": ["s-1"],
                    "choice": "defer",
                },
                "observed_at": "2026-09-30T12:00:00+00:00",
            }
        ]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows, held=held)):
            out = card_live._live_proposed(24)
        # 보류한 짝(틀린 노트)만 빠지고, 다른 판정(쓴 노트)은 그대로 오른다
        self.assertEqual([p.kind for p in out], ["used"])

        # 판정을 남긴 줄(agree/flip/delegate)은 억제하지 않는다 — 보류 사걸만 본다
        judged = [{**held[0], "attributes": {**held[0]["attributes"], "choice": "agree"}}]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows, held=judged)):
            out = card_live._live_proposed(24)
        self.assertEqual([p.kind for p in out], ["contested", "used"])

    def test_a_malformed_hold_row_raises_not_skips(self):
        rows = [
            {
                "attributes": {"session_id": "s-1", "note": "/vault/wiki/wiki-2498.md", "kind": "contested"},
                "observed_at": "2026-09-30T00:00:00+00:00",
            }
        ]
        held = [{"attributes": {"choice": "defer", "note": "/vault/wiki/wiki-2498.md"}}]
        with mock.patch.object(card_live, "_live_events", side_effect=self._events(rows, held=held)):
            with self.assertRaises(ValueError):
                card_live._live_proposed(24)

    def test_a_repeated_row_shows_once_with_the_note_title_and_the_session_note(self):
        with tempfile.TemporaryDirectory() as vault:
            wiki = os.path.join(vault, "wiki")
            os.makedirs(wiki)
            with open(os.path.join(wiki, "wiki-2216.md"), "w", encoding="utf-8") as f:
                f.write("---\nid: wiki-2216\ntitle: '폴더 관례'\nproject: ohmyboring\n---\nbody\n")
            with open(os.path.join(wiki, "wiki-2300.md"), "w", encoding="utf-8") as f:
                f.write(
                    "---\nid: wiki-2300\ntitle: 구조 개편 조각 3d\nproject: ohmyboring\n"
                    "date: 2026-09-29\nomb_session_id: s-1\n---\nbody\n"
                )
            row = {
                "attributes": {"session_id": "s-1", "note": "/vault/wiki/wiki-2216.md", "kind": "used"},
                "observed_at": "2026-09-29T23:54:47+00:00",
            }
            with (
                mock.patch.dict(os.environ, {"BORING_VAULT_DIR": vault}),
                mock.patch.object(card_live, "_live_events", side_effect=self._events([row, dict(row)])),
            ):
                out = card_live._live_proposed(24)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].note_title, "폴더 관례")
        self.assertEqual(out[0].work, "ohmyboring · 09-29 · 구조 개편 조각 3d")

    def test_a_non_utf8_note_and_a_second_session_note_do_not_change_the_row(self):
        with tempfile.TemporaryDirectory() as vault:
            wiki = os.path.join(vault, "wiki")
            os.makedirs(wiki)
            with open(os.path.join(wiki, "wiki-0100.md"), "w", encoding="utf-8") as f:
                f.write("---\ntitle: 첫 노트\nproject: p\ndate: 2026-09-01\nomb_session_id: s-1\n---\nb\n")
            with open(os.path.join(wiki, "wiki-0200.md"), "w", encoding="utf-8") as f:
                f.write("---\ntitle: 둘째 노트\nproject: p\ndate: 2026-09-02\nomb_session_id: s-1\n---\nb\n")
            with open(os.path.join(wiki, "wiki-0300.md"), "wb") as f:
                f.write(b"---\ntitle: \xff\xfe broken\n---\n")
            row = {
                "attributes": {"session_id": "s-1", "note": "/vault/wiki/wiki-0300.md", "kind": "used"},
                "observed_at": "2026-09-29T23:54:47+00:00",
            }
            with (
                mock.patch.dict(os.environ, {"BORING_VAULT_DIR": vault}),
                mock.patch.object(card_live, "_live_events", side_effect=self._events([row])),
            ):
                out = card_live._live_proposed(24)
        self.assertEqual(out[0].work, "p · 09-01 · 첫 노트", "the session's first note names it, every run")
        self.assertIn("broken", out[0].note_title)


class MainTests(unittest.TestCase):
    """Slice ③ (wiki-2049): card.py posts and exits — it never opens the socket hermes
    owns. A mutant that re-adds a SocketModeClient construction to main() fails the
    socket assertion here; one that drops the `[card] posted ts=` line fails the stdout
    assertion (doctor a2e and schedule-card.sh parse that line)."""

    def _run_main(self):
        web_module = mock.MagicMock()
        socket_module = mock.MagicMock()
        fake_graph = mock.Mock()
        fake_graph.invoke.return_value = {"message": cc.PostedCard(channel=CARD_CH, ts=CARD_TS)}
        fake_event_log = mock.Mock()
        fake_event_log.recent_events.return_value = []  # no card yet this morning
        buf = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "SLACK_BOT_TOKEN": "tok",
                    "SLACK_CARD_CHANNEL": CARD_CH,
                    "BORING_DOOR_URL": "http://door.invalid",
                    "CARD_DRY_RUN": "",
                },
            ),
            mock.patch.dict(
                sys.modules, {"slack_sdk.web": web_module, "slack_sdk.socket_mode": socket_module}
            ),
            mock.patch.object(card, "build_graph", return_value=fake_graph),
            mock.patch.object(card, "event_log", fake_event_log),
            contextlib.redirect_stdout(buf),
        ):
            rc = card.main()
        return rc, buf.getvalue(), socket_module, fake_graph

    def test_main_posts_prints_the_ts_line_and_never_builds_a_socket_client(self):
        rc, out, socket_module, fake_graph = self._run_main()
        self.assertEqual(rc, 0)
        self.assertIn(f"[card] posted ts={CARD_TS}", out)
        socket_module.SocketModeClient.assert_not_called()
        fake_graph.invoke.assert_called_once()


class PostedTodayGuardTests(unittest.TestCase):
    """The once-per-morning guard (launchd→hermes handover): the second runner of one KST
    day reads the first card's own events (card_proposal/card_confirmation) and exits 0
    with one line — the graph never builds, Slack never opens. A mutant that deletes the
    guard fails test_main_stops_before_the_graph here: build_graph would run."""

    def _env(self):
        return {
            "SLACK_BOT_TOKEN": "tok",
            "SLACK_CARD_CHANNEL": CARD_CH,
            "BORING_DOOR_URL": "http://door.invalid",
            "CARD_DRY_RUN": "",
        }

    def _today_event(self, card_ts="1727480000.000100", name="card_confirmation"):
        now = datetime.now(UTC)
        return {"event": name, "card_ts": card_ts, "ts": now.isoformat()}

    def _run_main(self, events, graph_side_effect="default"):
        fake_event_log = mock.Mock()
        fake_event_log.recent_events.return_value = events
        web_module = mock.MagicMock()
        fake_graph = mock.Mock()
        fake_graph.invoke.return_value = {"message": cc.PostedCard(channel=CARD_CH, ts=CARD_TS)}
        graph_patch = (
            mock.patch.object(card, "build_graph", side_effect=graph_side_effect)
            if graph_side_effect != "default"
            else mock.patch.object(card, "build_graph", return_value=fake_graph)
        )
        buf = io.StringIO()
        with (
            mock.patch.dict(os.environ, self._env()),
            mock.patch.dict(sys.modules, {"slack_sdk.web": web_module}),
            mock.patch.object(card, "event_log", fake_event_log),
            graph_patch,
            contextlib.redirect_stdout(buf),
        ):
            rc = card.main()
        return rc, buf.getvalue(), fake_event_log

    def test_main_stops_before_the_graph_when_todays_card_exists(self):
        rc, out, fake_event_log = self._run_main(
            [self._today_event()], graph_side_effect=AssertionError("the graph must not run")
        )
        self.assertEqual(rc, 0)
        self.assertIn("[card] already posted today (ts=1727480000.000100)", out)
        self.assertEqual(out.strip().count("\n"), 0, "one line, said so")
        self.assertEqual(
            {c.kwargs["event_name"] for c in fake_event_log.recent_events.call_args_list},
            {"card_proposal", "card_confirmation"},
        )

    def test_main_runs_when_yesterdays_card_is_the_newest(self):
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        rc, out, _ = self._run_main([{"event": "card_confirmation", "card_ts": "old", "ts": yesterday}])
        self.assertEqual(rc, 0)
        self.assertIn(f"[card] posted ts={CARD_TS}", out)

    def test_posted_today_picks_the_newest_ts_and_reads_the_kst_calendar(self):
        """15:30 UTC is 00:30 the next day KST: the 15:10 UTC confirmation counts as
        today's card even though its UTC date is still yesterday; at 23:30 KST the same
        events are both already tomorrow's, so the morning still belongs to nobody."""
        now = datetime(2026, 9, 27, 15, 30, tzinfo=UTC)  # 2026-09-28 00:30 KST
        events = [
            {"event": "card_proposal", "card_ts": "older", "ts": "2026-09-27T15:00:00+00:00"},
            {"event": "card_confirmation", "card_ts": "newer", "ts": "2026-09-27T15:10:00+00:00"},
        ]
        fake_event_log = mock.Mock()
        fake_event_log.recent_events.return_value = events
        with mock.patch.object(card, "event_log", fake_event_log):
            self.assertEqual(card._posted_today_ts(now=now), "newer")
            self.assertIsNone(
                card._posted_today_ts(now=datetime(2026, 9, 27, 14, 30, tzinfo=UTC))  # 23:30 KST 09-27
            )

    def test_rows_without_card_ts_or_with_unparseable_ts_are_skipped(self):
        now = datetime.now(UTC)
        fake_event_log = mock.Mock()
        fake_event_log.recent_events.return_value = [
            {"event": "card_confirmation", "ts": now.isoformat()},  # no card_ts
            {"event": "card_confirmation", "card_ts": "x", "ts": "not a timestamp"},
        ]
        with mock.patch.object(card, "event_log", fake_event_log):
            self.assertIsNone(card._posted_today_ts())


if __name__ == "__main__":
    unittest.main()
