"""remember 쓰기 — 문 안의 파이썬 쓰기 길 (E3c-1).

스위치(DOOR_REMEMBER_WRITER=python)가 켜진 문이 remember 를 엔진에 넘기지 않고 여기서
결정·쓰기·응답까지 한다. 결정은 그림자가 정하는 그대로다 — 그림자 모듈(parse·pii·dedup·
render·graph·index)을 다시 부르고 판정 규칙은 새로 만들지 않는다. 쓰기는 엔진
remember_note 의 행 단위 이식이다 (drudge/src/serve/mcp.rs:1339-1595 · store.rs ·
ingest.rs · vault/remember.rs · vault/projection.rs · anchor.rs) — SQL 문자열은
정본에서 그대로 옮긴다.

쓰는 것 — wiki 번호(vault/remember.rs next_wiki_id: 디스크∪DB 의 max+1, 빈칸 안
채움, O_EXCL 경합), 노트 파일(render.py — 엔진 렌더와 되돌림 동일), document·chunk
(ingest/chunk.py fixed_chunks 1500/200 + adapters/embed.py), 그래프 간선(도구·개념·
claim 노드/간선·태그·프로젝트·said), claim 행(upsert + fact/non-fact 봉인 UPDATE —
store.rs:1473-1539 그대로), supersedes 간선, 사건(dedup_decision·
owner_supersede_refused 는 엔진과 같은 이름·모양, 기록은 문의 사건 sink 로).

대체 봉인만 엔진과 일부러 다르다 — 판정 wiki-2855·2856(오너 확정): 엔진
(store.rs:2898 seal_superseded_claims)은 옛 노트의 현재 claim 을 통째로 닫지만, 여기서는
새 노트가 다시 말한 (subject, predicate) 슬롯만 닫고 새 노트가 말하지 않은 옛 사실은
산다(그래프 그림자의 목표(graph.expected_seal_states)가 정하는 부분 닫기를 실제로 쓴다).
슬롯 명단은 새 노트의 claim 행(캐논화된 슬롯)에서 읽는다. 승계(슬롯을 다음 살아 있는
행으로)는 없다 — 부분 닫기는 노트를 은퇴시키지 않는다. 엔진 sync 가 이 봉인을 다시
통째로 닫지 않는다(동일 sha 재수집은 claim_is_unchanged 로 건 너뛰어 봉인 UPDATE 를
돌리지 않음 — ingest.rs:424-436) — 통째 봉인은 compact 의 sweep(store.rs:2943) 몫으로
남는다.

relates_to 는 이식하지 않고 엔진의 주기 sync 에 맡긴다 — vault/projection.rs 의
project_links 가 sync 마다 볼트의 「모든」 wiki 노트를 다시 계산해 relates_to 를 고치고
(scheduler.rs do_sync 이 끝에 부른다), 링크 집합이 빈 노트는 절대 덧쓰지 않는다. 그러니
파이썬이 쓴 노트의 관계도 다음 sync 까지 늦는다(최대 BORING_SYNC_HOURS, 기본 4h) —
엔진이 쓰기 직후 project_note 를 돌려 바로 채우는 것과의 지연은 사건의 relates_to 칸에
적는다. anchor 도 anchor 까지만 이식한다(anchor.rs: 본문의 project:path:Lx 인용을
찾아 claim 에 붙인다) — anchor_hash·anchor_symbol 은 code_index 소스 파일을 읽고
심볼을 푸는 엔진 전용 계산이라 여기서는 늘 NULL(동일 sha 재수집의 claim_is_unchanged
는 anchor 만 비교하니 이 갭이 sync 재쓰기를 일으키지는 않는다 — 보고의 근거로만 둔다).

스위치를 켠 뒤에도 대조가 남게, 쓰기·걸러짐·거절마다 문 사건 remember_written 한 줄에
결정·경로·간선 수·claim 수·소요 시간을 남긴다. 시간 칸은 전체를 칸들의 합으로 설명하게
나눈다 — 임베딩 칸은 chunk+claim 임베딩까지, DB 칸은 execute+연결+커밋까지, 볼트 칸은
번호·파일 쓰기까지, 파싱 칸은 디스크 훑기의 노트 파싱까지, 근접문서 칸은
nearest_document 한 건 전체(임베딩+읽기 전용 질의)를, 사건 칸은 append_event 들의 합을
잰다(E3c-2). 사건에는 본문 원문을 싣지 않는다(그림자의 비밀 경계와 같다).

실패는 값으로 — 게이트 거절은 Refused(-32602 요청 모양·자격 / -32603 게이트·쓰기
고장), 예상 못한 예외는 문 경계에서 접는다.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ohmyboring.ingest.chunk import fixed_chunks
from ohmyboring.remember import dedup as _dedup
from ohmyboring.remember import graph as _graph
from ohmyboring.remember import pii as _pii
from ohmyboring.remember.parse import (
    RememberNote,
    parse_judge,
    parse_remember_note,
    parse_supersedes,
)
from ohmyboring.remember.render import render_wiki_note
from ohmyboring.result import Either, Err, Ok

#: 스위치가 켜도 대조가 남게 남기는 문 사건 한 줄.
EVENT_NAME = "remember_written"

#: relates_to 지연 사유 — 사건에 적는다(위 docstring 의 근거와 같다).
RELATES_TO_DEFERRED = (
    "deferred: engine sync project_links recomputes relates_to for every wiki note "
    "(up to BORING_SYNC_HOURS, default 4h)"
)

#: 엔진 사건 이름·모양 — mcp.rs:1878 dedup_decision_event, owner.rs:94-115
#: record_refusal 과 같은 이름과 칸으로 기록한다(기록은 문의 사건 sink).
DEDUP_DECISION_EVENT = "dedup_decision"
OWNER_REFUSED_EVENT = "owner_supersede_refused"
_DEDUP_COMPONENT = "drudge.mcp.remember"
_OWNER_COMPONENT = "drudge.owner"

#: 그래프 시맨틱 간선 부류 — store.rs:342 SEMANTIC_EDGE_KINDS.
SEMANTIC_EDGE_KINDS = ("uses", "about", "claims")

# ── SQL — store.rs/ingest.rs 정본 문자열을 그대로 옮긴다(psycopg 명명 파라미터). ───────

_UPSERT_DOCUMENT_SQL = (
    "INSERT INTO document (source_path, origin, project, kind, title, tags, sha, updated_at, author)"
    " VALUES (%(path)s, %(origin)s, %(project)s, %(kind)s, %(title)s, %(tags)s, %(sha)s, %(updated_at)s, %(author)s)"
    " ON CONFLICT (source_path) DO UPDATE SET"
    " origin = EXCLUDED.origin, project = EXCLUDED.project, kind = EXCLUDED.kind,"
    " title = EXCLUDED.title, tags = EXCLUDED.tags, sha = EXCLUDED.sha,"
    " updated_at = EXCLUDED.updated_at, author = EXCLUDED.author;"
)
_DELETE_TAGGED_SQL = "DELETE FROM edge WHERE src = %(doc)s AND kind = 'tagged';"
_UPSERT_NODE_SQL = (
    "INSERT INTO node (id, kind, label, outcome) VALUES (%(id)s, %(kind)s, %(label)s, %(outcome)s)"
    " ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;"
)
_UPSERT_EDGE_SQL = (
    "INSERT INTO edge (src, dst, kind, judge) VALUES (%(src)s, %(dst)s, %(kind)s, %(judge)s)"
    " ON CONFLICT DO NOTHING;"
)
_CLEAR_SEMANTIC_SQL = "DELETE FROM edge WHERE src = %(doc)s AND kind = ANY(%(kinds)s);"
_UPSERT_CHUNK_SQL = (
    "INSERT INTO chunk (id, source_path, content, embedding, origin, project, kind, chunk_idx)"
    " VALUES (%(id)s, %(path)s, %(content)s, %(embedding)s::vector, %(origin)s, %(project)s, %(kind)s, %(chunk_idx)s)"
    " ON CONFLICT (id) DO UPDATE SET"
    " content = EXCLUDED.content, embedding = EXCLUDED.embedding, origin = EXCLUDED.origin,"
    " project = EXCLUDED.project, kind = EXCLUDED.kind, chunk_idx = EXCLUDED.chunk_idx;"
)
_PRUNE_CHUNKS_SQL = "DELETE FROM chunk WHERE source_path = %(path)s AND chunk_idx >= %(from_idx)s;"
_GET_DOC_SHA_SQL = "SELECT sha FROM document WHERE source_path = %(path)s;"
_ALL_DOC_PATHS_SQL = "SELECT source_path FROM document;"
_SET_UPDATED_AT_SQL = (
    "UPDATE document SET updated_at = %(at)s"
    " WHERE source_path = %(path)s AND updated_at IS DISTINCT FROM %(at)s;"
)
_OWNER_AUTHORED_SQL = "SELECT source_path FROM document WHERE source_path = ANY(%(paths)s) AND author = 'owner' ORDER BY source_path;"
_CLAIM_UNCHANGED_SQL = (
    "SELECT 1 FROM claim"
    " WHERE subject = %(subject)s AND predicate = %(predicate)s AND source_path = %(path)s"
    "   AND valid_from = (SELECT max(valid_from) FROM claim"
    "                      WHERE subject = %(subject)s AND predicate = %(predicate)s AND source_path = %(path)s)"
    "   AND value = %(value)s AND kind = %(kind)s AND confidence = %(confidence)s"
    "   AND anchor IS NOT DISTINCT FROM %(anchor)s"
    " LIMIT 1;"
)
_UPSERT_CLAIM_SQL = (
    "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding, kind, confidence, anchor, anchor_hash, anchor_symbol, era)"
    " VALUES (%(subject)s, %(predicate)s, %(value)s, %(path)s, %(valid_from)s, %(embedding)s::vector,"
    "         %(kind)s, %(confidence)s, %(anchor)s::text, NULL::text, NULL::text,"
    "         CASE WHEN %(anchor)s::text IS NULL THEN 'unanchored' ELSE 'anchored' END)"
    " ON CONFLICT (subject, predicate, valid_from) DO UPDATE SET"
    "     value = EXCLUDED.value, source_path = EXCLUDED.source_path,"
    "     embedding = EXCLUDED.embedding, kind = EXCLUDED.kind, confidence = EXCLUDED.confidence,"
    "     anchor = EXCLUDED.anchor, anchor_hash = EXCLUDED.anchor_hash,"
    "     anchor_symbol = EXCLUDED.anchor_symbol, era = EXCLUDED.era;"
)
_MIRROR_SAID_BY_SQL = (
    "UPDATE claim SET said_by = %(said_by)s"
    " WHERE subject = %(subject)s AND predicate = %(predicate)s AND source_path = %(path)s"
    "   AND said_by IS DISTINCT FROM %(said_by)s"
    "   AND valid_from = (SELECT max(valid_from) FROM claim"
    "                      WHERE subject = %(subject)s AND predicate = %(predicate)s AND source_path = %(path)s);"
)

#: 은퇴 정의 — store.rs:358-363 의 RETIRED_NOTE 조각을 문자 그대로 (c = claim 별명).
_RETIRED_NOTE = (
    "EXISTS (SELECT 1 FROM edge e"
    " JOIN document n ON n.source_path = substr(e.src, 5)"
    " WHERE e.kind = 'supersedes' AND e.dst = 'doc:' || c.source_path"
    "   AND (n.author = 'owner' OR NOT EXISTS (SELECT 1 FROM document o"
    "                                         WHERE o.source_path = c.source_path"
    "                                           AND o.author = 'owner')))"
)

_FACT_SEAL_SQL = (
    "UPDATE claim c SET superseded_at = m.mx"
    " FROM (SELECT c.subject, c.predicate, max(c.valid_from) AS mx FROM claim c"
    f"       WHERE c.subject = %(subject)s AND c.predicate = %(predicate)s AND NOT {_RETIRED_NOTE}"
    "       GROUP BY c.subject, c.predicate) m"
    " WHERE c.subject = m.subject AND c.predicate = m.predicate"
    "   AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx"
    "   AND (NOT EXISTS (SELECT 1 FROM document o"
    "                     WHERE o.source_path = c.source_path AND o.author = 'owner')"
    "        OR EXISTS (SELECT 1 FROM claim l JOIN document d ON d.source_path = l.source_path"
    "                    WHERE l.subject = m.subject AND l.predicate = m.predicate"
    "                      AND l.valid_from = m.mx AND d.author = 'owner'));"
)
_FACT_UNSEAL_SQL = (
    "UPDATE claim c SET superseded_at = NULL"
    " WHERE c.subject = %(subject)s AND c.predicate = %(predicate)s AND c.superseded_at IS NOT NULL"
    "   AND c.valid_from = (SELECT max(c.valid_from) FROM claim c"
    "                       WHERE c.subject = %(subject)s AND c.predicate = %(predicate)s"
    f"                         AND NOT {_RETIRED_NOTE});"
)
_ITEM_SEAL_SQL = (
    "UPDATE claim c SET superseded_at = m.mx"
    " FROM (SELECT subject, predicate, source_path, max(valid_from) AS mx FROM claim"
    "       WHERE subject = %(subject)s AND predicate = %(predicate)s AND source_path = %(path)s"
    "       GROUP BY subject, predicate, source_path) m"
    " WHERE c.subject = m.subject AND c.predicate = m.predicate"
    "   AND c.source_path = m.source_path"
    "   AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx;"
)
_ITEM_UNSEAL_SQL = (
    "UPDATE claim c SET superseded_at = NULL"
    " WHERE c.subject = %(subject)s AND c.predicate = %(predicate)s AND c.source_path = %(path)s"
    "   AND c.superseded_at IS NOT NULL"
    "   AND c.valid_from = (SELECT max(valid_from) FROM claim"
    "                     WHERE subject = %(subject)s AND predicate = %(predicate)s"
    "                       AND source_path = %(path)s)"
    f"   AND NOT {_RETIRED_NOTE};"
)

#: 대체 부분 닫기 — engine seal_superseded_claims(store.rs:2898-2911)의 「통째로」를
#: 「새 노트가 다시 말한 슬롯만」으로 바꾼 것. 슬롯 명단은 새 노트의 claim 행에서 읽는다.
_PARTIAL_SEAL_SQL = (
    "UPDATE claim c SET superseded_at = COALESCE("
    "        (SELECT max(valid_from) FROM claim WHERE source_path = %(new)s), now())"
    " WHERE c.source_path = %(old)s AND c.superseded_at IS NULL"
    "   AND (c.subject, c.predicate) IN (SELECT subject, predicate FROM claim WHERE source_path = %(new)s);"
)


@dataclass(frozen=True)
class Written:
    """엔진 Remembered 의 파이썬 대응 — 응답 모양 재료.

    MCP 본문은 message 그대로, HTTP 본문은 다섯 칸(source_path·wiki_id·duplicate·
    supersedes·unknown — serve.rs:961-968 RememberResp)만 쓴다. 걸러진 중복도 이 모양으로
    난다(duplicate 가 기존 노트 경로를 싣는다 — mcp.rs:1703 skipped_duplicate)."""

    wiki_id: str
    source_path: str
    message: str
    duplicate: str | None
    supersedes: int
    unknown: int


@dataclass(frozen=True)
class Refused:
    """게이트·쓰기 거절 — code 는 엔진과 같다(-32602 요청 모양·자격 / -32603 게이트·쓰기)."""

    code: int
    message: str


WriteOutcome = Written | Refused


@dataclass(frozen=True)
class WriteRequest:
    """문이 싣는 remember 요청 하나 — route 는 사건 기록용 식별."""

    route: str  # "mcp" | "remember"
    arguments: dict[str, Any]
    omb_session_id: str | None


@dataclass(frozen=True)
class WriterDeps:
    """쓰기 길이 주입받는 면 — 문은 진짜를 싣고, 시험은 가짜를 싣는다.

    connect 는 쓰기 가능한 psycopg 연결을 내는 공장(문: DOOR_PG_DSN). 읽기 면은 그림자와
    같은 모양이다. embed 는 (텍스트) → Ok(벡터) | Err(사유) — adapters/embed.py 를
    감싼 것. note_index 가 있으면 중복 문 스캔은 쓰기마다 재고한 색인 목록으로 본다
    (그림자의 E3b-2 규약과 같다). clock 은 date·fallback now 용(기본 UTC now)."""

    vault_dir: str
    connect: Callable[[], Any]
    read_note: Callable[[str, str], str | None]
    split_frontmatter: Callable[[str], tuple[str, str] | None]
    list_notes: Callable[[], list[str]]
    pii_scanner: _pii.PiiScanner | None
    is_owner: bool
    nearest_document: _dedup.NearestDocument | None
    embed: Callable[[str], Either[list[float], str]]
    append_event: Callable[..., bool]
    note_index: Any | None = None
    embed_dim: int = 1024
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass
class _Stats:
    """ingest::Stats 의 문서 수집 칸 — 응답 문장의 graph(...) 숫자가 된다."""

    chunks: int = 0
    tools: int = 0
    concepts: int = 0
    claims: int = 0
    claims_unchanged: int = 0
    edges: int = 0
    skipped: bool = False


@dataclass
class _Timers:
    embedding: float = 0.0
    db: float = 0.0
    vault: float = 0.0
    parse: float = 0.0
    nearest: float = 0.0
    event: float = 0.0


@dataclass
class _Ctx:
    """한 번의 쓰기가 끌고 다니는 맥락 — 사건의 소요 시간 칸을 여기서 모은다."""

    deps: WriterDeps
    request: WriteRequest
    timers: _Timers
    started: float


@dataclass(frozen=True)
class _Plan:
    """저장 한 번의 입력 — 게이트·중복 문을 지난 뒤 정해진 것."""

    note: RememberNote
    targets: list[str]
    duplicate: str | None
    judge: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class _GraphJob:
    """그래프 투영 한 번의 입력 — 머리말·본문·경로·시각(valid_from)·본문 앵커."""

    front: Any
    body: str
    path: str
    valid_from: datetime
    anchors: list = field(default_factory=list)


class _WriteFailure(Exception):
    """쓰기 도중의 예상 가능한 고장 — 임베딩 불응·차원 불일치·SQL 고장."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _TimedCursor:
    """execute 시간을 재는 커서 껍질 — 진짜·가짜 커서 모두 감싼다."""

    def __init__(self, inner: Any, timers: _Timers) -> None:
        self._inner = inner
        self._timers = timers

    def execute(self, sql: str, params: Any = None) -> Any:
        started = time.monotonic()
        try:
            return self._inner.execute(sql, params)
        finally:
            self._timers.db += time.monotonic() - started

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TimedConn:
    """연결 맥락 껍질 — commit/rollback(= with 탈출) 시간을 db 칸에 더한다.

    문의 쓰기는 요청마다 새 연결을 열고 with 탈출에 커밋을 건다 — 이 두 시간은
    execute 재기와는 따로 잰다(E3c-2: 연결+커밋이 재는 밖에 있어 잔차로 샜다)."""

    def __init__(self, inner: Any, timers: _Timers) -> None:
        self._inner = inner
        self._timers = timers

    def cursor(self) -> Any:
        return self._inner.cursor()

    def __enter__(self) -> Any:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        started = time.monotonic()
        try:
            return self._inner.__exit__(*exc)
        finally:
            self._timers.db += time.monotonic() - started

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _TimedSeams:
    """볼트 읽기·색인 재고·파싱·사건 기록에 시간을 재는 그림자 규약의 껍질."""

    def __init__(self, deps: WriterDeps, timers: _Timers) -> None:
        self._deps = deps
        self._timers = timers

    def read_note(self, vault_dir: str, note_id: str) -> str | None:
        started = time.monotonic()
        try:
            return self._deps.read_note(vault_dir, note_id)
        finally:
            self._timers.vault += time.monotonic() - started

    def list_notes(self) -> list[str]:
        started = time.monotonic()
        try:
            return self._deps.list_notes()
        finally:
            self._timers.vault += time.monotonic() - started

    def parse_note(self, source_path: str, text: str, split_frontmatter: Any) -> Any:
        """디스크 훑기의 노트 파싱 — 칸에 안 잡히던 바로 그 시간(E3c-2). 색인이 없어
        중복 문 스캔이 디스크 훑기로 떨어질 때 claim 마다 이 껍질로 읽는다."""
        started = time.monotonic()
        try:
            return _dedup.parse_existing_note(source_path, text, split_frontmatter)
        finally:
            self._timers.parse += time.monotonic() - started

    def append_event(self, component: str, event: str, status: str, **fields: Any) -> bool:
        """사건 기록 한 건 — 사건 칸에 잰다(싱크가 느려도 잔차로 새지 않게)."""
        started = time.monotonic()
        try:
            return self._deps.append_event(component, event, status, **fields)
        finally:
            self._timers.event += time.monotonic() - started

    def sync_index(self) -> tuple[tuple[str, _dedup.ExistingNote], ...] | None:
        """문 안의 노트 색인 재고 — 채우기 전이면 None(디스크 훑기로, 그림자 규약)."""
        index = self._deps.note_index
        if index is None:
            return None
        started = time.monotonic()
        try:
            synced = index.sync()
        finally:
            self._timers.vault += time.monotonic() - started
        if synced is None:
            return None
        self._timers.parse += synced.parse_s
        return synced.entries

    def refresh_index(self) -> None:
        """쓴 뒤 색인 한 번 더 — 다음 중복 문 스캔이 이 노트를 디스크와 같이 보게."""
        if self._deps.note_index is not None:
            self._deps.note_index.sync()


