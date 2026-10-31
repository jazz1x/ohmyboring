"""4시간 볼트 재색인 — drudge scheduler.rs do_sync → ingest::run + gc_orphans 의 파이썬 이식.

순서: 볼트 wiki 훑기(노트별 판정) → 바뀐 노트만 writer.ingest_changed(remember 와 같은 단계) →
사라진 파일은 지우지 않고 prune_skipped 사건 → gc_orphans → project_links(links.py) → audit 집계 →
sync_run 사건 한 줄. anchor 재고정은 옮기지 않았다(c2 계약 non_goals).

엔진과 다른 두 곳(오너 판정): 해시가 파생 칸을 뺀다, 해시가 같은 노트는 updated_at 도 간선도
건드리지 않고 건너뛴다. 볼트 파일은 쓰지 않는다 — 엔진의 머리말 자동 수선은 실패로 세고 경로를
사건에 싣는다. 실패는 값으로: 노트 하나의 실패(읽기·머리말·임베딩·DB)는 그 노트만 실패로 세고
나머지는 계속한다.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ohmyboring.ingest.chunk import fixed_chunks
from ohmyboring.remember.parse import FrontMatter
from ohmyboring.remember.writer import (
    IngestFailure,
    IngestTimers,
    file_mtime,
    ingest_changed,
)
from ohmyboring.result import Either, Err, Ok
from ohmyboring.sync import audit, links
from ohmyboring.sync.classify import Different, LegacySame, NoRow, Same, judge_sha
from ohmyboring.sync.note import ClassifyOrigin, SplitFrontmatter, parse_vault_note

COMPONENT = "door-sync"
EVENT = "sync_run"
PRUNE_SKIPPED_EVENT = "prune_skipped"
PRUNE_SKIPPED_SAMPLE = 20
#: ingest.rs EXTS · NOISE_SUBSTR 그대로.
_EXTS = (".md", ".markdown", ".txt")
_NOISE = "tool-results"

_DOC_SHAS_SQL = "SELECT source_path, sha FROM document;"
_SET_SHA_SQL = "UPDATE document SET sha = %(sha)s WHERE source_path = %(path)s;"
_DOC_ROW_SQL = "SELECT origin, project, kind, title, tags, author FROM document WHERE source_path = %(path)s;"
_DOC_CHUNKS_SQL = "SELECT content FROM chunk WHERE source_path = %(path)s ORDER BY chunk_idx;"

# ── gc_orphans — store.rs:3609-3645 / 346-348 의 SQL 그대로. ────────────────────────
_STALE_CLAIM_MIRROR_NODES = (
    "(kind = 'claim' AND id NOT IN (SELECT 'claim:' || subject || ':' || predicate FROM claim))"
    " OR (kind IN (SELECT DISTINCT kind FROM claim WHERE kind <> 'fact')"
    " AND id NOT IN (SELECT kind || ':' || subject || ':' || predicate FROM claim WHERE kind <> 'fact'))"
)
_ORPHAN_WHERE = "kind = %(kind)s AND id NOT IN (SELECT src FROM edge UNION SELECT dst FROM edge)"
_GC_ORPHAN_SQL = f"DELETE FROM node WHERE {_ORPHAN_WHERE};"
_GC_ORPHAN_COUNT_SQL = f"SELECT count(*) FROM node WHERE {_ORPHAN_WHERE};"
_GC_DOOMED_SQL = f"SELECT id FROM node WHERE {_STALE_CLAIM_MIRROR_NODES};"
_GC_DOOMED_EDGES_SQL = "DELETE FROM edge WHERE src = ANY(%(ids)s) OR dst = ANY(%(ids)s);"
_GC_DOOMED_NODES_SQL = "DELETE FROM node WHERE id = ANY(%(ids)s);"
_GC_KINDS = ("tool", "concept")


@dataclass(frozen=True)
class SyncDeps:
    """sync 가 주입받는 면 — 문은 진짜를, 시험은 가짜를 싣는다. db_errors 는 노트 하나의 DB 고장으로
    세는 예외 종류(문: psycopg.Error)."""

    wiki_dir: str
    connect: Callable[[], Any]
    embed: Callable[[str], Either[list[float], str]]
    embed_dim: int
    split_frontmatter: SplitFrontmatter
    classify_origin: ClassifyOrigin
    append_event: Callable[..., bool]
    db_errors: tuple[type[Exception], ...] = ()
    #: 엔진 cfg.allow_company_origin — audit 의 clean 판정에만 쓴다.
    allow_company: bool = False


# ── 노트별 판정 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Unchanged:
    path: str


@dataclass(frozen=True)
class LegacyShaOnly:
    path: str
    sha: str


@dataclass(frozen=True)
class Empty:
    """조각 낼 본문이 없다 — 건너뛴다(ingest.rs:437-444)."""

    path: str


@dataclass(frozen=True)
class Failed:
    path: str
    reason: str


@dataclass(frozen=True)
class Ingest:
    path: str
    is_new: bool
    front: FrontMatter
    body: str
    sha: str
    mtime: datetime
    pieces: tuple[str, ...]


Verdict = Unchanged | LegacyShaOnly | Empty | Failed | Ingest


def target_paths(wiki_dir: str) -> list[str]:
    """ingest.rs collect_target_paths — 확장자 md·markdown·txt, tool-results 제외. 순서는 정렬로 고정."""

    def fail(error: OSError) -> None:
        raise error

    found: list[str] = []
    for root, _dirs, names in os.walk(wiki_dir, onerror=fail):
        for name in names:
            path = os.path.join(root, name)
            if name.lower().endswith(_EXTS) and _NOISE not in path:
                found.append(path)
    return sorted(found)


def _read_text(path: str) -> Either[str, str]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        return Ok(data.decode("utf-8").replace("\x00", ""))
    except (OSError, UnicodeDecodeError) as e:
        return Err(f"unreadable: {e}")


def judge_file(
    path: str, db_sha: str | None, split_frontmatter: SplitFrontmatter, classify_origin: ClassifyOrigin
) -> Verdict:
    match _read_text(path):
        case Err(reason):
            return Failed(path, reason)
        case Ok(text):
            pass
    match parse_vault_note(text, path, split_frontmatter, classify_origin):
        case Err(reason):
            return Failed(path, reason)
        case Ok((front, body)):
            pass
    match judge_sha(db_sha, text):
        case Same():
            return Unchanged(path)
        case LegacySame(new_sha):
            return LegacyShaOnly(path, new_sha)
        case NoRow(new_sha) | Different(new_sha):
            sha = new_sha
    body = body.strip()
    pieces = tuple(fixed_chunks(body, 1500, 200))
    if all(not piece.strip() for piece in pieces):
        return Empty(path)
    return Ingest(path, db_sha is None, front, body, sha, file_mtime(path), pieces)


def scan(
    wiki_dir: str,
    db_shas: dict[str, str],
    split_frontmatter: SplitFrontmatter,
    classify_origin: ClassifyOrigin,
) -> Iterator[Verdict]:
    for path in target_paths(wiki_dir):
        yield judge_file(path, db_shas.get(path), split_frontmatter, classify_origin)


def vanished_paths(wiki_dir: str, db_shas: dict[str, str], seen: set[str]) -> list[str]:
    """DB 에는 있고 wiki 아래 파일은 사라진 경로 — 지우지 않고 이름만 댄다(오너 2026-09-24)."""
    prefix = wiki_dir.rstrip("/") + "/"
    return sorted(p for p in db_shas if p.startswith(prefix) and p not in seen)


# ── 셈 ──────────────────────────────────────────────────────────────────────


@dataclass
class Counts:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    relates_only: int = 0
    legacy_sha_only: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    kept_vanished: int = 0
    chunks: int = 0
    failed_paths: list[dict[str, str]] = field(default_factory=list)

    def fail(self, path: str, reason: str) -> None:
        self.failed += 1
        if len(self.failed_paths) < PRUNE_SKIPPED_SAMPLE:
            self.failed_paths.append({"path": path, "reason": reason})


@dataclass
class GcStats:
    tool: int = 0
    concept: int = 0
    claim_nodes: int = 0
    claim_edges: int = 0

    def total(self) -> int:
        return self.tool + self.concept + self.claim_nodes


@dataclass(frozen=True)
class Tail:
    """gc 뒤 단계의 값 — 투영 개수와 위생 집계. 단계가 DB 고장으로 멈추면 error 에 사유가 남는다."""

    link_report: links.LinksReport = field(default_factory=links.LinksReport)
    audit_stats: audit.AuditStats | None = None
    links_error: str | None = None
    audit_error: str | None = None


@dataclass(frozen=True)
class Report:
    counts: Counts
    gc: GcStats
    elapsed_s: float
    tail: Tail = field(default_factory=Tail)


def _tail_fields(tail: Tail) -> dict[str, Any]:
    fields = tail.link_report.event_fields()
    fields["links_error"] = tail.links_error
    fields["audit_error"] = tail.audit_error
    if tail.audit_stats is not None:
        fields.update(tail.audit_stats.event_fields())
    return fields


def _event_fields(report: Report) -> dict[str, Any]:
    c = report.counts
    return {
        **_tail_fields(report.tail),
        "scanned": c.scanned,
        "new": c.new,
        "changed": c.changed,
        "legacy_sha_only": c.legacy_sha_only,
        "unchanged": c.unchanged,
        "skipped": c.skipped,
        "failed": c.failed,
        "kept_vanished": c.kept_vanished,
        "chunks": c.chunks,
        "gc": report.gc.total(),
        "elapsed_s": round(report.elapsed_s, 3),
        "failed_paths": c.failed_paths or None,
    }


# ── gc ──────────────────────────────────────────────────────────────────────


def gc_preview(cur: Any) -> GcStats:
    """지울 개수만 센다 — 읽기 전용(--shadow)."""
    gc = GcStats()
    for kind in _GC_KINDS:
        cur.execute(_GC_ORPHAN_COUNT_SQL, {"kind": kind})
        setattr(gc, kind, cur.fetchone()[0])
    cur.execute(_GC_DOOMED_SQL)
    gc.claim_nodes = len(cur.fetchall())
    return gc


def gc_apply(cur: Any) -> GcStats:
    gc = GcStats()
    for kind in _GC_KINDS:
        cur.execute(_GC_ORPHAN_SQL, {"kind": kind})
        setattr(gc, kind, cur.rowcount)
    cur.execute(_GC_DOOMED_SQL)
    doomed = [row[0] for row in cur.fetchall()]
    gc.claim_nodes = len(doomed)
    if doomed:
        cur.execute(_GC_DOOMED_EDGES_SQL, {"ids": doomed})
        gc.claim_edges = cur.rowcount
        cur.execute(_GC_DOOMED_NODES_SQL, {"ids": doomed})
    return gc


# ── 실행 ────────────────────────────────────────────────────────────────────


def _apply_ingest(deps: SyncDeps, verdict: Ingest, counts: Counts) -> None:
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            stats = ingest_changed(
                cur,
                deps,
                verdict.front,
                verdict.body,
                verdict.path,
                verdict.sha,
                verdict.mtime,
                IngestTimers(),
            )
    except (IngestFailure, *deps.db_errors) as e:
        counts.fail(verdict.path, str(e))
        return
    counts.chunks += stats.chunks
    if verdict.is_new:
        counts.new += 1
    else:
        counts.changed += 1


def _apply(deps: SyncDeps, verdict: Verdict, counts: Counts) -> None:
    match verdict:
        case Unchanged():
            counts.unchanged += 1
        case Empty():
            counts.skipped += 1
        case Failed(path, reason):
            counts.fail(path, reason)
        case LegacyShaOnly(path, sha):
            with deps.connect() as conn, conn.cursor() as cur:
                cur.execute(_SET_SHA_SQL, {"sha": sha, "path": path})
            counts.legacy_sha_only += 1
        case Ingest():
            _apply_ingest(deps, verdict, counts)


def _doc_shas(deps: SyncDeps) -> dict[str, str]:
    with deps.connect() as conn, conn.cursor() as cur:
        cur.execute(_DOC_SHAS_SQL)
        return dict(cur.fetchall())


def _run_tail(deps: SyncDeps) -> Tail:
    """gc 뒤 단계 — 읽기 전용 질의로 relates_to 를 투영하고(파일 쓰기), 위생 집계를 센다. 단계마다 연결을
    따로 써서 한쪽의 DB 고장이 다른 쪽을 막지 않고, 고장은 사건 칸에 사유로 남는다."""
    link_report = links.LinksReport()
    links_error: str | None = None
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            link_report = links.run_links(cur, deps.wiki_dir)
    except deps.db_errors as e:
        links_error = f"{type(e).__name__}: {e}"
    audit_stats: audit.AuditStats | None = None
    audit_error: str | None = None
    try:
        with deps.connect() as conn, conn.cursor() as cur:
            audit_stats = audit.stats(cur, deps.allow_company)
    except deps.db_errors as e:
        audit_error = f"{type(e).__name__}: {e}"
    return Tail(link_report, audit_stats, links_error, audit_error)


def run_sync(deps: SyncDeps) -> Report:
    """한 회차. 연결을 못 여는 것 같은 회차 전체의 고장은 그대로 올라간다 — 부르는 쪽이 사건에 남긴다."""
    started = time.monotonic()
    db_shas = _doc_shas(deps)
    counts = Counts()
    seen: set[str] = set()
    for verdict in scan(deps.wiki_dir, db_shas, deps.split_frontmatter, deps.classify_origin):
        counts.scanned += 1
        seen.add(verdict.path)
        _apply(deps, verdict, counts)
    vanished = vanished_paths(deps.wiki_dir, db_shas, seen)
    counts.kept_vanished = len(vanished)
    if vanished:
        deps.append_event(
            COMPONENT,
            PRUNE_SKIPPED_EVENT,
            "warn",
            count=len(vanished),
            paths=vanished[:PRUNE_SKIPPED_SAMPLE],
        )
    with deps.connect() as conn, conn.cursor() as cur:
        gc = gc_apply(cur)
    tail = _run_tail(deps)
    report = Report(counts, gc, time.monotonic() - started, tail)
    clean = counts.failed == 0 and tail.link_report.failed == 0 and tail.links_error is None
    deps.append_event(
        COMPONENT, EVENT, "ok" if clean and tail.audit_error is None else "warn", **_event_fields(report)
    )
    return report


# ── --shadow: 읽기 전용, 쓰기 0 ──────────────────────────────────────────────


def _relates_only(cur: Any, verdict: Ingest) -> bool:
    """바뀐 것으로 판정됐지만 document 행과 조각 내용이 파일과 이미 같다 — relates_to 만 달라진 노트의
    추정. 머리말의 claims·tools 는 안 봐서 추정이다(그래서 shadow 에서만 따로 센다)."""
    cur.execute(_DOC_ROW_SQL, {"path": verdict.path})
    row = cur.fetchone()
    if row is None:
        return False
    front = verdict.front
    same_row = row[:4] == (front.origin, front.project, front.kind, front.title) and (
        list(row[4] or []),
        row[5],
    ) == (list(front.tags), front.author)
    if not same_row:
        return False
    cur.execute(_DOC_CHUNKS_SQL, {"path": verdict.path})
    return [r[0] for r in cur.fetchall()] == list(verdict.pieces)


def shadow_report(
    cur: Any, wiki_dir: str, split_frontmatter: SplitFrontmatter, classify_origin: ClassifyOrigin
) -> Report:
    """쓰기 0 — 분류와 조각 수, gc 대상 개수만 센다."""
    started = time.monotonic()
    cur.execute(_DOC_SHAS_SQL)
    db_shas = dict(cur.fetchall())
    counts = Counts()
    seen: set[str] = set()
    for verdict in scan(wiki_dir, db_shas, split_frontmatter, classify_origin):
        counts.scanned += 1
        seen.add(verdict.path)
        match verdict:
            case Unchanged():
                counts.unchanged += 1
            case Empty():
                counts.skipped += 1
            case Failed(path, reason):
                counts.fail(path, reason)
            case LegacyShaOnly():
                counts.legacy_sha_only += 1
            case Ingest() if verdict.is_new:
                counts.new += 1
                counts.chunks += len(verdict.pieces)
            case Ingest() if _relates_only(cur, verdict):
                counts.relates_only += 1
            case Ingest():
                counts.changed += 1
                counts.chunks += len(verdict.pieces)
    counts.kept_vanished = len(vanished_paths(wiki_dir, db_shas, seen))
    tail = Tail(link_report=links.preview_links(cur, wiki_dir))
    return Report(counts, gc_preview(cur), time.monotonic() - started, tail)


def shadow_json(report: Report, session_read_only: str) -> dict[str, Any]:
    c = report.counts
    return {
        "mode": "shadow",
        "session_read_only": session_read_only,
        "scanned": c.scanned,
        "new": c.new,
        "changed": c.changed,
        "relates_only": c.relates_only,
        "legacy_sha_only": c.legacy_sha_only,
        "unchanged": c.unchanged,
        "skipped": c.skipped,
        "failed": c.failed,
        "kept_vanished": c.kept_vanished,
        "chunks": c.chunks,
        "gc": {
            "tool": report.gc.tool,
            "concept": report.gc.concept,
            "claim_nodes": report.gc.claim_nodes,
        },
        "failed_paths": c.failed_paths,
        "links_would_update": report.tail.link_report.would_update,
        "links_unchanged": report.tail.link_report.unchanged,
        "links_skipped_seed": report.tail.link_report.skipped_seed,
        "links_empty": report.tail.link_report.empty,
        "links_failed": report.tail.link_report.failed,
        "elapsed_s": round(report.elapsed_s, 3),
    }
