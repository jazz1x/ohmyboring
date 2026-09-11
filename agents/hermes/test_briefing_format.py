#!/usr/bin/env python3
"""Network-free tests for Hermes Slack briefing formatting."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_slack_mrkdwn_uses_flat_readable_bullets():
    briefing = load_module("briefing", ROOT / "briefing.py")
    weekly = load_module("weekly_briefing", ROOT / "weekly-briefing.py")
    slack_briefing = load_module("slack_briefing_test", ROOT / "slack_briefing.py")

    answer = """# oh-my-boring

- Done: fixed **DB** primary event logging
1. Next: add ops status JSON
Blocked:
- Blocked: -
## oh-my-boring
- 막힘： LM Studio embedding model is not loaded
"""

    expected = """🚨 막힘 1 · ▶️ 다음 행동 1 · ✅ 완료 1

*행동* (2)
• oh-my-boring
   ◦ 🚨 LM Studio embedding model is not loaded
   ◦ ▶️ add ops status JSON
_→ `recall("…", "oh-my-boring")` · 느림 `next_actions("oh-my-boring")`_"""

    assert briefing.slack_mrkdwn(answer) == expected
    assert weekly.slack_mrkdwn(answer) == expected

    # The shortlist half of this test needs a briefing big enough to warrant one: below the
    # threshold the whole message fits on a screen and a summary would only restate it.
    busy = "# oh-my-boring\n" + "\n".join(
        [f"- Next: action {i}" for i in range(6)] + ["- 막힘： LM Studio embedding model is not loaded"]
    )
    payload = slack_briefing.render_blocks_payload(
        "☀️ 아침 브리핑",
        "2026-07-01 Wed",
        busy,
        ["/vault/wiki/wiki-0001.md"],
        "비어 있음",
    )
    assert payload["text"].startswith("☀️ 아침 브리핑")
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][1]["type"] == "context"
    assert payload["blocks"][2]["type"] == "context"
    assert "🚨 막힘 1" in payload["blocks"][2]["elements"][0]["text"]
    # The shortlist is the third block on purpose: the question at 9am is "what first", and a
    # reader who has to scroll past a status ledger to find it is doing the triage themselves.
    assert payload["blocks"][3]["type"] == "section"
    shortlist = payload["blocks"][3]["text"]["text"]
    assert shortlist.startswith("*오늘의 1순위*")
    assert "LM Studio" in shortlist, "the blocker must be the first thing the reader sees"
    assert payload["blocks"][4]["type"] == "divider"
    # Six status headings were more precision than the classifier behind them delivers, so they
    # collapse into 행동/참고 and the status rides on each item as an emoji instead.
    group = payload["blocks"][5]["text"]["text"]
    assert group.startswith("*행동* ("), group
    assert "🚨" in group, "the status must survive on the item"
    assert "LM Studio" in group
    assert "Blocked: -" not in payload["text"]
    # No filename anywhere: Slack cannot open a vault file, so naming one was a line the reader
    # could do nothing with, and its count never moved off the cap.
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "wiki-0001.md" not in rendered, rendered


def test_the_reference_zone_stands_aside_on_the_mornings_with_the_most_work():
    """The zone that is read and forgotten had twice the room of the zone that is acted on.

    Reference held four labels at five each; the action zone held Blocked plus five and five.
    Across the 63 briefings in the vault the action zone overflowed on 27 of them, hiding 170
    items behind "외 N개 항목" while the reference zone under it printed 351. On those mornings
    reference collapses to its counts and the room goes to the work.
    """
    slack_briefing = load_module("slack_briefing_budget", ROOT / "slack_briefing.py")

    quiet = "# proj\n" + "\n".join(
        [f"- Next: action {i}" for i in range(3)] + [f"- Risks: risk {i}" for i in range(3)]
    )
    body = slack_briefing.render_body_mrkdwn(quiet)
    assert "risk 0" in body, "a quiet morning still shows the reference items themselves"

    busy = "# proj\n" + "\n".join(
        [f"- Next: action {i}" for i in range(14)] + [f"- Risks: risk {i}" for i in range(3)]
    )
    body = slack_briefing.render_body_mrkdwn(busy)
    payload = slack_briefing.render_blocks_payload("T", "S", busy, [], "empty")
    blocks_text = json.dumps(payload, ensure_ascii=False)

    for rendering in (body, blocks_text):
        assert "risk 0" not in rendering, f"reference did not stand aside:\n{rendering}"

    # The count has to be on the zone's own line. Asserting it anywhere in the message passes on
    # the header tally at the top, which says the same words and would keep saying them after the
    # zone line stopped carrying anything at all.
    zone_line = next(ln for ln in body.splitlines() if ln.startswith("*참고*"))
    assert "⚠️ 리스크 3" in zone_line, zone_line
    assert any(
        "*참고*" in json.dumps(b, ensure_ascii=False) and "⚠️ 리스크 3" in json.dumps(b, ensure_ascii=False)
        for b in payload["blocks"]
    ), blocks_text

    # The room actually goes to the work. Index 13 is past what the action zone shows on a quiet
    # morning, so this fails if the busy budget is not larger than the ordinary one.
    assert "action 13" in body, body
    assert ("action 13" in blocks_text) == ("action 13" in body), "the two renderings disagree"


def test_blocked_is_never_truncated_and_both_renderers_agree():
    slack_briefing = load_module("slack_briefing_limits", ROOT / "slack_briefing.py")

    # Eight blockers and thirty next-actions: past the busy budget, which is what the action zone
    # gets once the reference zone stands aside. The cap has to actually fire or this test asserts
    # on nothing — a fixture sized below the budget would pass no matter what the renderer did.
    lines = ["# proj"]
    lines += [f"- Blocked: blocker {i}" for i in range(8)]
    lines += [f"- Next: action {i}" for i in range(30)]
    answer = "\n".join(lines)

    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty")
    blocks_text = "\n".join(
        b["text"]["text"] for b in payload["blocks"] if b.get("type") == "section"
    )

    # A "+N more" hiding a blocker is the one omission that can cost the reader their morning.
    for i in range(8):
        assert f"blocker {i}" in body, f"text renderer dropped blocker {i}"
        assert f"blocker {i}" in blocks_text, f"block renderer dropped blocker {i}"

    # Next is truncated — and both renderers must truncate it to the SAME set, because Slack
    # picks between them and a fallback that disagrees is a second, quieter briefing.
    shown_text = {i for i in range(30) if f"action {i}" in body}
    shown_blocks = {i for i in range(30) if f"action {i}" in blocks_text}
    assert shown_text == shown_blocks, f"fallback and blocks disagree: {shown_text} vs {shown_blocks}"
    assert len(shown_text) < 30, "Next must actually be capped, or this test proves nothing"


def test_done_does_not_push_blockers_off_the_first_screen():
    slack_briefing = load_module("slack_briefing_done", ROOT / "slack_briefing.py")

    answer = "\n".join(
        ["# proj", "- Blocked: the one blocker"]
        + [f"- Done: finished {i}" for i in range(10)]
    )
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty")
    sections = [b for b in payload["blocks"] if b.get("type") == "section"]
    section_text = "\n".join(b["text"]["text"] for b in sections)

    # Done is confirmation, not work: it gets a count line at the bottom, never bullets.
    for i in range(10):
        assert f"finished {i}" not in section_text, "Done items must not occupy sections"
    tail = "\n".join(
        e["text"]
        for b in payload["blocks"]
        if b.get("type") == "context"
        for e in b["elements"]
    )
    assert "완료 10" in tail
    assert "the one blocker" in sections[0]["text"]["text"], "the blocker leads the message"


def test_long_text_is_cut_at_a_boundary_and_says_so():
    slack_briefing = load_module("slack_briefing_trim", ROOT / "slack_briefing.py")

    # A bare slice cuts mid-sentence with no sign it happened, which is how a reader ends up
    # trusting a sentence that was never finished.
    text = "\n".join(f"line {i} with some words" for i in range(400))
    out = slack_briefing._mrkdwn_text(text, 300)
    assert len(out) <= 300
    assert out.endswith("…"), "a truncated field must admit it"
    assert not out.rstrip("…").endswith("wor"), "cut should land on a line boundary"

    short = slack_briefing._mrkdwn_text("intact", 300)
    assert short == "intact", "text that fits must not be touched"


def _week(days_spec):
    """[(date, brief_markdown)] -> [(date, BriefDocument)] using the real parser."""
    slack_briefing = load_module("slack_briefing_week", ROOT / "slack_briefing.py")
    return [(d, slack_briefing.parse_brief(text)) for d, text in days_spec]


def test_weekly_reports_persistence_not_closure():
    trend = load_module("weekly_trend_persist", ROOT / "weekly_trend.py")

    # The same project blocked all week, worded differently every day — which is what actually
    # happens, because each daily is re-synthesised by the model (measured: 252 items over a
    # week, adjacent-day overlap 0,0,0,2,0,0).
    days = _week(
        [
            (f"2026-08-2{i}", f"# kb-rag-bot\n- Blocked: corpus boundary issue variant {i}\n")
            for i in range(4)
        ]
        # Blocked on exactly one day: a thing can be blocked overnight and cleared by lunch,
        # and calling that persistent would fill the intervention list with noise. It must be a
        # *blocked* project — a Done one is filtered by label anyway and would prove nothing.
        + [("2026-08-24", "# overnight\n- Blocked: cleared by lunch\n")]
    )
    projects = trend.collect_week(days)
    rows = trend.needs_intervention(projects)
    assert [(w.name, label, n) for w, label, n in rows] == [("kb-rag-bot", "Blocked", 4)], rows

    # A project seen once is not persistence.
    assert all(w.name != "overnight" for w, _l, _n in rows), "one day is not persistence"


def test_weekly_never_sums_label_counts_across_days():
    trend = load_module("weekly_trend_sum", ROOT / "weekly_trend.py")

    days = _week(
        [
            ("2026-08-20", "# p\n- Done: a\n- Done: b\n"),
            ("2026-08-21", "# p\n- Done: c\n- Done: d\n"),
            ("2026-08-22", "# p\n- Done: e\n"),
        ]
    )
    got = trend.label_trend(days)
    # Endpoints only. A sum would say "5 done this week" when each day is an independent
    # re-observation of the same corpus and no key can tell a repeat from a new item.
    assert got == {"Done": (2, 1)}, got


def test_weekly_quotes_the_latest_daily_rather_than_resummarising():
    trend = load_module("weekly_trend_quote", ROOT / "weekly_trend.py")

    days = _week(
        [
            ("2026-08-20", "# p\n- Blocked: the old wording\n"),
            ("2026-08-21", "# p\n- Blocked: the old wording again\n"),
            ("2026-08-22", "# p\n- Blocked: today's exact wording\n"),
        ]
    )
    projects = trend.collect_week(days)
    assert projects["p"].latest_line == "today's exact wording"


def test_the_weekly_text_carries_what_the_blocks_carry():
    """The persistence weekly could only be emitted as JSON, so it never shipped.

    Cron delivery is text-only, and the trend path sat behind `BORING_BRIEFING_FORMAT=blocks`
    which production does not set — so every Monday sent a re-synthesised `/weekly` instead,
    the exact thing this weekly exists to replace.
    """
    slack_briefing = load_module("slack_briefing_wtext", ROOT / "slack_briefing.py")
    weekly_trend = load_module("weekly_trend_wtext", ROOT / "weekly_trend.py")

    week = weekly_trend.ProjectWeek(name="kb-rag-bot")
    week.days = {"2026-08-25", "2026-08-26", "2026-08-27"}
    week.latest_line = "컨플루언스 상태 확인 불가"
    intervention = [(week, "Stalled", 3, 7)]
    board = [(week, 7)]
    trend = {"Done": (8, 5)}

    text = slack_briefing.render_weekly_mrkdwn("주간", "W36", intervention, board, trend, [])

    assert "kb-rag-bot — 3/7일" in text, text
    assert "컨플루언스 상태 확인 불가" in text, "the quoted line is the weekly's only item detail"
    # The scoreboard writes "name N/M일"; the intervention line writes "name — N/M일". Asserting
    # the bare count matches either, so removing the scoreboard entirely still passed.
    assert "주간 점유" in text and "kb-rag-bot 3/7일" in text, text
    assert "8→5" in text, text
    # Dropping the caveat lets a reader add ✅ 10→10 into "twenty done"; every day is an
    # independent re-synthesis, so the two numbers are observations, not a rate.
    assert "일자별 관측치이며 마감률이 아니다" in text, text


def test_weekly_blocks_carry_the_not_a_closure_rate_caveat():
    slack_briefing = load_module("slack_briefing_caveat", ROOT / "slack_briefing.py")
    trend = load_module("weekly_trend_caveat", ROOT / "weekly_trend.py")

    days = _week([("2026-08-2%d" % i, "# p\n- Blocked: x%d\n" % i) for i in range(3)])
    projects = trend.collect_week(days)
    blocks = slack_briefing.render_weekly_blocks(
        "T", "S", projects,
        [(w, l, n, 3) for w, l, n in trend.needs_intervention(projects)],
        [(w, 3) for w in trend.scoreboard(projects)],
        trend.label_trend(days), [],
    )
    tail = "\n".join(
        e["text"] for b in blocks if b.get("type") == "context" for e in b["elements"]
    )
    # A reader who adds "✅ 10→10" into "twenty done" has been lied to. The caveat is contract.
    assert "마감률이 아니다" in tail
    # No item bullets beyond the one quoted line per persistent project.
    sections = [b for b in blocks if b.get("type") == "section" and "text" in b]
    assert len(sections) == 1, "the weekly must not re-list items"


def test_slack_mrkdwn_handles_adversarial_inputs():
    slack_briefing = load_module("slack_briefing_test2", ROOT / "slack_briefing.py")

    # Empty answer falls back to the empty message.
    assert slack_briefing.render_body_mrkdwn("") == ""

    # Project heading with no items falls back to compact text (render_message_mrkdwn
    # will substitute the empty message when the body is empty).
    assert slack_briefing.render_body_mrkdwn("# empty-project\n") == "# empty-project"

    # Label without a value is skipped, and a label heading applies to multiple bullets.
    multi = """# p

