"""wiki 직독 회상 — drudge/src/wiki_recall.rs 의 파이썬 이식 (E4-2).

볼트 wiki/*.md 를 읽어 부분 문자열 출현 빈도로 점수를 낸다. 엔진의 이상한 점도 그대로다:
project 가 있으면 since_hours 는 무시되고, snippet 은 소문자 본문에서 떠 오며, 첫 적중 위치는
점수가 가장 높은 항이 아니라 질의 항 순서상 본문에 처음 나오는 항의 것이다. 점수 동점의 순서는
엔진에서 HashMap 순회 순서(비결정)라 여기서는 경로 오름차순으로 고정한다.
읽을 수 없는 노트는 엔진처럼 건너뛰되, 건너뛴 수는 결과 값에 싣는다.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_SNIPPET_BEFORE = 40
_SNIPPET_LEN = 200
_HOUR_NS = 3600 * 10**9
_ALNUM_SPAN = re.compile(r"[^\W_](?:.*[^\W_])?", re.DOTALL)

SplitFrontmatter = Callable[[str], tuple[str, str] | None]


@dataclass(frozen=True)
class WikiHit:
    id: str
    title: str
    source_path: str
    snippet: str
    score: float


@dataclass(frozen=True)
class WikiResult:
    """질의에 맞은 노트 전부(점수 내림차순·동점은 경로순) + 읽지 못해 건너뛴 파일 수."""

    hits: list[WikiHit]
    skipped: int


@dataclass(frozen=True)
class _Doc:
    id: str
    title: str
    source_path: str
    project: str
    title_lower: str
    body_lower: str
    mtime_ns: int


def query_terms(query: str) -> list[str]:
    spans = (_ALNUM_SPAN.search(word) for word in query.split())
    terms = (span.group().lower() for span in spans if span is not None)
    return [term for term in terms if len(term) >= 2]


def snippet_around(text: str, pos: int) -> str:
    start = max(pos - _SNIPPET_BEFORE, 0)
    cut = text[start : start + _SNIPPET_LEN].replace("\n", " ")
    return f"…{cut.strip()}" if start > 0 else cut.strip()


def score_lower(title_lower: str, body_lower: str, terms: list[str]) -> tuple[float, str] | None:
    """제목·본문 모두 소문자인 입력 — Σ본문 출현 + 3·Σ제목 출현 + 맞은 항 수. 0 이면 None."""
    score = 0
    coverage = 0
    first_hit: int | None = None
    for term in terms:
        in_body = body_lower.count(term)
        in_title = title_lower.count(term)
        if in_body + in_title > 0:
            coverage += 1
        score += in_body + 3 * in_title
        if first_hit is None and (found := body_lower.find(term)) >= 0:
            first_hit = found
    if score == 0:
        return None
    return float(score + coverage), snippet_around(body_lower, first_hit or 0)


def _lines(text: str) -> list[str]:
    return [line.removesuffix("\r") for line in text.split("\n")]


def _scalar(line: str) -> str:
    return line.partition(":")[2].strip().strip('"')


def extract_title_body(content: str, stem: str, split: SplitFrontmatter) -> tuple[str, str]:
    yaml, body = split(content) or ("", content)
    declared = next((line for line in _lines(yaml) if line.lstrip().startswith("title:")), None)
    if declared is not None and (title := _scalar(declared)):
        return title, body
    for line in _lines(body):
        if line.startswith("# "):
            return line[2:].strip(), body
    return stem, body


def extract_project(content: str, split: SplitFrontmatter) -> str:
    yaml = (split(content) or ("", ""))[0]
    declared = next((line for line in _lines(yaml) if line.lstrip().startswith("project:")), None)
    return "" if declared is None else _scalar(declared)


def _doc_of(path: Path, content: str, mtime_ns: int, split: SplitFrontmatter) -> _Doc:
    title, body = extract_title_body(content, path.stem, split)
    return _Doc(
        id=path.stem,
        title=title,
        source_path=str(path),
        project=extract_project(content, split),
        title_lower=title.lower(),
        body_lower=body.lower(),
        mtime_ns=mtime_ns,
    )


@dataclass(frozen=True)
class Ask:
    query: str
    project: str | None = None
    since_hours: int | None = None


def search(docs: Iterable[_Doc], ask: Ask, now_ns: int) -> list[WikiHit]:
    terms = query_terms(ask.query)
    if not terms:
        return []
    cutoff = None if ask.since_hours is None else now_ns - max(ask.since_hours, 0) * _HOUR_NS

    def kept(doc: _Doc) -> bool:
        if ask.project is not None:
            return doc.project == ask.project
        return cutoff is None or doc.mtime_ns >= cutoff

    hits = [
        WikiHit(doc.id, doc.title, doc.source_path, scored[1], scored[0])
        for doc in docs
        if kept(doc) and (scored := score_lower(doc.title_lower, doc.body_lower, terms)) is not None
    ]
    hits.sort(key=lambda hit: (-hit.score, -_creation_order(hit.id), hit.source_path))
    return hits


def _creation_order(note_id: str) -> int:
    """wiki-N 의 N — 엔진(remember.rs next_wiki_id)·파이썬(writer) 모두 max+1 로 매겨 생성 순서다.
    date: 는 하루 단위, mtime 은 sync 가 relates_to 를 다시 쓰며 바꾼다. 번호 없는 이름은 맨 뒤."""
    number = note_id.removeprefix("wiki-")
    return int(number) if number.isdigit() else -1


def _read_utf8(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


class WikiIndex:
    """볼트 wiki/ 의 mtime 캐시 — 바뀐 파일만 다시 읽고 사라진 파일은 버린다. 호출마다 디렉터리만
    다시 stat 한다(엔진 WikiIndex::refresh 와 같다). 문은 한 프로세스에 하나를 들고 스레드에서 부른다.

    엔진처럼 못 읽는 노트(비UTF-8·권한·디렉터리 이름이 .md·stat 실패)는 건너뛰고 — 이미 캐시에 있던
    노트는 옛 내용을 그대로 둔다 — 건너뛴 수를 센다. wiki 디렉터리가 없으면 정상(빈 회상), 있는데
    못 열면 빈 회상이되 1 로 센다."""

    def __init__(self, split: SplitFrontmatter, read_text: Callable[[Path], str] = _read_utf8) -> None:
        self._split = split
        self._read_text = read_text
        self._docs: dict[str, _Doc] = {}
        self._lock = threading.Lock()

    def recall(self, wiki_dir: Path, ask: Ask, now_ns: int) -> WikiResult:
        with self._lock:
            skipped = self._refresh(wiki_dir)
            return WikiResult(search(self._docs.values(), ask, now_ns), skipped)

    def _refresh(self, wiki_dir: Path) -> int:
        try:
            names = os.listdir(wiki_dir)
        except (FileNotFoundError, NotADirectoryError):
            self._docs = {}
            return 0
        except OSError:
            self._docs = {}
            return 1
        fresh: dict[str, _Doc] = {}
        skipped = 0
        for name in sorted(names):
            path = wiki_dir / name
            if path.suffix != ".md":
                continue
            cached = self._docs.get(str(path))
            doc = self._load(path, cached)
            if doc is None:
                skipped += 1
            if (kept := doc or cached) is not None:
                fresh[str(path)] = kept
        self._docs = fresh
        return skipped

    def _load(self, path: Path, cached: _Doc | None) -> _Doc | None:
        try:
            mtime_ns = path.stat().st_mtime_ns
            if cached is not None and cached.mtime_ns == mtime_ns:
                return cached
            return _doc_of(path, self._read_text(path), mtime_ns, self._split)
        except (OSError, UnicodeDecodeError):
            return None