# ── 앵커 — drudge anchor.rs:1-181 의 순수 파싱만 이식한다(hash/symbol 은 엔진 전용). ────

_SOURCE_EXTS = frozenset({"rs", "py", "ts", "tsx", "js", "go", "sh", "sql", "toml", "yaml", "yml", "md"})
_ANCHOR_CAP = 20
_ANCHOR_OPEN = "'\"([{`"
_ANCHOR_CLOSE = "'\")]}.,:;:!?`"


@dataclass(frozen=True)
class _Anchor:
    project: str
    path: str
    span: tuple[int, int] | None  # (start, end) — end > start 면 Lstart-Lend

    def to_db_string(self) -> str:
        if self.span is not None:
            start, end = self.span
            span = f"L{start}-L{end}" if end > start else f"L{start}"
            return f"{self.project}:{self.path}:{span}"
        return f"{self.project}:{self.path}"


def _has_source_ext(path: str) -> bool:
    ext = path.rsplit(".", 1)[1] if "." in path else ""
    return ext.lower() in _SOURCE_EXTS


def _normalize_path(path: str) -> str | None:
    while path.startswith("./"):
        path = path[2:]
    return path or None


def _is_path_char(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "_-./~")


def _parse_span(body: str, at: int) -> tuple[tuple[int, int], int] | None:
    end = at
    while end < len(body) and body[end].isascii() and body[end].isdigit():
        end += 1
    if end == at:
        return None
    start = int(body[at:end])
    if end < len(body) and body[end] == "-":
        end2 = end + 1
        while end2 < len(body) and body[end2].isascii() and body[end2].isdigit():
            end2 += 1
        if end2 > end + 1:
            return ((start, int(body[end + 1 : end2])), end2)
    return ((start, start), end)


