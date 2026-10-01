#!/usr/bin/env python3
"""rank.py 순위 시험 — 융합·판정 넛지·집합 안 순서·예산. 순수 (DB 없음).

Run: python3 ohmyboring/search/test_rank.py   (no pytest dependency)

Mutation targets: FEEDBACK_STEP=0 변이, owner 필터 빼기 변이, rank_key 의 superseded 빼기 변이,
merge_hits 의 판정 net 키 빼기 변이·판정 비교 방향 뒤집기 변이, attach_related 의 키를 source_path 로
되돌리기 변이 각각 여기서 사망 확인. 픽스처는 옳은/틀린 구현이 다른 답을 내는 값으로 고른다 — 점수 차가
정확히 한 step 이하인 두 노트(안 그러면 넛지 없는 구현도 같은 순위를 낸다).
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ohmyboring.search.rank import (  # noqa: E402
    FEEDBACK_STEP,
    Counts,
    Hit,
    RankFacts,
    Scored,
    attach_related,
    merge_hits,
    net_feedback,
    order_within_set,
    pool_size,
    rank_key,
    rrf_term,
    tally_feedback,
    within_budget,
)


def hit(chunk_id: str, path: str, content: str = "x") -> Hit:
    return Hit(
        id=chunk_id,
        content=content,
        origin="personal",
        project="test",
        source_path=path,
        dist=0.5,
        dist_kind="vector_cosine",
    )


def scored_pool() -> list[Scored]:
    """점수 내림차순 풀 — old 가 1위. retrieve.rs 의 score_ordered_pool 와 같은 모양."""
    return [
        Scored(hit(f"{name}#0", f"/{name}.md"), rrf_term(rank))
        for rank, name in enumerate(["old", "new", "b", "c", "d"], start=1)
    ]


def ids(items: list[Scored]) -> list[str]:
    return [item.hit.id for item in items]


class StepTests(unittest.TestCase):
    def test_step_is_exactly_one_rrf_rank_gap(self):
        self.assertEqual(FEEDBACK_STEP, rrf_term(1) - rrf_term(2))

    def test_net_clamps(self):
        self.assertEqual(net_feedback(10, 0), 3)
        self.assertEqual(net_feedback(0, 10), -3)
        self.assertEqual(net_feedback(4, 0), 3, "4 표도 상한 3 과 같은 델타")
        self.assertEqual(net_feedback(5, 5), 0)


class FeedbackFlipTests(unittest.TestCase):
    """(a) 판정 없는 두 노트 — 점수 차가 정확히 한 step 인 픽스처로 used 3 표가 뒤집는다."""

    def _fixture(self, used: int) -> list[Scored]:
        # b: 어휘 1위 = 1/61. a: 벡터 2위 = 1/62 (1위 자리는 판정 없는 c). 차 = 정확히 한 step.
        vec = [hit("c#0", "/c.md"), hit("a#0", "/a.md")]
        txt = [hit("b#0", "/b.md")]
        return merge_hits(vec, txt, {"/a.md": Counts(used=used)})

    def test_used_three_flips_the_pair(self):
        merged = self._fixture(3)
        # 판정 없으면 [c, b, a] — b·c 는 1/61 로 동점(id 오름차순 → b 먼저), a 는 한 step 아래.
        # used 3 으로 a 가 맨 앞으로 — b·c 동점은 그다음 id 오름차순.
        self.assertEqual(ids(merged), ["a#0", "b#0", "c#0"])
        self.assertEqual(merged[0].score, rrf_term(2) + 3 * FEEDBACK_STEP)

    def test_net_four_equals_net_three_cap(self):
        merged = self._fixture(4)
        self.assertEqual(
            merged[0].score,
            rrf_term(2) + 3 * FEEDBACK_STEP,
            "4 표는 3 표와 정확히 같은 델타(상한) — 클램프 없는 구현은 4*step 이다",
        )

    def test_no_counts_keeps_pool_order(self):
        vec = [hit("c#0", "/c.md"), hit("a#0", "/a.md")]
        txt = [hit("b#0", "/b.md")]
        merged = merge_hits(vec, txt, {})
        self.assertEqual(ids(merged), ["b#0", "c#0", "a#0"], "판정 0 이면 점수 순 — 동점은 id 오름차순")


class VerdictTieTests(unittest.TestCase):
    """(q3) 판정 한 표가 만든 동점 — 한 표 = 정확히 한 계단이라 vector 2위(used=1)는 vector 1위(판정 없음)와 동점."""

    def _fixture(self, counts: dict[str, Counts]) -> list[Scored]:
        # A: 벡터 1위 (판정 없음), B: 벡터 2위 — B 가 used 1 표를 얻으면 정확히 한 계단 올라 A 와 동점.
        vec = [hit("a#0", "/a.md"), hit("b#0", "/b.md")]
        return merge_hits(vec, [], counts)

    def test_one_verdict_breaks_the_exact_tie_toward_the_verdict(self):
        merged = self._fixture({"/b.md": Counts(used=1)})
        scores = {item.hit.id: item.score for item in merged}
        self.assertEqual(
            scores["a#0"],
            scores["b#0"],
            "vector 2위 + 한 👍 은 vector 1위와 정확히 동점 (한 표 = 한 계단)",
        )
        self.assertEqual(ids(merged), ["b#0", "a#0"], "동점은 판정이 가른다 — B 가 먼저")

    def test_no_verdicts_distinct_scores_keep_score_order(self):
        merged = self._fixture({})
        self.assertEqual(ids(merged), ["a#0", "b#0"], "대조군: 판정 없음 — 점수 순")


class TallyFeedbackTests(unittest.TestCase):
    """(b) ranking 피드백의 owner 필터 — owner 노트에 owner 아닌 judge 의 contested 는 안 깎는다."""

    ROWS = [
        ("doc:/own.md", "contested", "agent:x", "owner"),  # 깎지 않는다 (owner 글)
        ("doc:/own.md", "contested", "owner", "owner"),  # 깎는다
        ("doc:/own.md", "used", "agent:x", "owner"),  # used 는 누가 판정했든 센다
        ("doc:/other.md", "contested", "agent:x", None),  # owner 글이 아니면 누구든 깎는다
        ("doc:/plain.md", "used", "owner", None),
    ]

    def test_owner_only_filters_non_owner_judge_on_owner_notes(self):
        counts = tally_feedback(self.ROWS, owner_only=True)
        self.assertEqual(counts["/own.md"], Counts(used=1, contested=1))
        self.assertEqual(counts["/other.md"], Counts(contested=1))
        self.assertEqual(counts["/plain.md"], Counts(used=1))

    def test_any_judge_counts_everything(self):
        counts = tally_feedback(self.ROWS, owner_only=False)
        self.assertEqual(counts["/own.md"], Counts(used=1, contested=2))

    def test_doc_prefix_is_stripped(self):
        counts = tally_feedback([("doc:/x.md", "used", "owner", None)], owner_only=True)
        self.assertIn("/x.md", counts)


class RankKeyTests(unittest.TestCase):
    """(c) superseded 는 점수가 높아도 집합 끝 — owner 는 앞에서 갱신 최신 먼저."""

    def _facts(self, rows: dict[str, RankFacts]) -> dict[str, RankFacts]:
        return rows

    def test_superseded_top_scorer_is_returned_after_the_live_notes(self):
        facts = self._facts(
            {
                "/old.md": RankFacts(superseded=True, owner=False, updated_at=None),
                "/new.md": RankFacts(superseded=False, owner=False, updated_at=None),
            }
        )
        control = order_within_set(scored_pool()[:3], {})
        self.assertEqual(ids(control), ["old#0", "new#0", "b#0"], "통제: 점수 순")
        ranked = order_within_set(scored_pool()[:3], facts)
        self.assertEqual(ids(ranked), ["new#0", "b#0", "old#0"], "대체당한 노트는 밀리지 짤리지 않는다")
        budgeted = order_within_set(within_budget(scored_pool(), 3, 300), facts)
        self.assertEqual(ids(budgeted), ["new#0", "b#0", "old#0"], "예산 경로도 같은 순서")

    def test_owner_notes_lead_newest_first(self):
        def owner(secs: int) -> RankFacts:
            return RankFacts(
                superseded=False,
                owner=True,
                updated_at=datetime.fromtimestamp(secs, tz=UTC),
            )

        ranked = order_within_set(
            scored_pool()[:3],
            self._facts({"/b.md": owner(10)}),
        )
        self.assertEqual(ids(ranked), ["b#0", "old#0", "new#0"], "owner 노트가 더 높은 점수 위로")
        ranked = order_within_set(
            scored_pool()[:3],
            self._facts({"/new.md": owner(10), "/b.md": owner(20)}),
        )
        self.assertEqual(ids(ranked), ["b#0", "new#0", "old#0"], "owner 끼리는 갱신 최신 먼저")
        ranked = order_within_set(
            scored_pool()[:3],
            self._facts(
                {"/old.md": RankFacts(True, True, datetime.fromtimestamp(30, tz=UTC)), "/b.md": owner(10)}
            ),
        )
        self.assertEqual(ids(ranked), ["b#0", "new#0", "old#0"], "대첸 owner 노트도 여전히 끝")

    def test_rank_key_shape(self):
        owner_fact = RankFacts(False, True, datetime.fromtimestamp(100, tz=UTC))
        self.assertEqual(rank_key(owner_fact), (False, False, -100.0))
        self.assertEqual(rank_key(RankFacts(False, False, None)), (False, True, float("inf")))
        self.assertEqual(rank_key(None), (False, True, float("inf")))


class BudgetTests(unittest.TestCase):
    """(d) 예산 자르기 — per_hit_cap, 총예산, 빈 조각 건，넘김, max_results 멈춤."""

    def test_per_hit_cap_and_total_budget(self):
        pool = [
            Scored(hit("a#0", "/a.md", "a" * 500), 0.5),
            Scored(hit("b#0", "/b.md", "b" * 500), 0.4),
            Scored(hit("c#0", "/c.md", "c" * 500), 0.3),
        ]
        out = within_budget(pool, max_results=3, max_chars=300)
        self.assertEqual([len(item.hit.content) for item in out], [100, 100, 100])
        self.assertEqual(sum(len(item.hit.content) for item in out), 300)
        self.assertTrue(all(item.hit.content == item.hit.content[:100] for item in out))

    def test_remaining_budget_carries_and_empty_chunks_are_skipped(self):
        # max_results=4, max_chars=250 → per_hit_cap=62. 긴 조각은 cap(62)으로 자르고,
        # 빈 조각은 예산만 먹지 않고 넘긴다.
        pool = [
            Scored(hit("a#0", "/a.md", "a" * 100), 0.5),
            Scored(hit("b#0", "/b.md", ""), 0.45),  # 빈 조각은 예산만 먹지 않고 넘긴다
            Scored(hit("c#0", "/c.md", "c" * 100), 0.4),
            Scored(hit("d#0", "/d.md", "d" * 500), 0.3),
        ]
        out = within_budget(pool, max_results=4, max_chars=250)
        self.assertEqual(ids(out), ["a#0", "c#0", "d#0"])
        self.assertEqual([len(item.hit.content) for item in out], [62, 62, 62])
        self.assertEqual(sum(len(item.hit.content) for item in out), 186)
        self.assertEqual(pool[0].hit.content, "a" * 100, "입력은 그대로")

    def test_max_results_stops_the_cut(self):
        pool = [Scored(hit(f"{n}#0", f"/{n}.md", "x" * 100), 0.5 - n * 0.1) for n in range(5)]
        out = within_budget(pool, max_results=2, max_chars=10_000)
        self.assertEqual(len(out), 2)

    def test_zero_budget_breaks(self):
        pool = [Scored(hit("a#0", "/a.md", "x"), 0.5)]
        self.assertEqual(within_budget(pool, max_results=1, max_chars=0), [])


class PoolTests(unittest.TestCase):
    def test_pool_is_four_k_at_least_twenty(self):
        self.assertEqual(pool_size(3), 20)
        self.assertEqual(pool_size(10), 40)


class AttachRelatedTests(unittest.TestCase):
    """(e) related 는 hit 마다 — 같은 문서의 두 조각이 머리에 같이 있어도 첫 조각만 받는다.

    키를 source_path 로 잡는 변이에서는 두 조각이 한 리스트를 공유해 사망 — live 결함 E2c (wiki-2539):
    engine 은 뒷 조각에 [] 를 줬는데 door 는 첫 조각의 related 를 그대로 얹었다.
    """

    def test_two_chunks_of_one_document_first_takes_the_candidate(self):
        # 머리 둘이 같은 문서 wiki-0957.md 의 조각 — related_lists 는 같은 질의라 같은 후보 목록.
        heads = [("wiki-0957.md#1", "wiki-0957.md"), ("wiki-0957.md#0", "wiki-0957.md")]
        candidates = [[("wiki-0587.md", "older note")], [("wiki-0587.md", "older note")]]
        out = attach_related(heads, ["wiki-0957.md"], candidates, 1200)
        self.assertEqual(
            out.get("wiki-0957.md#1"),
            [{"source_path": "wiki-0587.md", "snippet": "older note"}],
        )
        self.assertEqual(
            out.get("wiki-0957.md#0"),
            [],
            "뒷 조각은 새 related 가 없다 — 첫 조각이 이미 seen 에 넣었다 (http.rs:553-555)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
