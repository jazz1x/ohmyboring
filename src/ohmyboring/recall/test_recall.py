#!/usr/bin/env python3
"""MCP recall 의 파이썬 이식 (E4-2) — wiki 직독·경로 선택·렌더·그림자 대조.

Run: python3 src/ohmyboring/recall/test_recall.py   (no pytest dependency)

사례의 원천은 시험 이름 뒤에 적는다 (Rust: drudge/src/wiki_recall.rs·retrieve.rs 의 #[test],
엔진 모양: mcp.rs::mcp_recall·recall_text). DB 는 seams 스텁으로 — 라이브 연결 없음.

Mutation targets: 제목 가중을 1 로 바꾸면 title_weighted 가 빨개진다; project 가 있을 때
since_hours 도 거르게 하면 project_overrides 가 빨개진다; 캐시를 항상 다시 읽으면 mtime 캐시
시험이 빨개진다; wiki 가 있어도 벡터를 부르면 wiki_first 가 빨개진다; order_within_set 을 빼면
demoted 시험이 빨개진다; 보강 뒤 텍스트로 대조하는 변이는 문 시험(test_door)이 잡는다;
동점 비교기가 열쇠 순서를 안 보거나(shadow._tie_only 가 늘 참) 늘 tie 를 내면
shadow_v1·shadow_tie 시험이 빨개진다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT / "src", ROOT / "agents" / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import vault_note  # noqa: E402 — the one frontmatter splitter, handed in as the port

from ohmyboring.recall import answer, shadow, wiki  # noqa: E402
from ohmyboring.result import Err, Ok  # noqa: E402
from ohmyboring.search import pg, rank  # noqa: E402

HOUR_NS = 3600 * 10**9
_split = vault_note.split_frontmatter


def _index(read_text=None) -> wiki.WikiIndex:
    return wiki.WikiIndex(_split) if read_text is None else wiki.WikiIndex(_split, read_text)


def _write(directory: Path, name: str, text: str, mtime_ns: int | None = None) -> Path:
    path = directory / name
    path.write_bytes(text.encode("utf-8"))
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _recall(index: wiki.WikiIndex, directory: Path, query: str, *, now_ns=None, **filters):
    now = now_ns if now_ns is not None else time.time_ns()
    return index.recall(directory, wiki.Ask(query, **filters), now)


def _ids(result: wiki.WikiResult, k: int = 5) -> list[str]:
    return [h.id for h in result.hits[:k]]


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
    def title(content: str, stem: str) -> str:
        return wiki.extract_title_body(content, stem, _split)[0]

    assert title("---\nid: wiki-0001\ntitle: 제목A\n---\n본문", "wiki-0001") == "제목A"
    assert title('---\ntitle: "따옴표"\n---\n본문', "s") == "따옴표"
    assert title("# 헤딩B\n본문", "wiki-0002") == "헤딩B"
    assert title("프런트매터 없음", "wiki-0003") == "wiki-0003"
    empty_title = title("---\ntitle:\ntitle: 둘째\n---\n# 헤딩", "s")
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


def test_same_titled_notes_matching_the_query_lead_newest_first_whatever_their_body_scores():
    # 2026-10-07 「ds-governance 판정 인계」: wiki-2760(10-01)·wiki-3834(10-07) 둘 다 20점, 경로순이면 2760 이 1위.
    # 제목이 질의와 같은 인계 노트끼리는 본문 점수가 아니라 새 노트가 앞이다 (2026-10-23 wiki-7115).
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        same = "---\ntitle: 판정 인계\n---\n판정 인계 본문"
        _write(d, "wiki-2760.md", same)
        _write(d, "wiki-3834.md", same)
        _write(d, "wiki-0100.md", "---\ntitle: 판정 인계 판정\n---\n판정 인계 판정 본문")
        assert _ids(_recall(_index(), d, "판정 인계")) == ["wiki-3834", "wiki-2760", "wiki-0100"]
        _write(d, "wiki-2760.md", "---\ntitle: 판정 인계\n---\n판정 인계 본문 " + "판정 " * 10)
        assert _ids(_recall(_index(), d, "판정 인계")) == ["wiki-3834", "wiki-2760", "wiki-0100"]


def test_project_filter():
    # wiki_recall::tests::search_filters_by_project
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: docker cache\nproject: omb\n---\nlayer caching tips")
        _write(d, "wiki-0002.md", "---\ntitle: pg pool\nproject: kb-rag-bot\n---\ntoo many clients fix")
        index = _index()
        assert _ids(_recall(index, d, "tips", project="omb")) == ["wiki-0001"]
        assert _ids(_recall(index, d, "tips", project="kb-rag-bot")) == []


def test_since_hours_filters_by_mtime_and_project_overrides_it():
    # wiki_recall::tests::search_filters_by_since_hours + 엔진 quirk(project 가 since_hours 를 덮음)
    now = 1_000 * HOUR_NS
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: recent\nproject: omb\n---\nrecent content", now)
        _write(d, "wiki-0002.md", "---\ntitle: old\nproject: omb\n---\nold content", now - 48 * HOUR_NS)
        index = _index()
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
        result = _recall(_index(), d, "needle")
        assert _ids(result, k=2) == ["wiki-0002", "wiki-0003"]
        assert _ids(result) == ["wiki-0002", "wiki-0003", "wiki-0001"], "검색은 맞은 노트 전부를 점수순으로"


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
        index = _index(counting_read)
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


def test_missing_wiki_dir_is_an_empty_recall_with_nothing_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        assert _recall(_index(), d / "none", "needle") == wiki.WikiResult([], 0)
        (d / "ignored.txt").write_text("needle")
        assert _recall(_index(), d, "needle") == wiki.WikiResult([], 0), "md 만 색인한다"


def test_unreadable_notes_are_skipped_like_the_engine_and_counted():
    # wiki_recall.rs::refresh — read_to_string 실패는 건너뜀. 파이썬은 건너뛴 수를 값으로 싣는다.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "---\ntitle: ok\n---\nneedle")
        (d / "wiki-0009.md").write_bytes(b"\xff\xfe needle")
        (d / "wiki-0010.md").mkdir()
        result = _recall(_index(), d, "needle")
        assert _ids(result) == ["wiki-0001"] and result.skipped == 2

        def read_denied(path: Path) -> str:
            if path.name == "wiki-0001.md":
                raise PermissionError(13, "denied")
            return path.read_bytes().decode("utf-8")

        _write(d, "wiki-0002.md", "needle needle")
        denied = _recall(_index(read_denied), d, "needle")
        assert _ids(denied) == ["wiki-0002"] and denied.skipped == 3


def test_stat_failure_skips_the_note_and_a_dead_wiki_dir_is_an_empty_recall_counted_once():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "needle")
        (d / "wiki-0002.md").symlink_to(d / "nowhere.md")
        result = _recall(_index(), d, "needle")
        assert _ids(result) == ["wiki-0001"] and result.skipped == 1, "끊긴 심볼릭 링크는 stat 실패"
        locked = d / "locked"
        locked.mkdir()
        _write(locked, "wiki-0003.md", "needle")
        locked.chmod(0o000)
        try:
            assert _recall(_index(), locked, "needle") == wiki.WikiResult([], 1)
        finally:
            locked.chmod(0o700)


def test_a_note_that_goes_unreadable_keeps_its_cached_body_as_in_the_engine():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "needle first", 10 * HOUR_NS)
        index = _index()
        assert _ids(_recall(index, d, "needle")) == ["wiki-0001"]
        (d / "wiki-0001.md").write_bytes(b"\xff\xfe")
        os.utime(d / "wiki-0001.md", ns=(30 * HOUR_NS, 30 * HOUR_NS))
        result = _recall(index, d, "needle")
        assert _ids(result) == ["wiki-0001"] and result.skipped == 1


def test_source_path_is_the_absolute_file_path():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-0001.md", "needle")
        match _recall(_index(), d, "needle").hits:
            case [hit]:
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
    def __init__(  # noqa: PLR0913
        self,
        wiki_hits=(),
        facts=None,
        vector=(),
        superseded=None,
        rank_err=None,
        claims=None,
        claims_err=None,
        vector_err=None,
        concepts=None,
    ):
        self._concepts = concepts or {}
        self._claims, self._claims_err = claims or {}, claims_err
        self._vector_err = vector_err
        self.skipped = 0
        self.calls: list[str] = []
        self.rank_seen: list[list[str]] = []
        self._wiki_hits, self._facts, self._vector = list(wiki_hits), facts or {}, list(vector)
        self._superseded, self._rank_err = superseded or {}, rank_err

    def seams(self) -> answer.Seams:
        return answer.Seams(self._wiki, self._rank, self._vec, self._sup, self._claims_of, self._concepts_of)

    def _concepts_of(self, paths):
        self.calls.append("concepts")
        return Ok({p: v for p, v in self._concepts.items() if p in paths})

    def _claims_of(self, paths):
        self.calls.append("claims")
        return (
            Err(self._claims_err)
            if self._claims_err
            else Ok({p: v for p, v in self._claims.items() if p in paths})
        )

    def _wiki(self, args):
        self.calls.append("wiki")
        hits = sorted(self._wiki_hits, key=lambda h: (-h.score, h.source_path))
        return wiki.WikiResult(hits, self.skipped)

    def _rank(self, paths):
        self.calls.append("rank")
        self.rank_seen.append(paths)
        return Err(self._rank_err) if self._rank_err else Ok(self._facts)

    def _vec(self, args):
        self.calls.append("vector")
        return Err(self._vector_err) if self._vector_err else Ok(self._vector)

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
    assert recalled == answer.Recalled(
        "(no experience recalled)", "vector", refined="(no experience recalled)"
    )
    assert stub.calls == ["wiki", "vector"], "빈 결과에는 superseded·claims 를 묻지 않는다"


def _shown(recalled: answer.Recalled) -> list[str]:
    return [p.rsplit("/", 1)[-1].removesuffix(".md") for p in recalled.shown]


def test_a_note_the_wiki_misses_but_the_vector_finds_enters_the_merged_top5():
    # 2026-10-23 82 질의 측정: 낱말이 하나라도 걸리면 벡터를 안 물어 wiki 밖·벡터 안인 노트가 13개 빠졌다.
    wiki_hits = [_hit(f"w{i}", 10.0 - i, f"낱말{i}") for i in range(5)]
    stub = _Seams(
        wiki_hits=wiki_hits, vector=[("/vault/wiki/x.md", "벡터 본문"), ("/vault/wiki/w0.md", "조각")]
    )
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert _shown(recalled) == ["w0", "x", "w1", "w2", "w3"], _shown(recalled)
    assert recalled.path == "wiki+vector"
    assert "- [w0.md] 낱말0" in recalled.text, "두 길에 다 있는 노트는 wiki 줄을 보인다"
    stub = _Seams(vector=[("/vault/wiki/v.md", "벡터 본문")])
    assert _text(answer.answer(_args(), stub.seams())) == answer.Recalled(
        "- [v.md] 벡터 본문", "vector", shown=("/vault/wiki/v.md",), refined="- [v.md] 벡터 본문"
    )


def test_a_title_match_stays_first_even_when_the_vector_brings_five_other_notes():
    # #491 은 vector=Ok([]) 로만 쟀다 — 합친 뒤에도 제목 일치가 1위인지
    hits = [
        _hit("body-heavy", 31.0),
        wiki.WikiHit("title", "t", "/vault/wiki/title.md", "s", 27.0, exact_title=True),
    ]
    # 벡터 1위가 body-heavy 라 점수만 합치면 body-heavy(1/62+1/61) 가 title(1/61) 을 앞선다
    vector = [("/vault/wiki/body-heavy.md", "벡터"), *((f"/vault/wiki/v{i}.md", "벡터") for i in range(4))]
    recalled = _text(answer.answer(_args(), _Seams(wiki_hits=hits, vector=vector).seams()))
    assert _shown(recalled)[0] == "title", _shown(recalled)


def test_a_failed_vector_path_keeps_the_wiki_answer_and_says_which_path_failed():
    stub = _Seams(wiki_hits=[_hit("w0", 3.0, "본문")], vector_err="embed: refused")
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert (recalled.path, recalled.failed) == ("wiki", (("vector", "embed: refused"),))
    assert recalled.refined.endswith("(vector 길 실패: embed: refused)"), recalled.refined
    assert recalled.text == "- [w0.md] 본문", "엔진 대조용 text 에는 붙이지 않는다"
    empty = _text(answer.answer(_args(max_results=0), stub.seams()))  # 낱말 hit 은 있는데 보일 줄이 0
    assert empty.failed and empty.refined.endswith("(vector 길 실패: embed: refused)"), empty


def test_superseded_label_names_the_newer_notes_and_lines_join_with_a_blank_line():
    # mcp.rs::recall_text
    stub = _Seams(
        wiki_hits=[_hit("old", 5.0, "옛"), _hit("live", 4.0, "새")],
        superseded={"/vault/wiki/old.md": ["/vault/wiki/n1.md", "/vault/wiki/n2.md"]},
    )
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert recalled.text == "- [old.md (superseded by n1.md, n2.md)] 옛\n\n- [live.md] 새", recalled.text
    assert answer.recall_text([("/a/b.md", "x")], {}) == "- [b.md] x"


def test_each_note_carries_one_concepts_line_of_at_most_six_and_the_length_cost_is_small():
    hits = [_hit("a", 5.0, "첫 본문"), _hit("b", 4.0, "둘째 본문"), _hit("c", 3.0, "셋째 본문")]
    names = ["mutation-testing", "single-source-of-truth", "rop", "fold", "adt", "slugify", "dropped-7th"]
    concepts = {"/vault/wiki/a.md": names, "/vault/wiki/b.md": ["rop"]}
    plain = _text(answer.answer(_args(), _Seams(wiki_hits=hits).seams()))
    rich = _text(answer.answer(_args(), _Seams(wiki_hits=hits, concepts=concepts).seams()))
    first = "- [a.md] 첫 본문\n  concepts: mutation-testing, single-source-of-truth, rop, fold, adt, slugify"
    assert rich.text.split("\n\n")[0] == first, rich.text
    assert "dropped-7th" not in rich.text
    assert rich.text.split("\n\n")[1] == "- [b.md] 둘째 본문\n  concepts: rop"
    assert rich.text.split("\n\n")[2] == "- [c.md] 셋째 본문", "no concepts → no line"
    assert rich.refined.count("concepts:") == 2
    assert len(rich.text) > len(plain.text)


def _claim_row(subject: str, predicate: str, value: str, kind: str = "decision") -> pg.RegisterRow:
    return pg.RegisterRow(
        f"claim:{subject}:{predicate}",
        subject,
        predicate,
        value,
        kind,
        "high",
        "2026-10-01T00:00:00+00:00",
        "p",
    )


def _register(*rows: pg.RegisterRow, total: int | None = None) -> pg.RegisterRows:
    return pg.RegisterRows(rows, len(rows) if total is None else total)


_OLD, _LIVE = "/vault/wiki/old.md", "/vault/wiki/live.md"


def _refined_stub(**over) -> _Seams:
    base = {
        "wiki_hits": [
            wiki.WikiHit("old", "옛 결정", _OLD, "옛 본문", 5.0),
            wiki.WikiHit("live", "새 결정", _LIVE, "새 본문", 4.0),
        ],
        "claims": {_OLD: _register(_claim_row("recall", "ranking", "점수식 고정"))},
    }
    return _Seams(**{**base, **over})


def test_refined_note_with_claims_shows_head_title_and_claims_instead_of_the_body():
    recalled = _text(answer.answer(_args(), _refined_stub().seams()))
    assert recalled.refined.splitlines()[:2] == [
        "- [old.md] 옛 결정",
        "  · decision recall/ranking: 점수식 고정",
    ]
    assert "옛 본문" not in recalled.refined
    assert recalled.refined.endswith(answer.FULL_BODY_HINT), "본문 전체를 받는 길을 끝줄이 가리킨다"


def test_refined_note_without_claims_keeps_todays_line():
    recalled = _text(answer.answer(_args(), _refined_stub().seams()))
    assert "- [live.md] 새 본문" in recalled.refined.split("\n\n"), (
        "통제군: claims 없는 노트는 지금 줄 그대로"
    )
    nothing = _text(answer.answer(_args(), _refined_stub(claims={}).seams()))
    assert nothing.refined == nothing.text, "claims 가 하나도 없으면 안내줄도 없다"


def test_refined_superseded_note_keeps_the_label_and_its_claims_and_hides_overflow():
    stub = _refined_stub(
        superseded={_OLD: ["/vault/wiki/n1.md"]},
        claims={_OLD: _register(_claim_row("a", "b", "c"), total=4)},
    )
    refined = _text(answer.answer(_args(), stub.seams())).refined
    assert refined.splitlines()[:3] == [
        "- [old.md (superseded by n1.md)] 옛 결정",
        "  · decision a/b: c",
        "  · … +3 more",
    ]


def test_text_stays_what_the_engine_shadow_compares_against():
    recalled = _text(answer.answer(_args(), _refined_stub().seams()))
    assert recalled.text == "- [old.md] 옛 본문\n\n- [live.md] 새 본문"
    assert recalled.refined != recalled.text


def test_failed_claims_lookup_fails_the_recall():
    stub = _refined_stub(claims_err="db down")
    assert answer.answer(_args(), stub.seams()) == Err(answer.Failed("recall claims: db down"))


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


def test_a_query_equal_to_a_title_puts_that_note_first_unless_it_is_superseded():
    # 2026-10-23 「global-judge 판정 인계 2026-10-23 새벽」: wiki-7115(제목 일치) 27점이 본문에 「판정」 24번인
    # wiki-6885 31점에 밀려 k=3 밖으로 빠졌다.
    title = "global-judge 판정 인계 2026-10-23 새벽"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "wiki-7115.md", f"---\ntitle: {title}\n---\n# 인계\nglobal-judge 판정 인계 2026-10-23 새벽")
        _write(d, "wiki-6885.md", "---\ntitle: kb 인계\n---\n" + "판정 " * 24)
        _write(d, "wiki-7110.md", "---\ntitle: omcr 인계 2026-10-23\n---\n" + "인계 2026-10-23 " * 12)
        _write(d, "wiki-6602.md", "---\ntitle: global-judge 판정 인계 2026-10-21\n---\n" + "판정 인계 " * 10)
        index = _index()

        def first_three(facts) -> list[str]:
            seams = answer.Seams(
                wiki=lambda a: index.recall(d, wiki.Ask(a.query, a.project, a.since_hours), time.time_ns()),
                rank_facts=lambda paths: Ok(facts),
                vector=lambda a: Ok([]),
                superseded_by=lambda paths: Ok({}),
                current_claims=lambda paths: Ok({}),
                concepts=lambda paths: Ok({}),
            )
            recalled = _text(answer.answer(_args(query=f"  {title.upper()} ", max_results=3), seams))
            return [
                line.split("]")[0].removeprefix("- [").removesuffix(".md")
                for line in recalled.text.split("\n\n")
            ]

        assert first_three({})[0] == "wiki-7115", first_three({})
        superseded = {str(d / "wiki-7115.md"): _other(True), str(d / "wiki-6885.md"): _other(False)}
        demoted = first_three(superseded)
        assert demoted[-1] == "wiki-7115", f"대체된 제목 일치 노트는 밀리되 잘리지 않는다: {demoted}"


def test_a_superseded_note_that_leads_by_rrf_sum_sinks_below_the_live_notes_after_fusion():
    # 벡터 1위·wiki 3위라 RRF 합은 s 가 가장 크다 — 합치고도 대체된 노트는 산 노트 뒤
    hits = [_hit("a", 5.0), _hit("b", 4.0), _hit("s", 3.0)]
    stub = _Seams(
        wiki_hits=hits,
        facts=_facts(s=_other(True), a=_other(False), b=_other(False)),
        vector=[("/vault/wiki/s.md", "벡터")],
    )
    recalled = _text(answer.answer(_args(), stub.seams()))
    assert _shown(recalled) == ["a", "b", "s"], _shown(recalled)
    cut = _text(answer.answer(_args(max_results=2), stub.seams()))
    assert _shown(cut) == ["a", "s"], f"RRF 로 컷 안에 든 대체 노트는 밀리되 잘리지 않는다: {_shown(cut)}"
    vector_only = _Seams(
        wiki_hits=[_hit("a", 5.0), _hit("b", 4.0)],
        facts=_facts(v=_other(True), a=_other(False), b=_other(False)),
        vector=[("/vault/wiki/v.md", "벡터")],
    )
    asked_only = replace(
        vector_only.seams(),
        rank_facts=lambda paths: Ok({p: f for p, f in vector_only._facts.items() if p in paths}),
    )  # 묻힌 경로만 돌려준다 — v 의 대체 여부는 벡터 전용 질의로만 들어온다
    assert _shown(_text(answer.answer(_args(), asked_only)))[-1] == "v", "벡터에서만 온 줄도 가라앉는다"


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
        common = {"wiki_dir": Path(tmp), "index": _index(), "now_ns": 0}
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
    return Ok(answer.Recalled(text, path, text))


def _engine_claims(claims):
    return lambda paths: Ok({p: v for p, v in claims.items() if p in paths})


def _compare(status, body, python, claims_for=None):
    return shadow.compare(status, body, python, claims_for or _engine_claims({}))


def test_shadow_same_text_is_ok_and_carries_path_and_line_counts():
    event = _compare(200, _engine("- [a.md] x\n\n- [b.md] y"), _py("- [a.md] x\n\n- [b.md] y"))
    assert event == shadow.ShadowEvent("ok", "wiki", 3, 3)


def test_shadow_mismatch_names_the_first_differing_line_with_80_chars_of_each():
    engine = "- [a.md] x\n\n" + "e" * 120
    python = "- [a.md] x\n\n" + "p" * 120 + "\n\nextra"
    event = _compare(200, _engine(engine), _py(python, "vector"))
    assert event.status == "mismatch" and event.path == "vector"
    assert (event.engine_lines, event.python_lines) == (3, 5)
    assert event.first_diff == shadow.FirstDiff(3, "e" * 80, "p" * 80)
    longer = _compare(200, _engine("a"), _py("a\nb"))
    assert longer.first_diff == shadow.FirstDiff(2, "", "b"), "한쪽이 더 길면 빈 줄과 견준다"


def test_shadow_event_payload_never_carries_the_query():
    secret = "sk-PRIVATE-QUERY"
    event = _compare(200, _engine("엔진 줄"), _py("파이썬 줄"))
    payload = shadow.event_payload(event)
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert set(payload) == {
        "path",
        "engine_lines",
        "python_lines",
        "skipped",
        "first_diff",
        "reason",
        "compared",
        "superseded_hit",
    }
    assert payload["first_diff"] == {"line": 1, "engine": "엔진 줄", "python": "파이썬 줄"}


def test_shadow_error_and_rejection_branches():
    unreadable = _compare(200, _engine("x", is_error=True), _py("x"))
    assert unreadable.status == "error" and "engine unreadable" in (unreadable.reason or "")
    assert _compare(502, b"{}", _py("x")).status == "error"
    assert _compare(200, b"not json", _py("x")).status == "error"
    failed = _compare(200, _engine("x"), Err(answer.Failed("pg connect: down")))
    assert failed.status == "error" and "pg connect: down" in (failed.reason or "")

    rpc = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "missing argument: query"}}
    )
    same = _compare(200, rpc.encode(), Err(answer.Rejected("missing argument: query")))
    assert same.status == "ok" and same.reason == "both rejected identically"
    other = _compare(200, rpc.encode(), Err(answer.Rejected("other")))
    assert other.status == "mismatch"
    assert _compare(200, rpc.encode(), _py("x")).status == "mismatch"
    assert _compare(200, _engine("x"), Err(answer.Rejected("missing argument: query"))).status == "mismatch"


def _wiki_names(*names: str) -> list[wiki.WikiHit]:
    return [_hit(n, float(s), f"본문 {n}") for n, s in (x.split(":") for x in names)]


def _line(name: str, superseded: dict[str, list[str]] | None = None) -> str:
    return answer.recall_line(f"/vault/wiki/{name}.md", f"본문 {name}", superseded or {})


def _py_answer(names: list[str], k: int = 5, skipped: int = 0, **seam_kwargs) -> answer.Recalled:
    stub = _Seams(wiki_hits=_wiki_names(*names), **seam_kwargs)
    stub.skipped = skipped
    return _text(answer.answer(_args(max_results=k), stub.seams()))


def _verdict(python: answer.Recalled, *engine_lines: str) -> str:
    return _compare(200, _engine("\n\n".join(engine_lines)), Ok(python)).status


def test_shadow_tie_only_difference_is_tie_and_identical_text_is_ok():
    python = _py_answer(["a:3", "b:3", "c:3"])
    assert [ln.split("]")[0] for ln in python.text.split("\n\n")] == ["- [a.md", "- [b.md", "- [c.md"]
    assert _verdict(python, _line("a"), _line("b"), _line("c")) == "ok"
    event = _compare(200, _engine("\n\n".join([_line("c"), _line("a"), _line("b")])), Ok(python))
    assert event.status == "tie" and event.first_diff is not None and event.path == "wiki"


def test_shadow_v1_swapping_two_notes_with_different_keys_is_a_mismatch():
    # 점수가 다른 두 노트: 엔진이 뒤집어 냈다면 동점으로 설명되지 않는다.
    python = _py_answer(["x:5", "y:4"])
    assert _verdict(python, _line("y"), _line("x")) == "mismatch"
    # 점수는 같아도 순서 열쇠(owner)가 다르면 열쇠 순서를 어긴 것이다.
    owner = rank.RankFacts(superseded=False, owner=True, updated_at=datetime.fromtimestamp(10, UTC))
    python = _py_answer(["a:3", "b:3"], facts=_facts(b=owner))
    assert [ln.split("]")[0] for ln in python.text.split("\n\n")] == ["- [b.md", "- [a.md"]
    assert _verdict(python, _line("a"), _line("b")) == "mismatch"
    assert _verdict(python, _line("b"), _line("a")) == "ok"
    demoted = _py_answer(["a:3", "b:3"], facts=_facts(a=_other(True)))
    assert _verdict(demoted, _line("a"), _line("b")) == "mismatch", "대체된 노트는 같은 점수여도 뒤로 간다"


def test_shadow_tie_at_the_k_boundary_is_a_tie_but_a_note_outside_the_tied_group_is_a_mismatch():
    python = _py_answer(["a:5", "b:3", "c:3", "d:3"], k=2)
    assert _verdict(python, _line("a"), _line("b")) == "ok"
    assert _verdict(python, _line("a"), _line("c")) == "tie"
    assert _verdict(python, _line("a"), _line("d")) == "tie"
    assert _verdict(python, _line("a"), _line("zzz")) == "mismatch", "묶음 밖(후보에 없는) 노트가 들었다"
    assert _verdict(python, _line("c"), _line("b")) == "mismatch", "점수 5 의 a 가 빠졌다"
    assert _verdict(python, _line("c"), _line("a")) == "mismatch", "열쇠 순서를 어겼다(점수 3 이 5 앞)"
    assert _verdict(python, _line("a")) == "mismatch", "줄 수가 다르다"
    assert _verdict(python, _line("a"), _line("a")) == "mismatch", "같은 노트 두 번"
    below = _py_answer(["a:5", "b:3", "c:3", "e:2"], k=2)
    assert _verdict(below, _line("a"), _line("e")) == "mismatch", "컷 아래 점수의 노트가 올라왔다"


def test_shadow_tied_notes_must_carry_the_same_line_text():
    newer = {"/vault/wiki/b.md": ["/vault/wiki/n.md"]}
    python = _py_answer(["a:3", "b:3"], superseded=newer)
    assert python.text.endswith("- [b.md (superseded by n.md)] 본문 b")
    assert _verdict(python, _line("b", newer), _line("a")) == "tie"
    assert _verdict(python, _line("b"), _line("a")) == "mismatch", "대체 표시가 빠졌다"
    other_body = answer.recall_line("/vault/wiki/a.md", "다른 발췌", {})
    plain = _py_answer(["a:3", "b:3"])
    assert _verdict(plain, _line("b"), other_body) == "mismatch", "동점 노트의 발췌가 다르다"


def test_shadow_vector_path_has_no_tie_allowance():
    stub = _Seams(vector=[("/vault/wiki/a.md", "x"), ("/vault/wiki/b.md", "y")])
    python = _text(answer.answer(_args(), stub.seams()))
    assert python.path == "vector" and python.pool is None
    assert _verdict(python, "- [b.md] y", "- [a.md] x") == "mismatch"


def test_shadow_counts_skipped_wiki_files_in_the_event():
    python = _py_answer(["a:3"], skipped=2)
    event = _compare(200, _engine(_line("a")), Ok(python))
    assert event.status == "ok" and event.skipped == 2
    assert shadow.event_payload(event)["skipped"] == 2
    none = _Seams()
    none.skipped = 4
    empty = _text(answer.answer(_args(), none.seams()))
    assert empty.skipped == 4 and empty.text == "(no experience recalled)"


def test_shadow_compares_refined_text_so_a_claim_line_difference_is_a_mismatch():
    stub = _refined_stub()
    python = _text(answer.answer(_args(), stub.seams()))
    body = _engine(python.text)
    same = _compare(200, body, Ok(python), _engine_claims(stub._claims))
    assert (same.status, same.compared) == ("ok", "refined"), "통제군: 같은 claims 면 ok"
    other = {_OLD: _register(_claim_row("recall", "ranking", "점수식 바뀜"))}
    event = _compare(200, body, Ok(python), _engine_claims(other))
    assert event.status == "mismatch" and event.first_diff is not None, event
    assert "점수식 고정" in event.first_diff.python and "점수식 바뀜" in event.first_diff.engine


def test_shadow_ties_stay_ties_under_the_refined_comparison():
    python = _py_answer(["a:3", "b:3", "c:3"])
    shuffled = _engine("\n\n".join([_line("c"), _line("a"), _line("b")]))
    event = _compare(200, shuffled, Ok(python), _engine_claims({}))
    assert (event.status, event.compared) == ("tie", "refined")


def test_shadow_error_names_its_cause_and_an_embed_outage_is_not_the_engine():
    ok_py = _py("x")
    outage = _compare(
        200, _engine("x"), Err(answer.Failed("retrieve: embed: Connection refused")), _engine_claims({})
    )
    assert (outage.status, outage.cause) == ("error", "embed"), outage
    pg_down = _compare(200, _engine("x"), Err(answer.Failed("pg connect: down")), _engine_claims({}))
    assert pg_down.cause == "python"
    engine_down = _compare(502, b"{}", ok_py, _engine_claims({}))
    assert engine_down.cause == "engine"
    assert shadow.event_payload(outage)["cause"] == "embed"
    assert shadow.event_payload(outage)["compared"] == "refined"


def test_shadow_says_when_a_superseded_note_was_hit():
    newer = {"/vault/wiki/b.md": ["/vault/wiki/n.md"]}
    python = _py_answer(["a:3", "b:3"], superseded=newer)
    hit = _compare(200, _engine(python.text), Ok(python), _engine_claims({}))
    assert shadow.event_payload(hit)["superseded_hit"] is True
    plain = _py_answer(["a:3", "b:3"])
    miss = _compare(200, _engine(plain.text), Ok(plain), _engine_claims({}))
    assert shadow.event_payload(miss)["superseded_hit"] is False


if __name__ == "__main__":
    test_shadow_compares_refined_text_so_a_claim_line_difference_is_a_mismatch()
    test_shadow_ties_stay_ties_under_the_refined_comparison()
    test_shadow_error_names_its_cause_and_an_embed_outage_is_not_the_engine()
    test_shadow_says_when_a_superseded_note_was_hit()
    test_query_terms_splits_and_filters()
    test_substring_match_handles_korean_josa()
    test_title_weighted_and_zero_is_none()
    test_title_from_frontmatter_then_heading_then_stem()
    test_snippet_has_leading_ellipsis_only_when_cut_from_inside_the_text()
    test_snippet_comes_from_the_lowercased_body_at_the_first_term_in_query_order()
    test_same_titled_notes_matching_the_query_lead_newest_first_whatever_their_body_scores()
    test_project_filter()
    test_since_hours_filters_by_mtime_and_project_overrides_it()
    test_hits_are_cut_to_k_by_score_descending()
    test_index_skips_unchanged_files_and_stays_honest_about_edits_and_removals()
    test_missing_wiki_dir_is_an_empty_recall_with_nothing_skipped()
    test_unreadable_notes_are_skipped_like_the_engine_and_counted()
    test_stat_failure_skips_the_note_and_a_dead_wiki_dir_is_an_empty_recall_counted_once()
    test_a_note_that_goes_unreadable_keeps_its_cached_body_as_in_the_engine()
    test_source_path_is_the_absolute_file_path()
    test_parse_args_clamps_and_narrows_at_the_boundary()
    test_missing_query_is_rejected_with_the_engine_message()
    test_empty_result_is_the_exact_engine_phrase_and_asks_the_vector_path_once()
    test_a_note_the_wiki_misses_but_the_vector_finds_enters_the_merged_top5()
    test_a_title_match_stays_first_even_when_the_vector_brings_five_other_notes()
    test_a_failed_vector_path_keeps_the_wiki_answer_and_says_which_path_failed()
    test_superseded_label_names_the_newer_notes_and_lines_join_with_a_blank_line()
    test_superseded_top_scorer_is_demoted_not_cut()
    test_owner_notes_lead_newest_first_and_nothing_outside_the_cut_is_pulled_in()
    test_failed_rank_facts_lookup_fails_the_recall()
    test_refined_note_with_claims_shows_head_title_and_claims_instead_of_the_body()
    test_refined_note_without_claims_keeps_todays_line()
    test_refined_superseded_note_keeps_the_label_and_its_claims_and_hides_overflow()
    test_text_stays_what_the_engine_shadow_compares_against()
    test_failed_claims_lookup_fails_the_recall()
    test_a_query_equal_to_a_title_puts_that_note_first_unless_it_is_superseded()
    test_run_rejects_before_connecting_and_reports_a_dead_store_as_a_value()
    test_shadow_same_text_is_ok_and_carries_path_and_line_counts()
    test_shadow_mismatch_names_the_first_differing_line_with_80_chars_of_each()
    test_shadow_event_payload_never_carries_the_query()
    test_shadow_error_and_rejection_branches()
    test_shadow_tie_only_difference_is_tie_and_identical_text_is_ok()
    test_shadow_v1_swapping_two_notes_with_different_keys_is_a_mismatch()
    test_shadow_tie_at_the_k_boundary_is_a_tie_but_a_note_outside_the_tied_group_is_a_mismatch()
    test_shadow_tied_notes_must_carry_the_same_line_text()
    test_shadow_vector_path_has_no_tie_allowance()
    test_shadow_counts_skipped_wiki_files_in_the_event()
    print("ok - recall: wiki 직독(항·점수·snippet·필터·mtime 캐시)·경로 선택·렌더·그림자 대조")