Blocked:
- first blocker
- second blocker
- 없음
"""
    body = slack_briefing.render_body_mrkdwn(multi)
    assert "*행동*" in body, "blocked items live in the action zone now"
    assert body.count("🚨") >= 2, "each blocked item keeps its status emoji"
    assert body.count("first blocker") == 1
    assert body.count("second blocker") == 1
    assert "없음" not in body  # EMPTY_VALUES should be dropped
    assert "기타" not in body  # both bullets inherited the Blocked label

    # An item the distiller failed to label is still an item. There is no "기타" heading any
    # more — it rides in 참고 — but it must never vanish, or the briefing is quietly lossy.
    misc = """# p

- UnknownLabel: something odd
- plain bullet without a label
"""
    body = slack_briefing.render_body_mrkdwn(misc)
    assert "*참고*" in body
    assert "something odd" in body, "an unknown label must not silently drop the item"
    assert "plain bullet without a label" in body
    assert "something odd" in body
    assert "plain bullet without a label" in body

    # HTML-like characters are preserved in mrkdwn and escaped in Block Kit.
    html = """# p

- Next: fix <body> & "quotes"
"""
    body = slack_briefing.render_body_mrkdwn(html)
    assert "fix <body> & \"quotes\"" in body
    payload = slack_briefing.render_blocks_payload(
        "t", "s", html, [], "empty"
    )
    # Fallback mrkdwn keeps raw characters; Block Kit blocks escape them.
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)
    assert "&lt;body&gt;" in block_blob
    assert "&amp;" in block_blob


def test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections():
    slack_briefing = load_module("slack_briefing_test3", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: README 최신화
- Next: 컨플루언스 문서 업데이트
## qa-tests
- Done: PoC 일정 전환
## kb-rag-bot
- Done: README 최신화
- Blocked: 토큰 문제
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # Done is a count line now, so its items never reach the body at all — the dedup that
    # matters here is on the labels that do print.
    assert body.count("README 최신화") == 0
    # Blocked from the second kb-rag-bot section is preserved.
    assert body.count("토큰 문제") == 1
    # Summary counts reflect dedup.
    assert "✅ 완료 2" in body  # README 최신화 + PoC 일정 전환
    assert "🚨 막힘 1" in body


def test_slack_mrkdwn_filters_placeholders_and_noise():
    slack_briefing = load_module("slack_briefing_test4", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: 게이트 4단계 구현
- Next: 다음 지시 기다림
- Blocked: -
- Risks: 없음
- Decisions: 출처 강등 처리
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # Vacuous bullets dropped.
    assert "다음 지시 기다림" not in body
    assert "Blocked: -" not in body
    assert "없음" not in body
    # Real bullets preserved.
    # A Done item survives parsing but not the body: it is counted, not bulleted.
    assert "게이트 4단계 구현" not in body
    assert "✅ 완료" in body
    assert "출처 강등 처리" in body


def test_done_is_counted_once_and_never_bulleted():
    """Done is a number, and it is that number in exactly one place.

    It used to be capped at three bullets here and demoted to a count in the blocks -- the two
    renderings disagreeing is the defect this contract exists to prevent. Then both renderers
    printed the count twice: once in the header line and again at the foot as "✅ 완료 N건 — 상세는
    위키". Measured on the 8 briefings actually sent, the two numbers were identical in all 7 that
    had any Done items, and on 09-02 the repeat was the whole body.
    """
    slack_briefing = load_module("slack_briefing_test5", ROOT / "slack_briefing.py")

    answer = "## kb-rag-bot\n" + "\n".join(f"- Done: task {i}" for i in range(10))
    body = slack_briefing.render_body_mrkdwn(answer)
    assert body.count("task") == 0
    assert "✅ 완료 10" in body
    assert body.count("✅ 완료") == 1, f"the count is stated twice:\n{body}"
    assert "상세는 위키" not in body


def test_singular_labels_are_not_dumped_into_the_unlabelled_bucket():
    slack_briefing = load_module("slack_briefing_alias", ROOT / "slack_briefing.py")

    # The engine writes these in the singular. The alias table only had the plurals, so eight
    # labelled items a day were reported as "기타" — the category was not missing, the alias was.
    answer = "# p\n- Decision: B 단독\n- Risk: 역방향 감사 누락\n- Blocker: 외부 차단\n"
    body = slack_briefing.render_body_mrkdwn(answer)
    assert "💡 결정 1" in body, body
    assert "⚠️ 리스크 1" in body, body
    assert "🚨 막힘 1" in body, body
    assert "기타" not in body, "a labelled item must never land in the unlabelled bucket"


def test_the_engines_default_claim_kind_is_a_category_not_a_leftover():
    slack_briefing = load_module("slack_briefing_fact", ROOT / "slack_briefing.py")

    # `fact` is what a claim is unless it says otherwise, so it is the biggest thing the
    # distiller emits -- and it was landing in the unlabelled bucket, the same defect the
    # singular aliases above fixed, on a much larger share of the day's items.
    answer = "# p\n- Fact: 재배포 없이는 배달되지 않는다\n- Decision: B 단독\n"
    body = slack_briefing.render_body_mrkdwn(answer)
    assert "📌 사실 1" in body, body
    assert "기타" not in body, "the engine's default claim kind is not an unlabelled leftover"
    # It belongs with what the reader consults, not with what the reader must act on. Assert
    # positively: "absent from 행동" also holds when the item was dropped altogether, which is
    # the louder bug of the two.
    action_zone, _, reference_zone = body.partition("*참고*")
    assert "재배포 없이는" in reference_zone, body
    assert "재배포 없이는" not in action_zone, body


def test_the_window_sample_is_named_in_both_renderings_and_falls_silent_when_met():
    slack_briefing = load_module("slack_briefing_window", ROOT / "slack_briefing.py")
    V = slack_briefing.verdict_core

    # The verdict's counts only move when a session *ends*, and sessions here run for days. The
    # ledger can look busy for a week while the floor sits still, and without this line nobody
    # finds out until the window closes on a refusal.
    answer = "# p\n- Next: 뭔가 한다\n"
    behind = {"sessions": 3, "total_prompts": 120}

    body = slack_briefing.render_message_mrkdwn("*T*", "S", answer, [], "empty", None, behind)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty", None, behind)

    def all_text(blocks):
        out = []
        for b in blocks:
            if isinstance(b.get("text"), dict):
                out.append(b["text"].get("text", ""))
            for el in b.get("elements") or []:
                if isinstance(el, dict) and el.get("text"):
                    out.append(el["text"])
        return "\n".join(out)

    for where, text in (("fallback", body), ("blocks", all_text(payload["blocks"]))):
        assert f"3/{V.MIN_SESSIONS}" in text, (where, text)
        assert f"120/{V.MIN_INJECTED_PROMPTS}" in text, (where, text)

    met = {"sessions": V.MIN_SESSIONS, "total_prompts": V.MIN_INJECTED_PROMPTS}
    assert slack_briefing.window_notice(met) == "", "a met floor stops reporting the sample"

    # One floor met is not both — the line has to keep reporting until the verdict is computable.
    assert slack_briefing.window_notice(
        {"sessions": V.MIN_SESSIONS, "total_prompts": 1}
    ) != ""

    assert slack_briefing.window_notice(None) == "", "an unreachable engine is not a zero sample"


def test_a_heading_becomes_a_project_label_only_if_it_looks_like_one():
    """The parser took whatever the model wrote as the project name beside every item.

    Measured across 62 briefings: 109 of 165 distinct headings were not projects — outline
    numbers, bolded prose, section titles from some other document. A blind reader given eight of
    these found the label disagreed with the item's own text in 14 of 24 top picks, and read the
    grouping as misinformation rather than as context.

    The rejected half matters as much as the accepted half: `1.95.3-stage-검증` is a real project
    whose name opens with a version number, and an earlier version of the pattern cut it.
    """
    slack_briefing = load_module("slack_briefing_heading", ROOT / "slack_briefing.py")

    for real in (
        "foodspring-front",
        "kb-rag-bot",
        "omm-consumer-rollout-plan",
        "boro-janus-worker",
        "1.95.3-stage-검증",
    ):
        assert slack_briefing._is_project_heading(real), f"real project rejected: {real}"

    for prose in (
        "1. **문제 개요**",
        "5. **남은 일**",
        "2. **해결 방안**",
        "Risk",
        "Stalled",
        "bi-slack-analytics-bot (S11 요구사항)",
        "필터링 및 자동화 관련 (공통/기타)",
        "",
    ):
        assert not slack_briefing._is_project_heading(prose), f"not a project: {prose!r}"


def test_nothing_in_the_message_names_the_unattributed_group_as_a_project():
    """`Brief` is a bucket, not a name, and it was being printed everywhere a name goes.

    It led the shortlist (`1. ⏸️ Brief — …`), it headed a bullet group beside real project names,
    and it was pasted into the follow-up command as `recall("…", "Brief")` -- sending the reader
    after a project that does not exist. Nearly half of all items land in this bucket (944 of 2016
    across the 62 briefings in the vault), so none of these is an edge case.
    """
    slack_briefing = load_module("slack_briefing_orphan_render", ROOT / "slack_briefing.py")
    answer = "\n".join(
        f"## 다른 프로젝트\n- Blocked: 막힌 항목 {n}" for n in range(1, 8)
    )
    rendered = slack_briefing.render_message_mrkdwn(
        "*brief*", "2026-09-03", answer, [], "없음", None, None, ["foodspring-front"]
    )

    assert "막힌 항목 1" in rendered, "the items themselves must survive"
    assert slack_briefing.UNATTRIBUTED not in rendered, rendered
    assert 'recall("…", "Brief")' not in rendered
    assert "Brief —" not in rendered


def test_the_action_zone_and_the_shortlist_agree_on_what_comes_first():
    """They were two copies of one order, and only one of them got fixed.

    `ZONES` carried its own `("Blocked", "Stalled", "Next")`, so reordering `ACTIONABLE` moved the
    shortlist while the action zone underneath it still opened with items that had not moved in a
    week -- the same briefing telling the reader two different things about what matters.
    """
    slack_briefing = load_module("slack_briefing_zones", ROOT / "slack_briefing.py")
    action_zone = dict(slack_briefing.ZONES)["행동"]
    assert tuple(action_zone) == tuple(slack_briefing.ACTIONABLE), (
        f"the action zone has its own order again: {action_zone}"
    )


def test_the_shortlist_meets_today_before_it_meets_last_week():
    """Stalled led the shortlist and kept taking slots from items that were actually for today.

    Measured across the 62 briefings in the vault: Stalled held 34 of 133 shortlist slots (26%),
    the same few items recurring. Ordered after Next it holds 7 (5%) and Next gains 27, which
    changes the opening line of 18 of those briefings.
    """
    slack_briefing = load_module("slack_briefing_order", ROOT / "slack_briefing.py")
    assert slack_briefing.ACTIONABLE.index("Next") < slack_briefing.ACTIONABLE.index("Stalled")
    assert slack_briefing.ACTIONABLE.index("Blocked") == 0, "a blocker still opens the message"

    items = {
        "Blocked": [("proj-a", slack_briefing.BriefItem("Blocked", "막힘"))],
        "Stalled": [("proj-b", slack_briefing.BriefItem("Stalled", "일주일째 그대로"))],
        "Next": [("proj-c", slack_briefing.BriefItem("Next", "오늘 할 일"))],
    }
    picks = slack_briefing.top_picks(items, limit=2)
    assert [label for label, _p, _i in picks] == ["Blocked", "Next"], picks

    # Stalled still reaches the shortlist on a day with room for it.
    picks = slack_briefing.top_picks(items, limit=3)
    assert [label for label, _p, _i in picks] == ["Blocked", "Next", "Stalled"], picks


def test_the_rendered_briefing_actually_uses_the_project_list():
    """The unit tests all call the validator directly, and that is not where the briefing runs.

    `briefing.py` hands the list to `render_message_mrkdwn`, which passes it to
    `render_body_mrkdwn`, which passes it to `parse_brief`. Drop the argument at any one of those
    hops and every heading-level test still passes while the cron -- which delivers through the
    mrkdwn path -- silently stops checking membership. This walks the whole chain.
    """
    slack_briefing = load_module("slack_briefing_wired", ROOT / "slack_briefing.py")
    answer = (
        "## foodspring-front\n- Next: ship the thing\n"
        "## 다른 프로젝트\n- Next: something under a section title\n"
    )

    known = slack_briefing.render_message_mrkdwn(
        "*brief*", "2026-09-03", answer, [], "없음", None, None, ["foodspring-front"]
    )
    assert "foodspring-front" in known
    assert "다른 프로젝트" not in known, (
        "the membership list did not reach the renderer the cron delivers through"
    )

    unavailable = slack_briefing.render_message_mrkdwn(
        "*brief*", "2026-09-03", answer, [], "없음", None, None, None
    )
    assert "다른 프로젝트" in unavailable, (
        "with no list the shape test admits it, so its absence above proves the list did the work"
    )


def test_each_shape_rule_rejects_something_no_other_rule_catches():
    """Five rules, and until now two of them carried every rejection between them.

    Every prose example in these tests looked like `1. **제목**`, which trips the outline rule and
    the bold rule at once -- so the sentence, italic and length rules were never the reason a
    heading was cut, and deleting any of them changed nothing. Each case below is written to be
    caught by exactly one rule.
    """
    slack_briefing = load_module("slack_briefing_rules", ROOT / "slack_briefing.py")

    assert not slack_briefing._is_project_heading("3. 남은 일")  # outline number alone
    assert not slack_briefing._is_project_heading("**남은 일**")  # bold alone
    assert not slack_briefing._is_project_heading("배포를 먼저 한다.")  # sentence punctuation
    assert not slack_briefing._is_project_heading("_남은 일_")  # italic opening
    assert not slack_briefing._is_project_heading("bi-slack-analytics-bot (S11)")  # parenthetical
    assert not slack_briefing._is_project_heading(
        "배포 순서를 정리하고 남은 일을 나누어 담당자에게 넘긴다"
    )  # length alone: 29 chars, no punctuation, no markup

    # The version marker is the case the length and outline rules must both let through.
    assert slack_briefing._is_project_heading("1.95.3-stage-검증")


def test_a_project_the_corpus_knows_is_not_cut_for_being_long():
    """The length cap is a guess, and membership is not -- so the guess must yield to it.

    Three of the 126 names the corpus holds are longer than the cap
    (`feature-FDS-16742-softnav-remainder` and two siblings). Applying the cap on top of a real
    membership list threw those away and no test noticed, because the cap was only ever exercised
    against strings that were prose anyway.
    """
    slack_briefing = load_module("slack_briefing_long", ROOT / "slack_briefing.py")
    long_name = "feature-FDS-16742-softnav-remainder"
    assert len(long_name) > slack_briefing._PROJECT_HEADING_MAX

    assert slack_briefing._is_project_heading(long_name, [long_name])
    assert not slack_briefing._is_project_heading(long_name, None), (
        "with no list to check, length is the only signal left and it still applies"
    )


def test_the_project_list_fetch_reports_absence_as_absence():
    """`None` and `[]` are different answers and the fetch must not collapse them.

    Everything downstream depends on it: `[]` says the corpus holds no projects, which strips the
    label off every item in the briefing; `None` says the engine could not be asked, which falls
    back to the shape test. A `except: return []` here would render a total outage as a clean
    briefing with no project on anything.
    """
    briefing = load_module("briefing_projects", ROOT / "briefing.py")

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    original = briefing.urllib.request.urlopen
    try:
        briefing.urllib.request.urlopen = lambda *a, **k: _Resp(
            '{"projects": ["foodspring-front", "scenario-compiler"]}'
        )
        assert briefing.known_projects() == ["foodspring-front", "scenario-compiler"]

        briefing.urllib.request.urlopen = lambda *a, **k: _Resp('{"projects": []}')
        assert briefing.known_projects() == [], "an empty corpus is a real answer, not an absence"

        def _down(*_a, **_k):
            raise OSError("connection refused")

        briefing.urllib.request.urlopen = _down
        assert briefing.known_projects() is None

        briefing.urllib.request.urlopen = lambda *a, **k: _Resp("not json at all")
        assert briefing.known_projects() is None

        briefing.urllib.request.urlopen = lambda *a, **k: _Resp('{"projects": "all of them"}')
        assert briefing.known_projects() is None, "a malformed payload is not a project list"
    finally:
        briefing.urllib.request.urlopen = original


def test_orphans_from_separate_stretches_share_one_unattributed_group():
    """Two groups with the same name are two things to the reader, and they are not.

    Non-project headings do not arrive in one run: the model writes a section title, then a real
    project, then another section title. Appending a fresh group each time renders "Brief" twice
    in the same briefing with different items under each.
    """
    slack_briefing = load_module("slack_briefing_orphan_merge", ROOT / "slack_briefing.py")
    doc = slack_briefing.parse_brief(
        "## Risk\n- Next: first orphan\n"
        "## foodspring-front\n- Next: a real one\n"
        "## 3. **후속**\n- Next: second orphan\n"
    )
    names = [p.name for p in doc.projects]
    assert names.count(slack_briefing.UNATTRIBUTED) == 1, names
    orphans = next(p for p in doc.projects if p.name == slack_briefing.UNATTRIBUTED)
    assert [i.text for i in orphans.items] == ["first orphan", "second orphan"]


def test_a_name_shaped_heading_the_corpus_never_heard_of_is_not_a_project():
    """The shape test is sound but not complete, and this closes the other half.

    Of the 121 headings the shape test passed across 62 briefings, only 55 named a project the
    corpus knows. `다른 프로젝트` is the rest of them: short, no prose, no outline number -- nothing
    in the string says it is a section title rather than a name. Only membership does.
    """
    slack_briefing = load_module("slack_briefing_known", ROOT / "slack_briefing.py")
    corpus = ["foodspring-front", "scenario-compiler", "1.95.3-stage-검증"]

    assert slack_briefing._is_project_heading("foodspring-front", corpus)
    assert slack_briefing._is_project_heading("1.95.3-stage-검증", corpus)
    assert not slack_briefing._is_project_heading("다른 프로젝트", corpus)
    assert not slack_briefing._is_project_heading("scenario-compiler-v2", corpus)

    # Membership decides, but it does not resurrect what the shape test already rejected: a corpus
    # that somehow holds an outline number as a project name still must not label items with it.
    assert not slack_briefing._is_project_heading("1. **문제 개요**", ["1. **문제 개요**"])


def test_an_unreachable_engine_does_not_read_as_a_corpus_with_no_projects():
    """The failure that this guards is worse than the defect it fixes.

    `known_projects()` returns None when the engine cannot be asked, and None is not []. If an
    empty list reached the validator as "no projects exist", every heading in the briefing would
    fail membership and every item would lose its label -- a total outage rendered as a tidy,
    plausible briefing. Absence of an answer falls back to the shape test instead.
    """
    slack_briefing = load_module("slack_briefing_absent", ROOT / "slack_briefing.py")

    assert slack_briefing._is_project_heading("foodspring-front", None), (
        "an unavailable project list must not strip the label off a real project"
    )
    assert not slack_briefing._is_project_heading("1. **문제 개요**", None), (
        "falling back must fall back to the shape test, not to accepting everything"
    )
    # An empty list is the corpus answering, not the engine failing to. Nothing is lookupable, so
    # nothing is labelled -- and the two cases stay distinguishable all the way down, rather than
    # `[]` quietly behaving like None because an empty container is falsy.
    assert not slack_briefing._is_project_heading("foodspring-front", []), (
        "an empty corpus is an answer: no heading names a project the reader could look up"
    )

    doc = slack_briefing.parse_brief(
        "## foodspring-front\n- Next: ship the thing\n", known_projects=None
    )
    assert [p.name for p in doc.projects] == ["foodspring-front"]


def test_items_under_a_non_project_heading_lose_the_label_rather_than_borrow_one():
    """Two wrong answers were available and this picks neither.

    Keeping the previous project attributes the items to whatever came before — wrong and silent.
    Opening a group under the prose makes the model's sentence the project name — wrong and loud.
    They go unattributed instead, so the reader is never handed a project they can look up when
    there is none.
    """
    slack_briefing = load_module("slack_briefing_orphan", ROOT / "slack_briefing.py")
    doc = slack_briefing.parse_brief(
        "## foodspring-front\n"
        "- Next: ship the thing\n"
        "## 1. **문제 개요**\n"
        "- Next: something the model wrote under prose\n"
    )
    grouped = {p.name: [i.text for i in p.items] for p in doc.projects}
    assert "1. **문제 개요**" not in grouped, grouped
    assert grouped["foodspring-front"] == ["ship the thing"], (
        "the orphan item must not fall back onto the project above it"
    )
    assert "something the model wrote under prose" in grouped["Brief"], grouped


def test_the_briefing_counts_the_verdicts_population_not_a_neighbouring_one():
    """`uptake_stats` had no test at all, which is why it summed adapters and skipped the split.

    Measured against the live store on 2026-09-02: pooled across the whole event log it said 14
    sessions where the verdict sees 3. With the midpoint gate riding this number, that pooled 14
    would have cleared the floor of 10 and the 09-08 briefing would have said nothing while the
    gate said "short" — a warning wired to the wrong number is not wired.
    """
    import importlib.util
    import sys as _sys
    from unittest import mock

    spec = importlib.util.spec_from_file_location(
        "briefing_under_test", Path(__file__).resolve().parent / "briefing.py"
    )
    briefing = importlib.util.module_from_spec(spec)
    _sys.modules["briefing_under_test"] = briefing
    spec.loader.exec_module(briefing)

    def row(session, agent, when, total):
        return {
            "event": "injection_uptake",
            "observed_at": when,
            "session_id": session,
            "attributes": {
                "agent": agent,
                "used_prompts": 0,
                "total_prompts": total,
                "used_control_prompts": 0,
            },
        }

    before, after = "2026-09-11T00:00:00+00:00", "2026-09-13T00:00:00+00:00"
    rows = [row(f"old{i}", "claude-code", before, 10) for i in range(6)]
    rows += [row(f"new{i}", "claude-code", after, 10) for i in range(3)]
    rows += [row(f"k{i}", "kimi", after, 10) for i in range(4)]

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"entries": rows}).encode()

    with mock.patch.object(briefing.urllib.request, "urlopen", lambda *a, **k: _Resp()):
        stats = briefing.uptake_stats()

    assert stats["sessions"] == 3, (
        f"pre-repair rows and other adapters must not inflate it: {stats}"
    )


def test_the_midpoint_warning_rides_the_one_channel_that_reaches_a_person():
    """PRD §2's midpoint gate, on the briefing rather than only in doctor.

    doctor carries the same check and doctor is in no crontab (measured 2026-09-02), so a gate
    that fires once, on a date days out, under a command nobody runs is a dead letter. The morning
    briefing is the only path that reaches a person every day.

    Silent outside 09-08 → the window close: the gate is one-time by contract, and a warning that
    repeats after it has been answered teaches the reader to skip the line it rides on.
    """
    import os

    slack_briefing = load_module("slack_briefing_midpoint", ROOT / "slack_briefing.py")
    V = slack_briefing.verdict_core
    short = {"sessions": 3, "total_prompts": 31}
    floor = V.MIDPOINT_MIN_SCORED

    def on(day):
        os.environ["BORING_TODAY"] = day
        try:
            return slack_briefing.window_notice(short)
        finally:
            os.environ.pop("BORING_TODAY", None)

    assert "중간점" not in on("2026-09-13"), "before the midpoint it is not due"
    assert "중간점" not in on("2026-09-27"), "after the close it is spent"

    due = on(V.MIDPOINT)
    assert "중간점" in due and f"3/{floor}" in due, due
    assert f"3/{V.MIN_SESSIONS}" in due, "the sample line must survive alongside it"

    os.environ["BORING_TODAY"] = V.MIDPOINT
    try:
        assert "중간점" not in slack_briefing.window_notice(
            {"sessions": floor, "total_prompts": 31}
        ), "at the floor there is no shortfall to report"
    finally:
        os.environ.pop("BORING_TODAY", None)


def test_the_audit_backlog_is_named_in_both_renderings_and_falls_silent_when_met():
    import os

    slack_briefing = load_module("slack_briefing_audit", ROOT / "slack_briefing.py")

    # The LLM judge accrues 24 a night on its own; the human side only moves when a person
    # sits down, and until it does the agreement figure -- the one the verdict contract
    # actually reads -- cannot be computed at all. Silence about that is how a two-week
    # window ends in "판단 보류".
    answer = "# p\n- Next: 뭔가 한다\n"
    behind = {"judges": [{"judge": "llm", "relevant": 18, "irrelevant": 30}], "compared": 4}

    os.environ["BORING_TODAY"] = "2026-09-14"  # a Monday inside the window
    body = slack_briefing.render_message_mrkdwn("*T*", "S", answer, [], "empty", behind)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty", behind)
    def all_text(blocks):
        # context blocks carry their text in `elements`, sections in `text` -- read both, or
        # the assertion passes for the wrong reason on whichever shape it forgot.
        out = []
        for b in blocks:
            if isinstance(b.get("text"), dict):
                out.append(b["text"].get("text", ""))
            for el in b.get("elements") or []:
                if isinstance(el, dict) and el.get("text"):
                    out.append(el["text"])
        return "\n".join(out)

    blocks_text = all_text(payload["blocks"])
    owed = str(slack_briefing.label_core.MIN_COMPARED - 4)
    # Slack picks between the two renderings; a line in only one of them is a second, quieter
    # briefing -- the defect this whole contract exists to prevent.
    assert owed in body and "--audit" in body, body
    assert owed in blocks_text and "--audit" in blocks_text, blocks_text

    met = {"judges": [], "compared": slack_briefing.label_core.MIN_COMPARED}
    assert slack_briefing.audit_notice(met) == "", "a met floor must stop asking"

    # Unknown is not the same as outstanding: an unreachable endpoint would otherwise print the
    # full floor as backlog, a number nobody can act on.
    assert slack_briefing.audit_notice(None) == "", "unknown counts must not be reported as owed"

    # The count only moves when a person sits down for a minute, so on every other day it repeats
    # itself word for word -- it stood at 20 across all 8 briefings actually sent, nine days
    # running. A line that never changes is one the reader learns to skip.
    try:
        os.environ["BORING_TODAY"] = "2026-09-15"  # Tuesday, mid-window
        assert slack_briefing.audit_notice(behind) == "", "a daily repeat of yesterday's number"

        os.environ["BORING_TODAY"] = "2026-09-24"  # the last stretch: a deadline is behind it now
        assert "--audit" in slack_briefing.audit_notice(behind)

        os.environ["BORING_TODAY"] = "2026-09-27"  # past the window
        assert slack_briefing.audit_notice(behind) == "", (
            "the figure this unblocks can no longer be computed, so the ask is spent"
        )
    finally:
        os.environ.pop("BORING_TODAY", None)


def test_the_text_fallback_carries_the_shortlist_too():
    slack_briefing = load_module("slack_briefing_fb", ROOT / "slack_briefing.py")

    answer = "# p\n- Blocked: the blocker\n" + "\n".join(
        f"- Next: action {i}" for i in range(6)
    )
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty")
    blocks_text = "\n".join(
        b["text"]["text"] for b in payload["blocks"] if b.get("type") == "section" and "text" in b
    )
    # Slack picks between the two renderings. A fallback missing the one thing the message exists
    # to answer is a second, quieter briefing — the defect this contract was written for.
    assert "*오늘의 1순위*" in body, body
    assert "*오늘의 1순위*" in blocks_text
    shortlist_at = body.index("*오늘의 1순위*")
    group_at = body.index("*행동*")
    assert shortlist_at < group_at, "the shortlist comes before the groups"
    assert "the blocker" in body[shortlist_at:group_at], "the blocker must lead the shortlist"


def test_zones_replace_status_headings_but_keep_the_status():
    slack_briefing = load_module("slack_briefing_zone", ROOT / "slack_briefing.py")

    answer = "# p\n- Blocked: cannot start\n- Stalled: sitting a week\n- Next: do the thing\n- Risk: might break\n"
    body = slack_briefing.render_body_mrkdwn(answer)

    # Six headings were more precision than the classifier delivers — the distiller's own labels
    # wander, and a six-way surface amplifies that instead of absorbing it.
    for gone in ("*막힘*", "*정체 중*", "*다음 행동*", "*리스크*"):
        assert gone not in body, f"{gone} should have collapsed into a zone"
    assert "*행동* (3)" in body
    assert "*참고* (1)" in body
    # The status survives on the item, so a reader still sees blocked-vs-next without the
    # briefing having to be right about which of six boxes the item belongs in.
    for emoji in ("🚨", "⏸️", "▶️", "⚠️"):
        assert emoji in body, f"{emoji} must ride on its item"

    # Two rendering paths carry the emoji — one project with several items nests them under the
    # name, one project with a single item writes it inline. A mutation in either must be caught.
    single = slack_briefing.render_body_mrkdwn(
        "# alpha\n- Blocked: only item\n## beta\n- Next: also only item\n"
    )
    assert "🚨 alpha — only item" in single, single
    assert "▶️ beta — also only item" in single


def test_each_zone_names_the_call_that_digs_into_it():
    slack_briefing = load_module("slack_briefing_follow", ROOT / "slack_briefing.py")

    answer = "# kb-rag-bot\n- Blocked: a\n- Next: b\n- Risk: c\n## other\n- Next: d\n"
    body = slack_briefing.render_body_mrkdwn(answer)

    # Naming the sources bought nothing — Slack cannot open a vault file. Naming the call does:
    # the reader is already in front of an agent that can run it.
    assert "recall(" in body and "claims(" in body, body
    # The project is filled in, not left as a placeholder for the reader to supply.
    assert '"kb-rag-bot"' in body
    assert "project)" not in body, "the briefing knows the name; do not make the reader type it"
    # Deterministic tools lead. next_actions was measured at over 30s with no response, so a
    # suggestion that leaves the reader waiting half a minute must at least say so.
    action_line = next(ln for ln in body.split("\n") if ln.startswith("_→") and "recall(" in ln)
    assert action_line.index("recall(") < action_line.index("next_actions("), action_line
    assert "느림" in action_line


def test_endings_are_shaved_not_truncated():
    slack_briefing = load_module("slack_briefing_shave", ROOT / "slack_briefing.py")

    # Korean puts the verb last, so cutting from the front deletes the action and leaves the
    # object. The tail is shaved instead and the verb stem stays.
    answer = "# p\n- Next: 재현 스크립트를 확보하여 근거를 보강해야 합니다.\n- Next: 코퍼스 경계 검증이 필요합니다.\n"
    body = slack_briefing.render_body_mrkdwn(answer)
    assert "근거를 보강" in body, body
    assert "해야 합니다" not in body
    assert "코퍼스 경계 검증 필요" in body
    # A line matching no pattern is left exactly as written.
    assert slack_briefing.shave_ending("no korean tail here") == "no korean tail here"


def test_no_renderer_names_a_source_file_the_reader_cannot_open():
    """The evidence line said the same thing every day and pointed at nothing openable.

    It was first five wiki titles (277 characters), then one line -- `근거: 위키 5건 (wiki-1501.md
    외 4)`. Measured on what was actually sent: 8 of 8 daily briefings and the 1 weekly that
    carried it all read "5건", because `SOURCE_LIMIT` was 5 and the brief always had at least
    that many. A number that cannot change is not a signal, and Slack has no URL for a vault
    file, so the filename bought nothing either.
    """
    slack_briefing = load_module("slack_briefing_src", ROOT / "slack_briefing.py")
    sources = [f"/vault/wiki/wiki-{i:04d}.md" for i in range(5)]

    text = slack_briefing.render_message_mrkdwn(
        "*brief*", "2026-09-04", "## proj\n- Next: ship it\n", sources, "없음"
    )
    assert "wiki-0001.md" not in text, text
    assert "근거" not in text, text
    assert "ship it" in text, "the items themselves still render"


def test_repeated_project_names_are_written_once():
    slack_briefing = load_module("slack_briefing_group", ROOT / "slack_briefing.py")

    answer = "# omcr\n" + "\n".join(f"- Next: action {i}" for i in range(4))
    body = slack_briefing.render_body_mrkdwn(answer)
    # Five lines that all begin with the same project name spend the first twenty characters of
    # every line saying nothing new, which on a phone is most of the line.
    # Item lines only: the follow-up line names the project too, and that repetition is the
    # point there — it is the argument the reader would otherwise have to type.
    item_lines = [ln for ln in body.split("\n") if ln.startswith(("•", "   ◦"))]
    assert sum(ln.count("omcr") for ln in item_lines) == 1, item_lines
    for i in range(4):
        assert f"action {i}" in body
    assert "◦" in body, "items must nest under the project name"


if __name__ == "__main__":
    # Every `test_*` defined above, discovered rather than listed. The list was hand-maintained
    # and two tests written on 2026-09-02 were simply never called — the same defect this repo
    # already catalogued when a test file sat unregistered in `guard.sh`. A registry you can
    # forget to update is a registry that eventually lies about coverage.
    import sys as _sys

    _module = _sys.modules[__name__]
    _tests = [
        (name, obj)
        for name, obj in sorted(vars(_module).items())
        if name.startswith("test_") and callable(obj)
    ]
    for _name, _fn in _tests:
        _fn()
    print(f"ok - hermes briefing Slack formatting ({len(_tests)} tests)")