def _t1_mentions(project: str, body: str) -> list[tuple[int, _Anchor]]:
    """path:12[-15] 인용 — 콜론+숫자를 찾고 path 문자만 뒤로 걸어 경로를 복원한다."""
    out: list[tuple[int, _Anchor]] = []
    i = 0
    while i + 1 < len(body):
        if body[i] == ":" and body[i + 1].isascii() and body[i + 1].isdigit():
            start = i
            while start > 0 and _is_path_char(body[start - 1]):
                start -= 1
            if start > 0 and body[start - 1] == ":" and body[start] == "/":
                i += 1
                continue
            path = _normalize_path(body[start:i])
            if (
                path is None
                or not ("/" in path or _has_source_ext(path))
                or path.startswith("~")
                or path.startswith("/tmp")
            ):
                i += 1
                continue
            parsed = _parse_span(body, i + 1)
            if parsed is None:
                i += 1
                continue
            span, resume = parsed
            out.append((start, _Anchor(project, path, span)))
            i = resume
            continue
        i += 1
    return out


def _trim_decoration(token: str) -> str:
    """토큰 양끝의 괄호·따옴표·문장 부호를 깐다(anchor.rs trim_decoration)."""
    while token and token[0] in _ANCHOR_OPEN:
        token = token[1:]
    while token and token[-1] in _ANCHOR_CLOSE:
        token = token[:-1]
    return token


