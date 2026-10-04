#!/usr/bin/env python3
"""MCP recall 의 파이썬 이식 (E4-2) — wiki 직독·경로 선택·렌더·그림자 대조.

Run: python3 src/ohmyboring/recall/test_recall.py   (no pytest dependency)

사례의 원천은 시험 이름 뒤에 적는다 (Rust: drudge/src/wiki_recall.rs·retrieve.rs 의 #[test],
엔진 모양: mcp.rs::mcp_recall·recall_text). DB 는 seams 스텁으로 — 라이브 연결 없음.

Mutation targets: 제목 가중을 1 로 바꾸면 title_weighted 가 빨개진다; project 가 있을 때
since_hours 도 거르게 하면 project_overrides 가 빨개진다; 캐시를 항상 다시 읽으면 mtime 캐시
시험이 빨개진다; wiki 가 있어도 벡터를 부르면 wiki_first 가 빨개진다; order_within_set 을 빼면
demoted 시험이 빨개진다; 보강 뒤 텍스트로 대조하는 변이는 문 시험(test_door)이 잡는다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ohmyboring.recall import answer, shadow, wiki  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search import rank  # noqa: E402

HOUR_NS = 3600 * 10**9


def _write(directory: Path, name: str, text: str, mtime_ns: int | None = None) -> Path:
    path = directory / name
    path.write_bytes(text.encode("utf-8"))
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _recall(index: wiki.WikiIndex, directory: Path, query: str, *, now_ns=None, k=5, **filters):
    now = now_ns if now_ns is not None else time.time_ns()
    return index.recall(directory, wiki.Ask(query, k, **filters), now)


def _ids(result) -> list[str]:
    match result:
        case Ok(hits):
            return [h.id for h in hits]
        case Err(failure):
            raise AssertionError(f"recall failed: {failure}")


# ── wiki.py (wiki_recall.rs) ────────────────────────────────────────────────


def test_query_terms_splits_and_filters():
    # wiki_recall::tests::query_terms_splits_and_filters
    assert wiki.query_terms("bge-m3 임베딩 a") == ["bge-m3", "임베딩"]
    assert wiki.query_terms('  "Docker!"  _x_ (ab) ') == ["docker", "ab"]
    assert wiki.query_terms("!! ?") == []


def test_substring_match_handles_korean_josa():
    # wiki_recall::tests::score_doc_substring_handles_korean_josa
    terms = wiki.query_terms("임베딩 차원")
    scored = wiki.score_lower("벡터 노트", "bge-m3 임베딩은 1024차원이다", terms)
    assert scored is not None and scored[0] > 0 and "임베딩" in scored[1]


def test_title_weighted_and_zero_is_none():
    # wiki_recall::tests::score_doc_title_weighted_and_zero_is_none
    terms = wiki.query_terms("docker")
    in_title = wiki.score_lower("docker 캐시", "본문 무관", terms)
    in_body = wiki.score_lower("무관", "docker 한 번", terms)
    assert in_title is not None and in_body is not None and in_title[0] == 4.0 and in_body[0] == 2.0
    assert wiki.score_lower("무관", "전혀 다른 내용", terms) is None


def test_title_from_frontmatter_then_heading_then_stem():
    # wiki_recall::tests::extract_title_from_frontmatter_then_heading_then_stem
    assert wiki.extract_title_body("---\nid: wiki-0001\ntitle: 제목A\n---\n본문", "wiki-0001")[0] == "제목A"
    assert wiki.extract_title_body('---\ntitle: "따옴표"\n---\n본문', "s")[0] == "따옴표"
    assert wiki.extract_title_body("# 헤딩B\n본문", "wiki-0002")[0] == "헤딩B"
    assert wiki.extract_title_body("프런트매터 없음", "wiki-0003")[0] == "wiki-0003"
    empty_title = wiki.extract_title_body("---\ntitle:\ntitle: 둘째\n---\n# 헤딩", "s")[0]
    assert empty_title == "헤딩", "빈 title 은 첫 title 줄에서 멈춘다 — 둘째 줄을 보지 않는다"


def test_snippet_has_leading_ellipsis_only_when_cut_from_inside_the_text():
    # 엔진 모양(wiki_recall.rs::snippet_around) — 앞 40자 + 약 200자, 시작이 0 이 아닐 때만 '…'.
    head = wiki.snippet_around("abc needle " + "x" * 300, 4)
    assert not head.startswith("…") and head.startswith("abc needle")
    inside = wiki.snippet_around("y" * 100 + "needle" + "z" * 300, 100)
    assert inside.startswith("…" + "y" * 40 + "needle") and len(inside) == 1 + 200
    assert wiki.snippet_around("a\nb", 0) == "a b"


def test_snippet_comes_from_the_lowercased_body_at_the_first_term_in_query_order():
    # 엔진의 첫 적중은 질의 항 순서상 본문에 처음 나오는 항의 위치 — 가장 앞선 위치가 아니다.
    scored = wiki.score_lower("t", "alpha " + "x" * 100 + " Beta".lower(), ["beta", "alpha"])
    assert scored is not None and scored[1].startswith("…") and "beta" in scored[1]
    upper = wiki.score_lower("t", "ABC".lower(), ["abc"])
    assert upper is not None and upper[1] == "abc", "snippet 은 소문자 본문"


def test_project_filter():
    # wiki_recall::tests::search_filters_by_project
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: docker cache\nproject: omb\n---\nlayer caching tips")
        _write(d, "wiki-0002.md", "---\ntitle: pg pool\nproject: kb-rag-bot\n---\ntoo many clients fix")
        index = wiki.WikiIndex()
        assert _ids(_recall(index, d, "tips", project="omb")) == ["wiki-0001"]
        assert _ids(_recall(index, d, "tips", project="kb-rag-bot")) == []


def test_since_hours_filters_by_mtime_and_project_overrides_it():
    # wiki_recall::tests::search_filters_by_since_hours + 엔진 quirk(project 가 since_hours 를 덮음)
    now = 1_000 * HOUR_NS
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: recent\nproject: omb\n---\nrecent content", now)
        _write(d, "wiki-0002.md", "---\ntitle: old\nproject: omb\n---\nold content", now - 48 * HOUR_NS)
        index = wiki.WikiIndex()
        assert _ids(_recall(index, d, "content", since_hours=24, now_ns=now)) == ["wiki-0001"]
        both = _ids(_recall(index, d, "content", project="omb", since_hours=24, now_ns=now))
        assert sorted(both) == ["wiki-0001", "wiki-0002"], "project 가 있으면 since_hours 는 무시된다"
        assert _ids(_recall(index, d, "content", since_hours=-5, now_ns=now)) == ["wiki-0001"], (
            "음수는 0 시간"
        )


def test_hits_are_cut_to_k_by_score_descending():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: a\n---\nneedle")
        _write(d, "wiki-0002.md", "---\ntitle: b\n---\nneedle needle needle")
        _write(d, "wiki-0003.md", "---\ntitle: c\n---\nneedle needle")
        assert _ids(_recall(wiki.WikiIndex(), d, "needle", k=2)) == ["wiki-0002", "wiki-0003"]


def test_index_skips_unchanged_files_and_stays_honest_about_edits_and_removals():
    # wiki_recall::tests::wiki_index_refresh_is_incremental_and_honest
    now = 1_000 * HOUR_NS
    reads: list[str] = []

    def counting_read(path: Path) -> str:
        reads.append(path.name)
        return path.read_bytes().decode("utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: docker cache\n---\nlayer caching tips", now)
        _write(d, "wiki-0002.md", "---\ntitle: pg pool\n---\ntoo many clients fix", now)
        index = wiki.WikiIndex(counting_read)
        assert _ids(_recall(index, d, "docker layer")) == ["wiki-0001"]
        assert sorted(reads) == ["wiki-0001.md", "wiki-0002.md"]

        reads.clear()
        assert _ids(_recall(index, d, "clients")) == ["wiki-0002"]
        assert reads == [], "바뀌지 않은 파일은 두 번째 호출에서 다시 읽지 않는다"

        _write(
            d, "wiki-0001.md", "---\ntitle: docker cache\n---\nkubernetes oomkilled memory", now + 5 * HOUR_NS
        )
        assert _ids(_recall(index, d, "kubernetes oomkilled")) == ["wiki-0001"]
        assert reads == ["wiki-0001.md"], "mtime 이 바뀐 파일 하나만 다시 읽는다"
        assert _ids(_recall(index, d, "layer caching")) == [], "낡은 본문은 사라진다"

        (d / "wiki-0002.md").unlink()
        assert _ids(_recall(index, d, "clients")) == [], "사라진 노트는 회상되지 않는다"


def test_missing_wiki_dir_is_an_empty_recall_but_an_unreadable_note_is_a_failure_value():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        assert _ids(_recall(wiki.WikiIndex(), d / "none", "needle")) == []
        (d / "ignored.txt").write_text("needle")
        assert _ids(_recall(wiki.WikiIndex(), d, "needle")) == [], "md 만 색인한다"
        (d / "wiki-0009.md").write_bytes(b"\xff\xfe needle")
        match _recall(wiki.WikiIndex(), d, "needle"):
            case Err(failure):
                assert "wiki-0009.md" in failure.detail
            case Ok(_):
                raise AssertionError("a vault read failure must come back as Err, not an empty recall")


def test_source_path_is_the_absolute_file_path():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "needle")
        match _recall(wiki.WikiIndex(), d, "needle"):
            case Ok([hit]):
                assert hit.source_path == str(d / "wiki-0001.md") and hit.id == "wiki-0001"
            case other:
                raise AssertionError(other)


# ── answer.py (mcp.rs::mcp_recall · recall_text) ────────────────────────────


def _args(**over) -> answer.Args:
    base = {"query": "q", "max_results": 5, "max_tokens": 2000, "project": None, "since_hours": None}
    return answer.Args(**{**base, **over})


def _hit(name: str, score: float, snippet: str = "s") -> wiki.WikiHit:
    return wiki.WikiHit(name, name, f"/vault/wiki/{name}.md", snippet, score)


class _Seams:
    def __init__(self, wiki_hits=(), facts=None, vector=(), superseded=None, rank_err=None):
        self.calls: list[str] = []
        self.rank_seen: list[list[str]] = []
        self._wiki_hits, self._facts, self._vector = list(wiki_hits), facts or {}, list(vector)
        self._superseded, self._rank_err = superseded or {}, rank_err

    def seams(self) -> answer.Seams:
        return answer.Seams(self._wiki, self._rank, self._vec, self._sup)

    def _wiki(self, args):
        self.calls.append("wiki")
        return Ok(self._wiki_hits)

    def _rank(self, paths):
        self.calls.append("rank")
        self.rank_seen.append(paths)
        return Err(self._rank_err) if self._rank_err else Ok(self._facts)

    def _vec(self, args):
        self.calls.append("vector")
        return Ok(self._vector)

    def _sup(self, paths):
        self.calls.append("superseded")
        return Ok({p: v for p, v in self._superseded.items() if p in paths})


def _text(result) -> answer.Recalled:
    match result:
        case Ok(recalled):
            return recalled
        case Err(failure):
            raise AssertionError(failure)


def test_parse_args_clamps_and_narrows_at_the_boundary():
    # mcp.rs::mcp_recall — max_results 1..=50 기본 5, max_tokens 1..=16384 기본 2000, 공백 값은 버림.
    def parsed(arguments):
        match answer.parse_args(arguments):
            case Ok(args):
                return args
            case Err(rejected):
                raise AssertionError(rejected)

    assert parsed({"query": " q "}) == _args()
    assert parsed({"query": "q", "max_results": 0}).max_results == 1
    assert parsed({"query": "q", "max_results": 999}).max_results == 50
    assert parsed({"query": "q", "max_results": -3}).max_results == 5, "음수는 u64 가 아니라 기본값"
    assert parsed({"query": "q", "max_results": 2.0}).max_results == 5, "실수는 u64 가 아니다"
    assert parsed({"query": "q", "max_results": True}).max_results == 5
    assert parsed({"query": "q", "max_tokens": 99999}).max_tokens == 16384
    assert parsed({"query": "q", "project": "  "}).project is None
    assert parsed({"query": "q", "project": " omb "}).project == "omb"
    assert parsed({"query": "q", "since_hours": -2}).since_hours == -2
    assert parsed({"query": "q", "since_hours": 2**31}).since_hours is None, "i32 밖이면 버린다"
    assert parsed({"query": "q", "since_hours": "3"}).since_hours is None


def test_missing_query_is_rejected_with_the_engine_message():
    for arguments in ({}, {"query": "   "}, {"query": 5}):
        assert answer.parse_args(arguments) == Err(answer.Rejected("missing argument: query"))


def test_empty_result_is_the_exact_engine_phrase_and_asks_the_vector_path_once():
    stub = _Seams()
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert recalled == answer.Recalled("(no experience recalled)", "vector")
    assert stub.calls == ["wiki", "vector"], "빈 결과에는 superseded 를 묻지 않는다"


def test_wiki_first_the_vector_path_is_not_asked_when_the_wiki_answers():
    stub = _Seams(wiki_hits=[_hit("wiki-0001", 3.0, "본문")], vector=[("/vault/wiki/other.md", "x")])
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert recalled == answer.Recalled("- [wiki-0001.md] 본문", "wiki")
    assert "vector" not in stub.calls
    stub = _Seams(vector=[("/vault/wiki/v.md", "벡터 본문")])
    assert _text(answer.answer(_args(), stub.seams())) == answer.Recalled("- [v.md] 벡터 본문", "vector")


def test_superseded_label_names_the_newer_notes_and_lines_join_with_a_blank_line():
    # mcp.rs::recall_text
    stub = _Seams(
        wiki_hits=[_hit("old", 5.0, "옛"), _hit("live", 4.0, "새")],
        superseded={"/vault/wiki/old.md": ["/vault/wiki/n1.md", "/vault/wiki/n2.md"]},
    )
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert recalled.text == "- [old.md (superseded by n1.md, n2.md)] 옛\n\n- [live.md] 새", recalled.text
    assert answer.recall_text([("/a/b.md", "x")], {}) == "- [b.md] x"


def _facts(**rows: rank.RankFacts) -> dict[str, rank.RankFacts]:
    return {f"/vault/wiki/{name}.md": facts for name, facts in rows.items()}


def _other(superseded: bool) -> rank.RankFacts:
    return rank.RankFacts(superseded=superseded, owner=False, updated_at=None)


def _owner(superseded: bool, secs: int) -> rank.RankFacts:
    return rank.RankFacts(superseded=superseded, owner=True, updated_at=datetime.fromtimestamp(secs, UTC))


def _order(facts) -> list[str]:
    hits = [_hit(n, s) for n, s in (("old", 5.0), ("new", 4.0), ("b", 3.0))]
    recalled = _text(answer.answer(_args(), _Seams(wiki_hits=hits, facts=facts).seams()))
    return [
        line.split("]")[0].removeprefix("- [").removesuffix(".md") for line in recalled.text.split("\n\n")
    ]


def test_superseded_top_scorer_is_demoted_not_cut():
    # retrieve::tests::superseded_top_scorer_is_returned_after_the_live_notes (order_wiki_hits 길)
    assert _order({}) == ["old", "new", "b"], "control: 판정 사실이 없으면 점수 순"
    assert _order(_facts(old=_other(True), new=_other(False))) == ["new", "b", "old"]


def test_owner_notes_lead_newest_first_and_nothing_outside_the_cut_is_pulled_in():
    # retrieve::tests::owner_notes_lead_the_returned_set_newest_first_and_nothing_is_pulled_in
    assert _order(_facts(b=_owner(False, 10))) == ["b", "old", "new"]
    assert _order(_facts(new=_owner(False, 10), b=_owner(False, 20))) == ["b", "new", "old"]
    stub = _Seams(wiki_hits=[_hit("old", 5.0)], facts=_facts(d=_owner(False, 10)))
    _text(answer.answer(_args(), stub.seams()))
    assert stub.rank_seen == [["/vault/wiki/old.md"]], "rank_facts 는 컷 안의 경로만 정렬·중복 없이 묻는다"


def test_failed_rank_facts_lookup_fails_the_recall():
    # retrieve::tests::failed_rank_facts_lookup_fails_the_retrieval
    stub = _Seams(wiki_hits=[_hit("a", 1.0)], rank_err="db down")
    match answer.answer(_args(), stub.seams()):
        case Err(answer.Failed(detail)):
            assert "rank facts lookup" in detail and "db down" in detail
        case other:
            raise AssertionError(other)


def test_run_rejects_before_connecting_and_reports_a_dead_store_as_a_value():
    with tempfile.TemporaryDirectory() as tmp:
        common = {"wiki_dir": Path(tmp), "index": wiki.WikiIndex(), "now_ns": 0}
        dead = "postgresql://u:p@127.0.0.1:9/none"
        assert answer.run({"query": " "}, dsn=dead, **common) == Err(
            answer.Rejected("missing argument: query")
        )
        match answer.run({"query": "q"}, dsn=dead, **common):
            case Err(answer.Failed(detail)):
                assert detail.startswith("pg connect:")
            case other:
                raise AssertionError(other)


# ── shadow.py ───────────────────────────────────────────────────────────────


def _engine(text: str, *, is_error=False) -> bytes:
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()


def _py(text: str, path="wiki"):
    return Ok(answer.Recalled(text, path))


def test_shadow_same_text_is_ok_and_carries_path_and_line_counts():
    event = shadow.compare(200, _engine("- [a.md] x\n\n- [b.md] y"), _py("- [a.md] x\n\n- [b.md] y"))
    assert event == shadow.ShadowEvent("ok", "wiki", 3, 3)


def test_shadow_mismatch_names_the_first_differing_line_with_80_chars_of_each():
    engine = "- [a.md] x\n\n" + "e" * 120
    python = "- [a.md] x\n\n" + "p" * 120 + "\n\nextra"
    event = shadow.compare(200, _engine(engine), _py(python, "vector"))
    assert event.status == "mismatch" and event.path == "vector"
    assert (event.engine_lines, event.python_lines) == (3, 5)
    assert event.first_diff == shadow.FirstDiff(3, "e" * 80, "p" * 80)
    longer = shadow.compare(200, _engine("a"), _py("a\nb"))
    assert longer.first_diff == shadow.FirstDiff(2, "", "b"), "한쪽이 더 길면 빈 줄과 견준다"


def test_shadow_event_payload_never_carries_the_query():
    secret = "sk-PRIVATE-QUERY"
    event = shadow.compare(200, _engine("엔진 줄"), _py("파이썬 줄"))
    payload = shadow.event_payload(event)
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert set(payload) == {"path", "engine_lines", "python_lines", "first_diff", "reason"}
    assert payload["first_diff"] == {"line": 1, "engine": "엔진 줄", "python": "파이썬 줄"}


def test_shadow_error_and_rejection_branches():
    unreadable = shadow.compare(200, _engine("x", is_error=True), _py("x"))
    assert unreadable.status == "error" and "engine unreadable" in (unreadable.reason or "")
    assert shadow.compare(502, b"{}", _py("x")).status == "error"
    assert shadow.compare(200, b"not json", _py("x")).status == "error"
    failed = shadow.compare(200, _engine("x"), Err(answer.Failed("pg connect: down")))
    assert failed.status == "error" and "pg connect: down" in (failed.reason or "")

    rpc = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "missing argument: query"}}
    )
    same = shadow.compare(200, rpc.encode(), Err(answer.Rejected("missing argument: query")))
    assert same.status == "ok" and same.reason == "both rejected identically"
    other = shadow.compare(200, rpc.encode(), Err(answer.Rejected("other")))
    assert other.status == "mismatch"
    assert shadow.compare(200, rpc.encode(), _py("x")).status == "mismatch"
    assert (
        shadow.compare(200, _engine("x"), Err(answer.Rejected("missing argument: query"))).status
        == "mismatch"
    )


def test_run_shadow_lets_a_python_exception_out_for_the_door_to_fold():
    def boom():
        raise RuntimeError("boom")

    try:
        shadow.run_shadow(200, _engine("x"), boom)
    except RuntimeError:
        return
    raise AssertionError("run_shadow must not swallow the python path's exception")


if __name__ == "__main__":
    test_query_terms_splits_and_filters()
    test_substring_match_handles_korean_josa()
    test_title_weighted_and_zero_is_none()
    test_title_from_frontmatter_then_heading_then_stem()
    test_snippet_has_leading_ellipsis_only_when_cut_from_inside_the_text()
    test_snippet_comes_from_the_lowercased_body_at_the_first_term_in_query_order()
    test_project_filter()
    test_since_hours_filters_by_mtime_and_project_overrides_it()
    test_hits_are_cut_to_k_by_score_descending()
    test_index_skips_unchanged_files_and_stays_honest_about_edits_and_removals()
    test_missing_wiki_dir_is_an_empty_recall_but_an_unreadable_note_is_a_failure_value()
    test_source_path_is_the_absolute_file_path()
    test_parse_args_clamps_and_narrows_at_the_boundary()
    test_missing_query_is_rejected_with_the_engine_message()
    test_empty_result_is_the_exact_engine_phrase_and_asks_the_vector_path_once()
    test_wiki_first_the_vector_path_is_not_asked_when_the_wiki_answers()
    test_superseded_label_names_the_newer_notes_and_lines_join_with_a_blank_line()
    test_superseded_top_scorer_is_demoted_not_cut()
    test_owner_notes_lead_newest_first_and_nothing_outside_the_cut_is_pulled_in()
    test_failed_rank_facts_lookup_fails_the_recall()
    test_run_rejects_before_connecting_and_reports_a_dead_store_as_a_value()
    test_shadow_same_text_is_ok_and_carries_path_and_line_counts()
    test_shadow_mismatch_names_the_first_differing_line_with_80_chars_of_each()
    test_shadow_event_payload_never_carries_the_query()
    test_shadow_error_and_rejection_branches()
    test_run_shadow_lets_a_python_exception_out_for_the_door_to_fold()
    print("ok - recall: wiki 직독(항·점수·snippet·필터·mtime 캐시)·경로 선택·렌더·그림자 대조")
