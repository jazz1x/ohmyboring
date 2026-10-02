"""중복 문 — drudge 의 check_duplicate·dedup_gate 를 파이썬으로 미러한다 (순수).

정본: drudge/src/serve/mcp.rs:1702-1844 (check_duplicate·pick_duplicate·note_quality·
should_replace_duplicate·dedup_gate)·:1905-1999 (has_evidence_signal·
probable_session_duplicate·token_set 계열)·owner.rs:52-59 (gated_by·may_rewrite).
supersedes 가 비어 있을 때만 돈다(교정은 언제나 새 노트로 떨어진다 — mcp.rs:1367).
파일을 고쳐 쓰는 일은 없고, 볼트는 주입받은 읽기 함수로만 본다.

갈래(dedup_decision 의 reason 어휘 그대로):
  same_session     — 같은 omb_session_id 를 가진 노트가 하나라 있을 때
  probable_session — 세션 추정: 머리말 의미 칸 겹침 ≥9/20 & (제목 Jaccard ≥1/5 또는 본문 Jaccard ≥1/5)
  exact_title      — 제목이 소문자·다듬음에서 같을 때
  embedding        — 파일 갈래가 없을 때 제목+본문 임베딩의 최근접 문서가 ≤0.07 일 때
대체(점수 판정): same_session·probable_session 갈래에서 들어오는 노트가 기존 것보다
+8 이상 좋거나(≥ 현재+8), 근거 신호를 새로 달고 더 좋으면 대체(Supersede) — 아니면 건
너뛰기(Skip). owner 가 아닌 호출은 owner 노트에 묶이지 않고(gated_by), owner 노트는
owner 만 대체할 수 있다(may_rewrite).

그림자가 쓴다 — 판정만 내고 쓰기 0. 임베딩 갈래는 주입받는 `nearest_document` 콜백에
맡긴다(문 → 로컬 임베딩 + DOOR_PG_DSN 읽기 전용 세션; 시험 → 가짜).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import yaml

from ohmyboring.remember.parse import RememberNote, _StrOnlyLoader
from ohmyboring.result import Either, Err, Ok

#: 임베딩 최근접 중복 상한 — mcp.rs:1673 DUPLICATE_MAX_DIST (코사인 거리, 이하).
DUPLICATE_MAX_DIST = 0.07

#: 세션 추정 세 칸의 문지방 — mcp.rs:1675-1677 (분자, 분모).
SESSION_DUP_TITLE_MIN = (1, 5)
SESSION_DUP_BODY_MIN = (1, 5)
SESSION_DUP_SEMANTIC_MIN = (9, 20)

#: 대체에 필요한 점수 차 — mcp.rs:1678 DUPLICATE_REPLACE_MIN_DELTA.
DUPLICATE_REPLACE_MIN_DELTA = 8

#: 갈래 이름 — dedup_decision_event 의 reason 어휘 (mcp.rs:1863).
BRANCH_SAME_SESSION = "same_session"
BRANCH_PROBABLE_SESSION = "probable_session"
BRANCH_EXACT_TITLE = "exact_title"
BRANCH_EMBEDDING = "embedding"

#: 결정 이름 — DedupOutcome::status (mcp.rs:1854).
OUTCOME_STORED = "stored"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_SKIPPED = "skipped"

#: 근거 신호 바늘 — mcp.rs:1905-1929 (소문자 부분 문자열).
_EVIDENCE_NEEDLES = (
    "## evidence",
    "## verification",
    "## result",
    "## decision",
    "## 검증",
    "## 결과",
    "## 결정",
    "as-is",
    "to-be",
    "asis",
    "tobe",
    "실제",
    "수치",
    "명령",
    "command",
    "commit",
    "pr #",
    "wiki-",
)

_WIKI_STEM_RE = re.compile(r"wiki-(\d+)$")


@dataclass(frozen=True)
class ExistingNote:
    """디스크에 있는 노트의 느슨한 머리말 뷰 — serde_yaml unwrap_or_default 규약.

    머리말이 깨져 있으면 칸은 전부 빈 값이고 본문만 살아 있다(mcp.rs:1723-1725).
    author 는 owner 여부만 게이트에 쓰인다 — vocabulary 는 parse.author 와 같다.
    """

    source_path: str
    title: str | None = None
    tags: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    claims: tuple[tuple[str, str, str], ...] = ()  # (subject, predicate, value)
    sources: tuple[str, ...] = ()
    omb_session_id: str | None = None
    author: str = "unknown"
    body: str = ""

    @classmethod
    def empty(cls, source_path: str) -> ExistingNote:
        return cls(source_path=source_path)


@dataclass(frozen=True)
class DuplicateMatch:
    """중복 판정 한 건 — DuplicateMatch { source_path, reason, front, body }."""

    source_path: str
    branch: str
    existing: ExistingNote


@dataclass(frozen=True)
class NoteQuality:
    """노트의 정보량 점수 — NoteQuality (mcp.rs:1696)."""

    score: int
    evidence_signal: bool


def token_set(text: str) -> set[str]:
    """알파벳·숫자 덩어리를 소문자 집합으로 — mcp.rs:2001 token_set (한 글자 덩어리 버림)."""
    out: set[str] = set()
    buf: list[str] = []
    for ch in text:
        if ch.isalnum():
            buf.append(ch.lower())
        elif buf:
            if len(buf) > 1:
                out.add("".join(buf))
            buf = []
    if len(buf) > 1:
        out.add("".join(buf))
    return out


def ratio_at_least(numerator: int, denominator: int, minimum: tuple[int, int]) -> bool:
    """numerator/denominator ≥ minimum.0/minimum.1 — mcp.rs:1994 (0 나눗셈은 False)."""
    if denominator == 0:
        return False
    return numerator * minimum[1] >= denominator * minimum[0]


def token_jaccard_at_least(a: str, b: str, minimum: tuple[int, int]) -> bool:
    """두 텍스트의 토큰 Jaccard 가 문지방 이상 — mcp.rs:1972."""
    sa, sb = token_set(a), token_set(b)
    if not sa or not sb:
        return False
    return ratio_at_least(len(sa & sb), len(sa | sb), minimum)


def token_overlap_min_at_least(a: str, b: str, minimum: tuple[int, int]) -> bool:
    """교집합이 작은 쪽의 문지방 이상 — mcp.rs:1983."""
    sa, sb = token_set(a), token_set(b)
    if not sa or not sb:
        return False
    return ratio_at_least(len(sa & sb), min(len(sa), len(sb)), minimum)


def has_evidence_signal(body: str) -> bool:
    """본문에 근거 신호가 있는지 — mcp.rs:1905 (소문러 바늘 부분 문자열)."""
    lower = body.lower()
    return any(needle in lower for needle in _EVIDENCE_NEEDLES)


def _semantic_text(
    tools: tuple[str, ...],
    concepts: tuple[str, ...],
    tags: tuple[str, ...],
    claims: tuple[tuple[str, str, str], ...],
) -> str:
    """머리말의 의미 칸을 한 줄로 — mcp.rs:1954 (repo/ 태그 빠짐, claims 는 세 칸)."""
    parts: list[str] = list(tools)
    parts.extend(concepts)
    parts.extend(tag for tag in tags if not tag.startswith("repo/"))
    for subject, predicate, value in claims:
        parts.extend((subject, predicate, value))
    return " ".join(parts)


@dataclass(frozen=True)
class _QualitySource:
    """점수 계산에 쓰는 칸들 — 들어오는 노트(FrontMatter)와 디스크 노트(ExistingNote)의
    공통 모양."""

    title: str
    tags: tuple[str, ...]
    tools: tuple[str, ...]
    concepts: tuple[str, ...]
    claims: int
    sources: int
    body: str


def _incoming_source(note: RememberNote) -> _QualitySource:
    front = note.front
    return _QualitySource(
        title=front.title,
        tags=front.tags,
        tools=front.tools,
        concepts=front.concepts,
        claims=len(front.claims),
        sources=len(front.sources),
        body=note.body,
    )


def _existing_source(existing: ExistingNote) -> _QualitySource:
    return _QualitySource(
        title=existing.title or "",
        tags=existing.tags,
        tools=existing.tools,
        concepts=existing.concepts,
        claims=len(existing.claims),
        sources=len(existing.sources),
        body=existing.body,
    )


def note_quality(source: _QualitySource) -> NoteQuality:
    """mcp.rs:1818 note_quality — 각 항의 상한 그대로."""
    evidence_signal = has_evidence_signal(source.body)
    heading_count = min(sum(1 for line in source.body.split("\n") if line.lstrip().startswith("#")), 8)
    repo_tag_count = min(sum(1 for tag in source.tags if not tag.startswith("repo/")), 6)
    score = (
        min(len(token_set(source.body)), 120) // 4
        + min(len(token_set(source.title)), 8)
        + min(source.claims, 8) * 8
        + min(len(source.tools), 8) * 3
        + min(len(source.concepts), 8) * 3
        + repo_tag_count * 2
        + min(source.sources, 4) * 4
        + heading_count * 4
        + (8 if evidence_signal else 0)
    )
    return NoteQuality(score=score, evidence_signal=evidence_signal)


def should_replace_duplicate(branch: str, incoming: NoteQuality, current: NoteQuality) -> bool:
    """대체 판정 — mcp.rs:1804. 세션 갈래 둘만 대체를 본다(제목·임베딩 갈래는 건 너뛴다)."""
    if branch not in (BRANCH_SAME_SESSION, BRANCH_PROBABLE_SESSION):
        return False
    return incoming.score >= current.score + DUPLICATE_REPLACE_MIN_DELTA or (
        incoming.score > current.score and incoming.evidence_signal and not current.evidence_signal
    )


def wiki_number(source_path: str) -> int | None:
    """경로 끝의 wiki 번호 — mcp.rs:1793 wiki_number."""
    match = _WIKI_STEM_RE.search(source_path.rsplit("/", 1)[-1].removesuffix(".md"))
    return int(match.group(1)) if match else None


def newest_match(matches: list[DuplicateMatch]) -> DuplicateMatch | None:
    """살아 있는 노트는 가장 번호가 큰 쪽 — mcp.rs:1787 (대첸 노트도 디스크에 남는다)."""
    if not matches:
        return None
    return max(matches, key=lambda m: wiki_number(m.source_path) or -1)


def pick_duplicate(matches: list[DuplicateMatch]) -> DuplicateMatch | None:
    """같은 세션 갈래를 먼저, 그 안에서 최신 — mcp.rs:1778 pick_duplicate."""
    same_session = [m for m in matches if m.branch == BRANCH_SAME_SESSION]
    others = [m for m in matches if m.branch != BRANCH_SAME_SESSION]
    return newest_match(same_session) or newest_match(others)


def probable_session_duplicate(note: RememberNote, existing: ExistingNote) -> bool:
    """세션 추정 — mcp.rs:1931 (양쪽 다 omb_session_id 가 있어야 하고, 의미 칸이 문지방을
    넘고 제목 또는 본문이 문지방을 넘어야 한다)."""
    target_session = note.front.omb_session_id
    if target_session is None or existing.omb_session_id is None:
        return False
    title_match = token_jaccard_at_least(note.front.title, existing.title or "", SESSION_DUP_TITLE_MIN)
    body_match = token_jaccard_at_least(note.body, existing.body, SESSION_DUP_BODY_MIN)
    semantic_match = token_overlap_min_at_least(
        _semantic_text(
            note.front.tools,
            note.front.concepts,
            note.front.tags,
            tuple((c.subject, c.predicate, c.value) for c in note.front.claims),
        ),
        _semantic_text(existing.tools, existing.concepts, existing.tags, existing.claims),
        SESSION_DUP_SEMANTIC_MIN,
    )
    return semantic_match and (title_match or body_match)


def parse_existing_note(
    source_path: str,
    text: str,
    split_frontmatter: Callable[[str], tuple[str, str] | None],
) -> ExistingNote:
    """디스크 노트를 느슨하게 — mcp.rs:1723-1725 (split/yaml 실패는 빈 머리말, 본문은 살림)."""
    split = split_frontmatter(text)
    if split is None:
        return ExistingNote.empty(source_path)
    raw_yaml, body = split
    try:
        front = yaml.load(raw_yaml, Loader=_StrOnlyLoader)
    except yaml.YAMLError:
        front = None
    if not isinstance(front, dict):
        # serde_yaml::from_str 실패는 FrontMatter::default() — 본문은 이미 갈라 놓은 것을 쓴다.
        return ExistingNote(source_path=source_path, body=body)

    def strings(key: str) -> tuple[str, ...]:
        raw = front.get(key)
        if not isinstance(raw, list):
            return ()
        return tuple(item for item in raw if isinstance(item, str))

    title = front.get("title")
    omb = front.get("omb_session_id")
    author = front.get("author")
    claims: list[tuple[str, str, str]] = []
    raw_claims = front.get("claims")
    if isinstance(raw_claims, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("subject"), str)
        and isinstance(item.get("predicate"), str)
        and isinstance(item.get("value"), str)
        for item in raw_claims
    ):
        claims = [(item["subject"], item["predicate"], item["value"]) for item in raw_claims]

    return ExistingNote(
        source_path=source_path,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        tags=strings("tags"),
        tools=strings("tools"),
        concepts=strings("concepts"),
        claims=tuple(claims),
        sources=strings("sources"),
        omb_session_id=omb.strip() if isinstance(omb, str) and omb.strip() else None,
        author=author if isinstance(author, str) and author == "owner" else "unknown",
        body=body,
    )


#: 임베딩 갈래 콜백 — (임베딩할 텍스트, 빼 둘 경로) → Ok(최근접 source_path | None) | Err(사유).
NearestDocument = Callable[[str, str | None], Either[str | None, str]]


@dataclass(frozen=True)
class VaultView:
    """그림자가 보는 볼트의 읽기 면 — 문은 디스크 읽기만 싣고, 쓰기 면은 없다.

    parsed_entries 를 싣으면 목록·읽기·파싱을 건 너고 색인이 미리 뽑아 둔 (경로,
    ExistingNote) 목록이 후보가 된다(E3b-2 — 문 안의 노트 색인). 후보가 되는 노트의
    집합과 파싱 결과는 디스크 훑기와 같아야 하니, 색인은 읽기를 줄일 뿐 판정을 바꾸지
    않는다. parse_note 는 디스크 훑기 길이가 쓰는 파서 자리 — 그림자가 시간을 재는
    껍질을 싸기 위해 주입받는다(정본은 parse_existing_note)."""

    list_notes: Callable[[], list[str]]
    read_note: Callable[[str, str], str | None]
    split_frontmatter: Callable[[str], tuple[str, str] | None]
    vault_dir: str
    parse_note: Callable[[str, str, Callable[[str], tuple[str, str] | None]], ExistingNote] = (
        parse_existing_note
    )
    parsed_entries: tuple[tuple[str, ExistingNote], ...] | None = None


def _branch_for(note: RememberNote, existing: ExistingNote) -> str | None:
    """노트 하나에 걸리는 갈래 — mcp.rs:1730-1739 (same_session → probable → exact_title)."""
    target_session = note.front.omb_session_id
    target_title = note.front.title.strip().lower()
    same_session = target_session is not None and existing.omb_session_id == target_session
    if same_session:
        return BRANCH_SAME_SESSION
    if probable_session_duplicate(note, existing):
        return BRANCH_PROBABLE_SESSION
    if target_title and (existing.title or "").strip().lower() == target_title:
        return BRANCH_EXACT_TITLE
    return None


def _disk_entries(vault: VaultView, exclude_paths: frozenset[str]) -> list[tuple[str, ExistingNote]]:
    """디스크 훑기 — 목록에서 하나씩 읽고 파싱해 후보를 모은다(mcp.rs:1718-1740 순회)."""
    entries: list[tuple[str, ExistingNote]] = []
    for note_id in vault.list_notes():
        source_path = f"/vault/wiki/{note_id}.md"
        if source_path in exclude_paths:
            continue
        text = vault.read_note(vault.vault_dir, note_id)
        if text is None:
            continue
        entries.append((source_path, vault.parse_note(source_path, text, vault.split_frontmatter)))
    return entries


def _scan_vault(note: RememberNote, vault: VaultView, exclude_paths: frozenset[str]) -> list[DuplicateMatch]:
    """파일 세 갈래 스캔 — 각 노트와 갈래를 맞춰 모은다(엔진의 read_dir 순회).

    색인이 싣는 parsed_entries 가 있으면 읽기·파싱을 건 너고 그 목록으로 본다 — 후보
    집합·파싱 결과가 디스크 훑기와 같다는 게 색인의 계약이다. exclude_paths(엔진이 방금
    쓴 새 노트)는 두 갈래 모두에서 먼저 빠진다."""
    if vault.parsed_entries is not None:
        candidates = (
            (path, existing) for path, existing in vault.parsed_entries if path not in exclude_paths
        )
    else:
        candidates = _disk_entries(vault, exclude_paths)
    matches: list[DuplicateMatch] = []
    for source_path, existing in candidates:
        if branch := _branch_for(note, existing):
            matches.append(DuplicateMatch(source_path, branch, existing))
    return matches


def _embedding_match(
    note: RememberNote,
    nearest_document: NearestDocument,
    exclude_paths: frozenset[str],
) -> Either[DuplicateMatch | None, str]:
    """임베딩 갈래 한 건 — mcp.rs:1746-1758 (제목+본문의 최근접 문서가 0.07 이하면 중복)."""
    text = f"{note.front.title}\n\n{note.body}"
    match nearest_document(text, next(iter(exclude_paths), None)):
        case Err(reason):
            return Err(f"embedding nearest: {reason}")
        case Ok(None):
            return Ok(None)
        case Ok(source_path):
            return Ok(
                DuplicateMatch(
                    source_path=source_path,
                    branch=BRANCH_EMBEDDING,
                    existing=ExistingNote.empty(source_path),
                )
            )


def check_duplicate(
    *,
    note: RememberNote,
    vault: VaultView,
    nearest_document: NearestDocument | None,
    exclude_paths: frozenset[str] = frozenset(),
) -> Either[DuplicateMatch | None, str]:
    """중복을 찾는다 — mcp.rs:1702 check_duplicate (파일 세 갈래 → 임베딩 한 갈래).

    `exclude_paths` 는 그림자만의 것: 엔진이 방금 쓴 새 노트를 후보에서 빼서 「엔진이 본
    볼트」를 재현한다(그림자는 응답 뒤에 도니 새 노트가 이미 디스크에 있다). exclude 가
    기존 노트까지 가리면 엔진 판정과 달라지니, 문은 엔진이 새로 쓴 노트 하나만 싣는다.
    """
    matches = _scan_vault(note, vault, exclude_paths)
    if found := pick_duplicate(matches):
        return Ok(found)
    if nearest_document is None:
        return Ok(None)
    return _embedding_match(note, nearest_document, exclude_paths)


def dedup_gate(
    is_owner: bool, note: RememberNote, found: DuplicateMatch | None
) -> tuple[str, DuplicateMatch | None]:
    """판정을 내린다 — mcp.rs:1422 dedup_gate (gated_by → 대체 조건 → 건 너뛰기).

    owner 가 아닌 호출은 owner 노트에 묶이지 않는다(gated_by), owner 노트는 owner 만
    대체할 수 있다(may_rewrite). embedding 갈래의 기존 노트는 author 를 모르니(빈
    머리말 취급) 누구에게나 묶이고 누구도 대체하지 못한다 — 엔진과 같다.
    """
    if found is None:
        return (OUTCOME_STORED, None)
    author = found.existing.author
    if not ((not is_owner) or author == "owner"):
        return (OUTCOME_STORED, None)
    incoming = note_quality(_incoming_source(note))
    current = note_quality(_existing_source(found.existing))
    may_rewrite = is_owner or author != "owner"
    if should_replace_duplicate(found.branch, incoming, current) and may_rewrite:
        return (OUTCOME_SUPERSEDED, found)
    return (OUTCOME_SKIPPED, found)