def _t1b_mentions(project: str, body: str) -> list[tuple[int, _Anchor]]:
    """소스 확장자를 가진 경로 토큰(줄·범위 없이) — 공백 단위로만 본다."""
    out: list[tuple[int, _Anchor]] = []
    offset = 0
    for token in body.split():
        raw = token
        token = _trim_decoration(token)
        if ":" in token or "/" not in token or not _has_source_ext(token):
            offset += len(raw) + 1
            continue
        if token.startswith("~") or token.startswith("/tmp") or "://" in token:
            offset += len(raw) + 1
            continue
        path = _normalize_path(token)
        if path is not None:
            out.append((offset, _Anchor(project, path, None)))
        offset += len(raw) + 1
    return out


def _from_note_body(project: str, body: str) -> list[_Anchor]:
    """본문에서 앵커 목록 — 위치순, to_db_string 별 중복 제거, 상한 20(anchor.rs:88-114)."""
    found = sorted([*_t1_mentions(project, body), *_t1b_mentions(project, body)], key=lambda p: p[0])
    seen: set[str] = set()
    out: list[_Anchor] = []
    for _, anchor in found:
        key = anchor.to_db_string()
        if key in seen:
            continue
        seen.add(key)
        out.append(anchor)
        if len(out) >= _ANCHOR_CAP:
            break
    return out


def _anchor_for_claim(note_anchors: list[_Anchor], subject: str, value: str) -> _Anchor | None:
    """claim 이 가리키는 앵커 하나 — haystack(소문러 subject+value)에 경로 또는 파일명이
    들어 있으면 그 첫 앵커(anchor.rs:73-82)."""
    haystack = f"{subject} {value}".lower()
    for anchor in note_anchors:
        file_name = anchor.path.rsplit("/", 1)[-1]
        if anchor.path.lower() in haystack or file_name.lower() in haystack:
            return anchor
    return None


def embed_via_adapter(text: str) -> Either[list[float], str]:
    """adapters/embed.py 를 문의 사유 문자열 규약으로 감싼 것."""
    from ohmyboring.adapters import embed as embed_adapter

    match embed_adapter.embed(text):
        case Ok(vec):
            return Ok(vec)
        case Err(failure):
            return Err(str(failure))


# ── 번호 매기기 — vault/remember.rs:105-168 그대로 (max+1, 빈칸 안 채움, O_EXCL). ──────


def existing_wiki_ids(doc_paths: list[str]) -> set[int]:
    """DB document.source_path 에 이미 잡힌 wiki 번호 — next_wiki_id 의 db_ids."""
    out: set[int] = set()
    for path in doc_paths:
        stem = path.rsplit("/", 1)[-1].removesuffix(".md")
        if stem.startswith("wiki-"):
            try:
                n = int(stem[5:])
            except ValueError:
                continue
            if 0 <= n <= 0xFFFFFFFF:
                out.add(n)
    return out


def allocate_wiki_path(wiki_dir: str, db_ids: set[int]) -> tuple[str, str]:
    """다음 wiki-NNNN 을 O_EXCL 로 만들고 (id, 절대 경로)를 돌려준다 — 경합은 재시정."""
    os.makedirs(wiki_dir, exist_ok=True)
    used = set(db_ids)
    for name in os.listdir(wiki_dir):
        if not name.endswith(".md"):
            continue
        stem = name[: -len(".md")]
        if stem.startswith("wiki-"):
            try:
                n = int(stem[5:])
            except ValueError:
                continue
            if 0 <= n <= 0xFFFFFFFF:
                used.add(n)
    n = max(used) + 1 if used else 1
    while True:
        wiki_id = f"wiki-{n:04}"
        path = os.path.join(wiki_dir, f"{wiki_id}.md")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(fd)
            return wiki_id, path
        except FileExistsError:
            n += 1


def _doc_node_id(path: str) -> str:
    """chunk id 의 path 부분 — store.rs:426-431 doc_node_id."""
    return f"doc:{path}"


