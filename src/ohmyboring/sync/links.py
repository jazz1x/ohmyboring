"""그래프 → 볼트 relates_to 투영 — drudge vault/projection.rs project_links·project_note 의 파이썬 이식.

wiki 노트마다 [[wiki-NNNN]] 링크를 DB(읽기만)에서 계산해 머리말 relates_to 칸만 고쳐 쓴다.
결정(plan_note: 링크 계산 결과 → 무엇을 할지)과 효과(write_plan: 파일 쓰기)를 나눴다. 쓰지 않는
갈래: wiki-*.md 가 아닌 것, seed 노트(wiki-0000, 개인 노트 번호가 공개 샘플에 새는 것 방지), 머리말 없음,
링크 0(비우지 않는다), 내용이 같음. 머리말 쪼개기·relates_to 고쳐 쓰기는 vault.rs 의 것과 바이트까지
같아야 해서(다른 칸 순서·따옴표를 바꾸면 볼트 전체 diff) 공용 split_frontmatter 가 아니라 엄격한 엔진
규칙(`---\\n` 시작, `\\n---\\n` 끝)을 그대로 옮겼다.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ohmyboring.result import Either, Err, Ok

RELATED_LIMIT = 6
SEMANTIC_LIMIT = 4
SEMANTIC_MAX_DIST = 0.40
RECENT_LIMIT = 2
MIN_LINKS = 2
MAX_LINKS = 8
SEED_STEM = "wiki-0000"
FAILED_SAMPLE = 20

# ── SQL — store.rs:1108 / 1144 / 1315 그대로(SELECT 뿐). ─────────────────────────────
RELATED_DOCS_SQL = (
    "WITH self_nodes AS ("
    " SELECT dst FROM edge WHERE src = %(doc)s"
    " AND dst NOT LIKE 'project:%%' AND dst NOT LIKE 'topic:%%')"
    " SELECT e.src, count(*) AS shared"
    " FROM edge e JOIN self_nodes sn ON e.dst = sn.dst"
    " WHERE e.src <> %(doc)s AND e.src LIKE 'doc:%%'"
    " GROUP BY e.src"
    " HAVING count(*) >= 2"
    " ORDER BY shared DESC, e.src ASC"
    " LIMIT %(limit)s;"
)
SEMANTIC_DOCS_SQL = (
    "WITH src AS ("
    " SELECT embedding FROM chunk WHERE source_path = %(path)s AND embedding IS NOT NULL)"
    " SELECT c.source_path, MIN(c.embedding <=> s.embedding)::float8 AS dist"
    " FROM chunk c, src s"
    " WHERE c.source_path <> %(path)s AND c.embedding IS NOT NULL"
    " GROUP BY c.source_path"
    " HAVING MIN(c.embedding <=> s.embedding) <= %(max_dist)s"
    " ORDER BY dist ASC"
    " LIMIT %(limit)s;"
)
RECENT_DOCS_SQL = (
    "SELECT d2.source_path FROM document d1"
    " JOIN document d2 ON d2.project = d1.project AND d2.source_path <> d1.source_path"
    " WHERE d1.source_path = %(path)s AND d1.project <> ''"
    " ORDER BY d2.updated_at DESC"
    " LIMIT %(limit)s;"
)


# ── 순수: 이름·머리말·링크 합치기 ─────────────────────────────────────────────


def wiki_stem(source_path: str) -> str | None:
    """vault.rs:244 — `/…/wiki-0002.md` → `wiki-0002`, wiki 노트가 아니면 None."""
    name = source_path.rsplit("/", 1)[-1]
    stem = name.removesuffix(".md")
    return stem if stem != name and stem.startswith("wiki-") else None


def merge_stems(
    related: Sequence[str], semantic: Sequence[str], recent: Callable[[], Sequence[str]]
) -> list[str]:
    """projection.rs:57-83 — 그래프 이웃 → 의미 이웃(중복 뺌) → 2개 미만이면 같은 프로젝트 최근 문서, 최대 8개."""
    stems = [s for s in map(wiki_stem, related) if s is not None]
    for s in map(wiki_stem, semantic):
        if s is not None and s not in stems:
            stems.append(s)
    if len(stems) < MIN_LINKS:
        for s in map(wiki_stem, recent()):
            if s is not None and s not in stems:
                stems.append(s)
    return stems[:MAX_LINKS]


def split_front(content: str) -> tuple[str, str] | None:
    """vault.rs:192 — 엔진의 엄격한 쪼개기. (머리말 yaml, 닫는 울타리 뒤 본문)."""
    rest = content.removeprefix("---\n")
    if rest is content:
        return None
    end = rest.find("\n---\n")
    if end < 0:
        return None
    return rest[:end], rest[end + 5 :]


def _rust_lines(text: str) -> list[str]:
    """Rust str::lines — `\\n` 으로 자르고 줄 끝 `\\r` 을 떼며 마지막 빈 조각은 없다."""
    if text == "":
        return []
    pieces = text.split("\n")
    last = pieces.pop()
    lines = [p.removesuffix("\r") for p in pieces]
    return lines if last == "" else [*lines, last]


def set_relates_to(yaml_text: str, stems: Sequence[str]) -> str:
    """vault.rs:252 — relates_to 블록만 새 목록으로 바꾸고 나머지 줄은 그대로. 키가 없으면 끝에 붙인다."""
    block = ["relates_to: []"] if not stems else ["relates_to:", *[f'- "[[{s}]]"' for s in stems]]
    out: list[str] = []
    handled = False
    skip_list = False
    for line in _rust_lines(yaml_text):
        if skip_list:
            if line.lstrip().startswith("-"):
                continue
            skip_list = False
        if not handled and line.startswith("relates_to:"):
            out.extend(block)
            handled = True
            skip_list = True
            continue
        out.append(line)
    if not handled:
        out.extend(block)
    return "\n".join(out)


# ── 결정 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NotCandidate:
    path: str


@dataclass(frozen=True)
class SeedNote:
    path: str


@dataclass(frozen=True)
class NoFrontmatter:
    path: str


@dataclass(frozen=True)
class NoLinks:
    path: str


@dataclass(frozen=True)
class AlreadyCurrent:
    path: str


@dataclass(frozen=True)
class Unreadable:
    path: str
    reason: str


@dataclass(frozen=True)
class Rewrite:
    path: str
    content: str


Plan = NotCandidate | SeedNote | NoFrontmatter | NoLinks | AlreadyCurrent | Unreadable | Rewrite


def _is_candidate(name: str) -> bool:
    stem, ext = os.path.splitext(name)
    return ext.lower() == ".md" and stem.startswith("wiki-")


def _by_name(path: str) -> NotCandidate | SeedNote | None:
    name = os.path.basename(path)
    if not _is_candidate(name):
        return NotCandidate(path)
    if os.path.splitext(name)[0] == SEED_STEM:
        return SeedNote(path)
    return None


def read_text(path: str) -> Either[str, str]:
    try:
        with open(path, "rb") as handle:
            return Ok(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        return Err(f"unreadable: {e}")


def plan_note(
    path: str, read: Callable[[str], Either[str, str]], stems_of: Callable[[str], list[str]]
) -> Plan:
    """projection.rs:37-96 의 순서 그대로 — 이름 → seed → 읽기 → 머리말 → 링크 계산 → 비교.
    read·stems_of 는 읽기 전용 주입이라 파일도 DB 도 쓰지 않는다."""
    excluded = _by_name(path)
    if excluded is not None:
        return excluded
    match read(path):
        case Err(reason):
            return Unreadable(path, reason)
        case Ok(content):
            pass
    parts = split_front(content)
    if parts is None:
        return NoFrontmatter(path)
    yaml_text, body = parts
    stems = stems_of(path)
    if not stems:
        return NoLinks(path)
    new_content = f"---\n{set_relates_to(yaml_text, stems)}\n---\n{body}"
    if new_content == content:
        return AlreadyCurrent(path)
    return Rewrite(path, new_content)


# ── 읽기(DB) ────────────────────────────────────────────────────────────────


def _doc_node_id(path: str) -> str:
    return f"doc:{path.rsplit('#', 1)[0]}"


def query_stems(cur: Any, path: str) -> list[str]:
    """세 SELECT 로 이웃을 모아 merge_stems 에 넘긴다. 쓰기 없음."""
    doc = _doc_node_id(path)
    cur.execute(RELATED_DOCS_SQL, {"doc": doc, "limit": RELATED_LIMIT})
    related = [row[0].removeprefix("doc:") for row in cur.fetchall()]
    cur.execute(SEMANTIC_DOCS_SQL, {"path": path, "max_dist": SEMANTIC_MAX_DIST, "limit": SEMANTIC_LIMIT})
    semantic = [row[0] for row in cur.fetchall()]

    def recent() -> list[str]:
        cur.execute(RECENT_DOCS_SQL, {"path": path, "limit": RECENT_LIMIT})
        return [row[0] for row in cur.fetchall()]

    return merge_stems(related, semantic, recent)


def iter_plans(cur: Any, wiki_dir: str) -> Iterator[Plan]:
    """wiki 바로 아래 파일만(재귀 없음 — read_dir) 정렬 순서로."""
    for name in sorted(os.listdir(wiki_dir)):
        path = os.path.join(wiki_dir, name)
        if os.path.isfile(path):
            yield plan_note(path, read_text, lambda p: query_stems(cur, p))


# ── 효과 ────────────────────────────────────────────────────────────────────


def write_plan(plan: Rewrite) -> Either[None, str]:
    try:
        with open(plan.path, "wb") as handle:
            handle.write(plan.content.encode("utf-8"))
        return Ok(None)
    except OSError as e:
        return Err(f"write failed: {e}")


# ── 셈 ──────────────────────────────────────────────────────────────────────


@dataclass
class LinksReport:
    updated: int = 0
    would_update: int = 0
    unchanged: int = 0
    skipped_seed: int = 0
    empty: int = 0
    no_frontmatter: int = 0
    failed: int = 0
    failed_paths: list[dict[str, str]] = field(default_factory=list)

    def fail(self, path: str, reason: str) -> None:
        self.failed += 1
        if len(self.failed_paths) < FAILED_SAMPLE:
            self.failed_paths.append({"path": path, "reason": reason})

    def event_fields(self) -> dict[str, Any]:
        return {
            "links_updated": self.updated,
            "links_unchanged": self.unchanged,
            "links_skipped_seed": self.skipped_seed,
            "links_empty": self.empty,
            "links_no_frontmatter": self.no_frontmatter,
            "links_failed": self.failed,
            "links_failed_paths": self.failed_paths or None,
        }


def _tally_quiet(report: LinksReport, plan: Plan) -> None:
    """쓰지 않는 갈래의 셈 — 쓰기·미리보기가 같이 쓴다."""
    match plan:
        case NotCandidate():
            pass
        case SeedNote():
            report.skipped_seed += 1
        case NoFrontmatter():
            report.no_frontmatter += 1
        case NoLinks():
            report.empty += 1
        case AlreadyCurrent():
            report.unchanged += 1
        case Unreadable(path, reason):
            report.fail(path, reason)
        case Rewrite():
            pass


def run_links(cur: Any, wiki_dir: str) -> LinksReport:
    """wiki 전체를 한 번 돌며 바뀐 노트를 쓴다. 파일 하나의 읽기·쓰기 실패는 그 노트만 실패로 센다."""
    report = LinksReport()
    for plan in iter_plans(cur, wiki_dir):
        match plan:
            case Rewrite():
                match write_plan(plan):
                    case Ok():
                        report.updated += 1
                    case Err(reason):
                        report.fail(plan.path, reason)
            case _:
                _tally_quiet(report, plan)
    return report


def preview_links(cur: Any, wiki_dir: str) -> LinksReport:
    """쓰기 0 — 쓸 노트 수만 would_update 로 센다(--shadow)."""
    report = LinksReport()
    for plan in iter_plans(cur, wiki_dir):
        match plan:
            case Rewrite():
                report.would_update += 1
            case _:
                _tally_quiet(report, plan)
    return report