def _wiki_stem(path: str | None) -> str | None:
    if path is None:
        return None
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    return stem if stem.startswith("wiki-") else None


def _confidence(raw: str) -> str:
    """Claim::confidence() — 빈 값은 unknown(frontmatter.rs:146-150)."""
    cleaned = raw.strip()
    return cleaned if cleaned else "unknown"


def _vector_literal(vec: list[float]) -> str:
    """pgvector 입력 리터럴 — search_pg._vector_literal 과 같은 형태."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _checked_vector(vec: list[float], dim: int) -> str:
    """checked_vector(store.rs:974-985) — 차원이 다륾면 엔진 메시지로 고장."""
    if len(vec) != dim:
        raise _WriteFailure(
            f"embedding dim mismatch: got {len(vec)}, expected {dim}. boring.json embed_model "
            f"must output {dim}-dim vectors (embed_dim), or change embed_dim + `make reset`."
        )
    return _vector_literal(vec)


# ── 문지방 — 그림자의 게이트 순서(엔진과 대조된 것)를 한 줄씩의 검사로 쪼갠다. ─────────


def _judge_refusal(arguments: dict[str, Any], note: RememberNote, deps: WriterDeps) -> Refused | None:
    """judge 어휘 + owner 자격 — owner 를 자처하는 호출이 토큰 없이 온 거절."""
    match parse_judge(arguments):
        case Err(reason):
            return Refused(-32602, reason)
        case Ok(judge):
            pass
    if (note.front.author == "owner" or judge == "owner") and not deps.is_owner:
        return Refused(
            -32602,
            "owner (as author or judge) needs the owner door token in x-boring-owner-token",
        )
    return None


def _owner_written_targets(cur: Any, paths: list[str]) -> list[str]:
    """대상 중 오너가 쓴 것(document.author='owner') — owner.rs:64-80 refused_supersedes."""
    if not paths:
        return []
    cur.execute(_OWNER_AUTHORED_SQL, {"paths": list(paths)})
    return sorted(row[0] for row in cur.fetchall())


def _supersedes_gate(
    arguments: dict[str, Any], deps: WriterDeps, cur: Any, seams: _TimedSeams
) -> Either[list[str], Refused]:
    """supersedes 모양·대상 — 오너 노트를 오너 아닌 호출이 대체하려 하면 사건까지 남긴다."""
    match parse_supersedes(arguments):
        case Err(reason):
            return Err(Refused(-32602, reason))
        case Ok(supersedes):
            pass
    if not deps.is_owner:
        refused = _owner_written_targets(cur, supersedes)
        if refused:
            seams.append_event(
                _OWNER_COMPONENT, OWNER_REFUSED_EVENT, "warn", door="remember", targets=refused
            )
            return Err(
                Refused(
                    -32602,
                    f"only the owner may supersede an owner-written note: {', '.join(refused)}",
                )
            )
    return Ok(supersedes)


def _pii_gate(note: RememberNote, deps: WriterDeps) -> Either[RememberNote, Refused]:
    """PII 게이트 — 규칙이 없으면 비활성으로 통과, block 이면 거절."""
    if deps.pii_scanner is None:
        return Ok(note)
    match _pii.apply_pii_gate(deps.pii_scanner, note):
        case Err(reason):
            return Err(Refused(-32603, reason))
        case Ok(gated):
            return Ok(gated)


def _gate(
    arguments: dict[str, Any], note: RememberNote, deps: WriterDeps, cur: Any, seams: _TimedSeams
) -> tuple[Refused | None, RememberNote, list[str]]:
    """자격 → supersedes 모양·대상 → PII 게이트 — shadow._gate_decision 순서 그대로.

    거절이면 (Refused, 노트, []) — 아니면 (None, 게이트를 지난 노트, 대상 목록)."""
    if refusal := _judge_refusal(arguments, note, deps):
        return (refusal, note, [])
    match _supersedes_gate(arguments, deps, cur, seams):
        case Err(refusal):
            return (refusal, note, [])
        case Ok(supersedes):
            pass
    match _pii_gate(note, deps):
        case Err(refusal):
            return (refusal, note, [])
        case Ok(note):
            pass
    return (None, note, supersedes)


def _edge_judge(author: str, judge: str | None) -> str | None:
    """supersedes 간선의 judge 칸 — Author::as_judge(frontmatter.rs:39-44): unknown 이면 None."""
    value = judge if judge is not None else author
    return None if value == "unknown" else value


def _dedup_decision_payload(
    note: RememberNote, match_: _dedup.DuplicateMatch | None, outcome: str
) -> dict[str, Any]:
    """dedup_decision 사건 본문 — mcp.rs:1878-1893 칸 그대로."""
    incoming = _dedup.note_quality(_dedup._incoming_source(note))
    existing_score = (
        None
        if match_ is None or match_.branch == _dedup.BRANCH_EMBEDDING
        else _dedup.note_quality(_dedup._existing_source(match_.existing)).score
    )
    return {
        "component": _DEDUP_COMPONENT,
        "event": DEDUP_DECISION_EVENT,
        "status": outcome,
        "reason": match_.branch if match_ is not None else None,
        "incoming_score": incoming.score,
        "existing_score": existing_score,
        "score_delta": incoming.score - existing_score if existing_score is not None else None,
        "replace_min_delta": _dedup.DUPLICATE_REPLACE_MIN_DELTA,
        "omb_session_id": note.front.omb_session_id,
        "existing_source_path": match_.source_path if match_ is not None else None,
    }


def _log_dedup(ctx: _Ctx, payload: dict[str, Any]) -> None:
    """엔진 log_dedup_decision(mcp.rs:1901)처럼 — None 칸은 사건 싱크가 빼고 기록하고,
    기록 실패는 sink 몫(거절은 아니다). 사건 기록 시간은 사건 칸에 잰다."""
    _TimedSeams(ctx.deps, ctx.timers).append_event(
        payload["component"],
        payload["event"],
        payload["status"],
        **{
            key: value
            for key, value in payload.items()
            if key not in {"component", "event", "status"} and value is not None
        },
    )


# ── 저장 — store_new_note(mcp.rs:1451-1493) + ingest_file(ingest.rs:378-490). ──────────


def _upsert_document(cur: Any, front: Any, sha: str, mtime: datetime, path: str) -> None:
    """store.rs:2083-2139 — document upsert + 프로젝트 노드/간선 + tagged 재생성."""
    cur.execute(
        _UPSERT_DOCUMENT_SQL,
        {
            "path": path,
            "origin": front.origin,
            "project": front.project,
            "kind": front.kind,
            "title": front.title,
            "tags": list(front.tags),
            "sha": sha,
            "updated_at": mtime,
            "author": front.author,
        },
    )
    doc = _doc_node_id(path)
    if front.project:
        cur.execute(
            _UPSERT_NODE_SQL,
            {
                "id": f"project:{front.project}",
                "kind": "project",
                "label": front.project,
                "outcome": None,
            },
        )
        cur.execute(
            _UPSERT_EDGE_SQL,
            {"src": doc, "dst": f"project:{front.project}", "kind": "in_project", "judge": None},
        )
    cur.execute(_DELETE_TAGGED_SQL, {"doc": doc})
    for tag in front.tags:
        cur.execute(_UPSERT_NODE_SQL, {"id": f"topic:{tag}", "kind": "topic", "label": tag, "outcome": None})
        cur.execute(_UPSERT_EDGE_SQL, {"src": doc, "dst": f"topic:{tag}", "kind": "tagged", "judge": None})


def _write_slugs(cur: Any, doc: str, kind: str, node_kind: str, items: tuple[str, ...]) -> int:
    """도구·개념 한 부류의 노드+간선 — cap 6·빈칸·한자·슬러그 중복 빼기(ingest.rs:173-205)."""
    written = 0
    seen: set[str] = set()
    for item in list(items)[: _graph.PROJECT_CAP]:
        item = item.strip()
        if not item or _graph.has_han(item):
            continue
        slug = _graph.slugify(item)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        cur.execute(
            _UPSERT_NODE_SQL, {"id": f"{node_kind}:{slug}", "kind": node_kind, "label": item, "outcome": None}
        )
        cur.execute(_UPSERT_EDGE_SQL, {"src": doc, "dst": f"{node_kind}:{slug}", "kind": kind, "judge": None})
        written += 1
    return written


def _write_tool_edges(cur: Any, doc: str, tools: tuple[str, ...], stats: _Stats) -> None:
    written = _write_slugs(cur, doc, "uses", "tool", tools)
    stats.tools += written
    stats.edges += written


def _write_concept_edges(cur: Any, doc: str, concepts: tuple[str, ...], stats: _Stats) -> None:
    written = _write_slugs(cur, doc, "about", "concept", concepts)
    stats.concepts += written
    stats.edges += written


def _upsert_claim_node(cur: Any, project: str, path: str, slot: tuple[str, str], claim: Any) -> int:
    """store.rs:1549-1593 — claim 노드·is_a·doc→claim·claim_of_project·said 간선 수."""
    subject, predicate = slot
    kind = _graph.claim_kind(claim.kind, claim.value)
    claim_id = f"claim:{subject}:{predicate}"
    cur.execute(
        _UPSERT_NODE_SQL,
        {"id": claim_id, "kind": "claim", "label": f"{predicate}: {claim.value}", "outcome": kind},
    )
    written = 1
    if kind != "fact":
        typed_id = f"{kind}:{subject}:{predicate}"
        cur.execute(
            _UPSERT_NODE_SQL,
            {
                "id": typed_id,
                "kind": kind,
                "label": f"{subject} — {claim.value}",
                "outcome": _confidence(claim.confidence),
            },
        )
        cur.execute(_UPSERT_EDGE_SQL, {"src": claim_id, "dst": typed_id, "kind": "is_a", "judge": None})
        written += 1
    doc = _doc_node_id(path)
    cur.execute(_UPSERT_EDGE_SQL, {"src": doc, "dst": claim_id, "kind": "claims", "judge": None})
    if project:
        cur.execute(
            _UPSERT_EDGE_SQL,
            {"src": claim_id, "dst": f"project:{project}", "kind": "claim_of_project", "judge": None},
        )
        written += 1
    if claim.said_by is not None:
        speaker = _graph.OWNER_SPEAKER_NODE
        cur.execute(_UPSERT_NODE_SQL, {"id": speaker, "kind": "person", "label": "owner", "outcome": None})
        cur.execute(_UPSERT_EDGE_SQL, {"src": speaker, "dst": doc, "kind": "said", "judge": None})
        cur.execute(_UPSERT_EDGE_SQL, {"src": speaker, "dst": claim_id, "kind": "said", "judge": None})
        written += 2
    return written


def _claim_slot(claim: Any) -> tuple[str, str] | None:
    """claim 하나가 실제로 쓰이는 캐논 슬롯 — 비거나 한자면 엔진이 걸러낸다(ingest.rs:220-227)."""
    subject = _graph.canon(claim.subject)
    predicate = _graph.canon(claim.predicate)
    value = claim.value.strip()
    if not subject or not predicate or not value or _graph.has_han(subject) or _graph.has_han(value):
        return None
    return (subject, predicate)


def _upsert_claim(  # noqa: PLR0913
    cur: Any, deps: WriterDeps, job: _GraphJob, claim: Any, stats: _Stats, timers: _Timers
) -> None:
    """claim 행 하나 — unchanged 프로브·임베딩(임베딩 칸에 잰다)·upsert·봉인·said_by 거울·노드/간선."""
    slot = _claim_slot(claim)
    if slot is None:
        return
    subject, predicate = slot
    value = claim.value.strip()
    kind = _graph.claim_kind(claim.kind, value)
    confidence = _confidence(claim.confidence)
    anchor = _anchor_for_claim(job.anchors, subject, value)
    anchor_db = anchor.to_db_string() if anchor is not None else None
    cur.execute(
        _CLAIM_UNCHANGED_SQL,
        {
            "subject": subject,
            "predicate": predicate,
            "path": job.path,
            "value": value,
            "kind": kind,
            "confidence": confidence,
            "anchor": anchor_db,
        },
    )
    if cur.fetchone() is None:
        started = time.monotonic()
        match deps.embed(f"{subject} {predicate} {value}"):
            case Err(reason):
                timers.embedding += time.monotonic() - started
                raise _WriteFailure(str(reason))
            case Ok(vec):
                pass
        timers.embedding += time.monotonic() - started
        cur.execute(
            _UPSERT_CLAIM_SQL,
            {
                "subject": subject,
                "predicate": predicate,
                "value": value,
                "path": job.path,
                "valid_from": job.valid_from,
                "embedding": _checked_vector(vec, deps.embed_dim),
                "kind": kind,
                "confidence": confidence,
                "anchor": anchor_db,
            },
        )
        stats.claims += 1
        if kind == "fact":
            cur.execute(_FACT_SEAL_SQL, {"subject": subject, "predicate": predicate})
            cur.execute(_FACT_UNSEAL_SQL, {"subject": subject, "predicate": predicate})
        else:
            cur.execute(_ITEM_SEAL_SQL, {"subject": subject, "predicate": predicate, "path": job.path})
            cur.execute(_ITEM_UNSEAL_SQL, {"subject": subject, "predicate": predicate, "path": job.path})
    else:
        stats.claims_unchanged += 1
    cur.execute(
        _MIRROR_SAID_BY_SQL,
        {"subject": subject, "predicate": predicate, "path": job.path, "said_by": claim.said_by},
    )
    stats.edges += _upsert_claim_node(cur, job.front.project, job.path, slot, claim)


def _extract_graph(cur: Any, deps: WriterDeps, job: _GraphJob, stats: _Stats, timers: _Timers) -> None:
    """FrontmatterGraphExtractor(ingest.rs:160-281) — 정해진 간선만 쓰고 봉인은 upsert 안에서."""
    doc = _doc_node_id(job.path)
    cur.execute(_CLEAR_SEMANTIC_SQL, {"doc": doc, "kinds": list(SEMANTIC_EDGE_KINDS)})
    _write_tool_edges(cur, doc, job.front.tools, stats)
    _write_concept_edges(cur, doc, job.front.concepts, stats)
    job = replace(job, anchors=_from_note_body(job.front.project, job.body) if job.front.project else [])
    for claim in job.front.claims:
        _upsert_claim(cur, deps, job, claim, stats, timers)


def _embed_chunks(deps: WriterDeps, pieces: list[str], timers: _Timers) -> list[tuple[str, list[float]]]:
    """chunk 임베딩 — 첫 오류에서 단축(엔진의 try_collect 과 같고, 순서는 유지)."""
    embedded: list[tuple[str, list[float]]] = []
    for piece in pieces:
        started = time.monotonic()
        match deps.embed(piece):
            case Err(reason):
                timers.embedding += time.monotonic() - started
                raise _WriteFailure(str(reason))
            case Ok(vec):
                pass
        timers.embedding += time.monotonic() - started
        embedded.append((piece, vec))
    return embedded


def _ingest(cur: Any, deps: WriterDeps, note: RememberNote, path: str, timers: _Timers) -> _Stats:
    """ingest_file(ingest.rs:378-490) — sha 비교·chunk 임베딩·document/chunk upsert·그래프."""
    with open(path, encoding="utf-8") as handle:
        data = handle.read()
    data = data.replace("\x00", "")
    sha = hashlib.sha256(data.encode("utf-8")).hexdigest()
    st = os.stat(path)
    # 뒷시각 = 파일 mtime. 엔진은 SystemTime→timestamptz 를 「잘라내기」로 바꾸니(초+subsec_micros)
    # 파이썬도 나노초를 밑으로 깎아 같은 값을 내야 sync 의 set_updated_at 이 0 변경으로 선다.
    mtime = datetime.fromtimestamp(0, tz=UTC) + timedelta(microseconds=st.st_mtime_ns // 1000)
    front = note.front
    stats = _Stats()
    cur.execute(_GET_DOC_SHA_SQL, {"path": path})
    prev = cur.fetchone()
    if prev is not None and prev[0] == sha:
        # 동일 내용 — chunk 재임베딩 없이 뒷시각만 밀고 그래프만 재건(엔진과 같은 자리).
        cur.execute(_SET_UPDATED_AT_SQL, {"path": path, "at": mtime})
        _extract_graph(cur, deps, _GraphJob(front, note.body.strip(), path, mtime), stats, timers)
        return stats
    body = note.body.strip()
    pieces = fixed_chunks(body, 1500, 200)
    if all(not piece.strip() for piece in pieces):
        stats.skipped = True
        return stats
    embedded = _embed_chunks(deps, pieces, timers)
    _upsert_document(cur, front, sha, mtime, path)
    for idx, (content, vec) in enumerate(embedded):
        cur.execute(
            _UPSERT_CHUNK_SQL,
            {
                "id": f"{path}#{idx}",
                "path": path,
                "content": content,
                "embedding": _checked_vector(vec, deps.embed_dim),
                "origin": front.origin,
                "project": front.project,
                "kind": front.kind,
                "chunk_idx": idx,
            },
        )
        stats.chunks += 1
    cur.execute(_PRUNE_CHUNKS_SQL, {"path": path, "from_idx": len(embedded)})
    _extract_graph(cur, deps, _GraphJob(front, body, path, mtime), stats, timers)
    return stats


def _record_supersedes(
    cur: Any, new_path: str, targets: list[str], judge: str | None
) -> tuple[int, int, list[str]]:
    """supersedes 간선 + 부분 닫기 — record_supersedes(store.rs:2855-2888)의 봉인만 갈라진다.

    없는 문서를 이름한 쌍은 unknown 으로 세고 간선·봉인을 안 쓴다(엔진과 같다)."""
    linked = 0
    unknown = 0
    unknown_paths: list[str] = []
    for old in targets:
        if old == new_path:
            unknown += 1
            unknown_paths.append(old)
            continue
        cur.execute(_GET_DOC_SHA_SQL, {"path": new_path})
        if cur.fetchone() is None:
            unknown += 1
            unknown_paths.append(new_path)
            continue
        cur.execute(_GET_DOC_SHA_SQL, {"path": old})
        if cur.fetchone() is None:
            unknown += 1
            unknown_paths.append(old)
            continue
        cur.execute(
            _UPSERT_EDGE_SQL,
            {"src": _doc_node_id(new_path), "dst": _doc_node_id(old), "kind": "supersedes", "judge": judge},
        )
        linked += 1
        cur.execute(_PARTIAL_SEAL_SQL, {"new": new_path, "old": old})
    return linked, unknown, unknown_paths


def _supersedes_suffix(targets: list[str], linked: int, unknown: int, unknown_paths: list[str]) -> str:
    """mcp.rs:1547-1565 supersedes_suffix — 이름한 교정이 다 이어졌는지 말해 준다."""
    if not targets:
        return ""
    if unknown == 0:
        return f" · supersedes linked {linked}"
    return f" · supersedes linked {linked}, not found {unknown} ({', '.join(unknown_paths)})"


def _record_written(ctx: _Ctx, written: Written, decision: str, branch: str | None, stats: _Stats) -> None:
    """문 사건 remember_written — 스위치를 켜도 대조가 남게, 결정·경로·수·소요 시간을 남긴다.

    칸이 전체를 설명하게 한다 — 임베딩 칸은 chunk+claim 임베딩까지, DB 칸은 연결+커밋까지,
    볼트 칸은 번호·파일 쓰기까지, 근접문서·사건 칸은 각각 nearest_document 한 건과
    append_event 들의 합이다(E3c-2)."""
    timers = ctx.timers
    started = time.monotonic()
    try:
        ctx.deps.append_event(
            "door",
            EVENT_NAME,
            "ok",
            route=ctx.request.route,
            decision=decision,
            branch=branch,
            source_path=written.source_path,
            duplicate=True if written.duplicate else None,
            supersedes=written.supersedes or None,
            unknown=written.unknown or None,
            chunks=stats.chunks or None,
            edges=stats.edges or None,
            claims=stats.claims or None,
            omb_session_id=ctx.request.omb_session_id,
            relates_to=RELATES_TO_DEFERRED,
            elapsed_total_s=round(time.monotonic() - ctx.started, 3),
            elapsed_embedding_s=round(timers.embedding, 3),
            elapsed_db_s=round(timers.db, 3),
            elapsed_vault_s=round(timers.vault, 3),
            elapsed_parse_s=round(timers.parse, 3),
            elapsed_nearest_s=round(timers.nearest, 3),
            elapsed_event_s=round(timers.event, 3),
        )
    finally:
        timers.event += time.monotonic() - started


def _skipped_answer(ctx: _Ctx, match_: _dedup.DuplicateMatch, payload: dict[str, Any]) -> Written:
    """걸러진 중복 — 아무것도 안 쓰고 엔진 skipped_duplicate 모양으로만 답한다."""
    _log_dedup(ctx, payload)
    written = Written(
        wiki_id=_wiki_stem(match_.source_path) or "",
        source_path=match_.source_path,
        message=f"skipped — duplicate of {match_.source_path}",
        duplicate=match_.source_path,
        supersedes=0,
        unknown=0,
    )
    _record_written(ctx, written, payload["status"], match_.branch, _Stats())
    return written


def _store_new_note(ctx: _Ctx, cur: Any, plan: _Plan) -> Written:
    """store_new_note(mcp.rs:1451-1493) — 번호·파일·사건·수집·간선·답."""
    deps = ctx.deps
    note = plan.note
    wiki_dir = os.path.join(deps.vault_dir, "wiki")
    try:
        cur.execute(_ALL_DOC_PATHS_SQL)
        doc_paths = [row[0] for row in cur.fetchall()]
    except Exception as failure:  # noqa: BLE001 — 엔진: "wiki id: cannot read existing document paths"
        raise _WriteFailure(f"wiki id: cannot read existing document paths: {failure}") from failure
    started = time.monotonic()
    try:
        try:
            wiki_id, path = allocate_wiki_path(wiki_dir, existing_wiki_ids(doc_paths))
        except OSError as failure:
            raise _WriteFailure(f"wiki id: {failure}") from failure
        front = replace(note.front, date=deps.clock().date().isoformat())
        content = render_wiki_note(wiki_id, front, note.body)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as failure:
            raise _WriteFailure(f"wiki note write: {failure}") from failure
    finally:
        ctx.timers.vault += time.monotonic() - started
    _log_dedup(ctx, plan.payload)
    try:
        stats = _ingest(cur, deps, replace(note, front=front), path, ctx.timers)
    except _WriteFailure as failure:
        raise _WriteFailure(f"ingest: {failure.message}") from failure
    except Exception as failure:  # noqa: BLE001 — 엔진도 수집 고장은 -32603 하나로 접는다
        raise _WriteFailure(f"ingest: {failure}") from failure
    try:
        linked, unknown, unknown_paths = _record_supersedes(cur, path, plan.targets, plan.judge)
    except Exception as failure:  # noqa: BLE001
        raise _WriteFailure(f"supersedes edges: {failure}") from failure
    stem = _wiki_stem(plan.duplicate)
    label = f"wiki/{wiki_id}.md" + (f" (supersedes wiki/{stem}.md)" if stem else "")
    message = (
        f"remembered → {label} · chunks {stats.chunks} · "
        f"graph(tools {stats.tools} concepts {stats.concepts} claims {stats.claims}) — recallable now"
    )
    message += _supersedes_suffix(plan.targets, linked, unknown, unknown_paths)
    written = Written(
        wiki_id=wiki_id,
        source_path=path,
        message=message,
        duplicate=plan.duplicate,
        supersedes=linked,
        unknown=unknown,
    )
    _record_written(ctx, written, plan.payload["status"], plan.payload.get("reason"), stats)
    return written


def _targets_for(
    supersedes: list[str], outcome: str, match_: _dedup.DuplicateMatch | None
) -> tuple[list[str], str | None]:
    """쓸 대상 목록과 duplicate 칸 — 중복 문 대체는 matched 노트 하나가 대상이다."""
    if outcome == _dedup.OUTCOME_SUPERSEDED:
        assert match_ is not None
        return [match_.source_path], match_.source_path
    return list(supersedes), None


@dataclass(frozen=True)
class _Gated:
    """게이트를 지난 한 요청 — 결정·쓰기로 넘기는 묶음."""

    note: RememberNote
    judge: str | None
    supersedes: list[str]
    indexed: tuple[tuple[str, _dedup.ExistingNote], ...] | None


def _find_duplicate(
    ctx: _Ctx, seams: _TimedSeams, gated: _Gated
) -> Either[Refused, _dedup.DuplicateMatch | None]:
    """중복 문 — 교정은 언제나 새 노트(mcp.rs:1631 needs_dedup)라 중복 문을 안 탄다."""
    if gated.supersedes:
        return Ok(None)
    deps = ctx.deps

    def nearest_timed(text: str, exclude: str | None):
        """근접 문서 프로브 한 건 전체(임베딩+읽기 전용 질의) — 근접문서 칸에 잰다."""
        started = time.monotonic()
        try:
            return deps.nearest_document(text, exclude)
        finally:
            ctx.timers.nearest += time.monotonic() - started

    match _dedup.check_duplicate(
        note=gated.note,
        vault=_dedup.VaultView(
            list_notes=seams.list_notes,
            read_note=seams.read_note,
            split_frontmatter=deps.split_frontmatter,
            vault_dir=deps.vault_dir,
            parse_note=seams.parse_note,
            parsed_entries=gated.indexed,
        ),
        nearest_document=nearest_timed,
        exclude_paths=frozenset(),
    ):
        case Err(reason):
            return Err(Refused(-32603, f"dedup check: {reason}"))
        case Ok(found):
            return Ok(found)


def _decide_and_write(ctx: _Ctx, cur: Any, seams: _TimedSeams, gated: _Gated) -> WriteOutcome:
    """중복 문 판정 → 걸러짐이면 답만, 저장·대체이면 쓰기까지 — 연결 안에서."""
    match _find_duplicate(ctx, seams, gated):
        case Err(refusal):
            return refusal
        case Ok(found):
            pass
    outcome, match_ = _dedup.dedup_gate(ctx.deps.is_owner, gated.note, found)
    payload = _dedup_decision_payload(gated.note, match_, outcome)
    if outcome == _dedup.OUTCOME_SKIPPED:
        assert match_ is not None
        return _skipped_answer(ctx, match_, payload)
    targets, duplicate = _targets_for(gated.supersedes, outcome, match_)
    plan = _Plan(
        note=gated.note,
        targets=targets,
        duplicate=duplicate,
        judge=_edge_judge(gated.note.front.author, gated.judge),
        payload=payload,
    )
    try:
        return _store_new_note(ctx, cur, plan)
    except _WriteFailure as failure:
        return Refused(-32603, failure.message)


def run_write(request: WriteRequest, deps: WriterDeps) -> WriteOutcome:
    """remember 하나를 결정·쓰고 응답 재료를 돌려준다 — 문 핸들러가 모양을 입힌다.

    게이트(자격·모양·PII) → 중복 문(걸러짐·대체·저장) → 번호·파일·사건 → 수집·그래프 →
    supersedes 간선·부분 닫기. 실패는 Refused 값 — 예상 못한 예외는 문 경계에서 접는다.
    연결+커밋 시간은 DB 칸에, 사건 기록은 사건 칸에 같이 잰다(칸의 합 = 전체)."""
    ctx = _Ctx(deps=deps, request=request, timers=_Timers(), started=time.monotonic())
    seams = _TimedSeams(deps, ctx.timers)
    arguments = request.arguments
    match parse_remember_note(arguments):
        case Err(reason):
            return Refused(-32602, reason)
        case Ok(note):
            pass
    match parse_judge(arguments):
        case Err(reason):
            return Refused(-32602, reason)
        case Ok(judge):
            pass
    connect_started = time.monotonic()
    raw_conn = deps.connect()
    ctx.timers.db += time.monotonic() - connect_started
    with _TimedConn(raw_conn, ctx.timers) as conn, conn.cursor() as raw_cur:
        cur = _TimedCursor(raw_cur, ctx.timers)
        refusal, note, supersedes = _gate(arguments, note, deps, cur, seams)
        if refusal is not None:
            return refusal
        gated = _Gated(note=note, judge=judge, supersedes=supersedes, indexed=seams.sync_index())
        outcome = _decide_and_write(ctx, cur, seams, gated)
    if isinstance(outcome, Written):
        seams.refresh_index()
    return outcome
