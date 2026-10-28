"""그래프 규칙 — 슬러그·canon·한자·claim kind 와 대체 봉인의 목표 상태 (E3b).

포트 출처 — drudge/src/ingest.rs(슬러그·canon·한자), frontmatter.rs(kind 기본값·WORK_DENIALS).
순수 계산이라 그래프·DB 에 쓰는 일이 없다.

대체 봉인만 엔진과 일부러 다르다. 엔진(store.rs:2898 seal_superseded_claims)은 옛 노트의
현재 claim 을 통째로 봉인하지만, 여기서 정하는 목표는 부분 닫기다 — 새 노트가 다시 말한
(subject, predicate) 슬롯만 닫고, 새 노트가 말하지 않은 옛 사실은 산다(판정 wiki-2855,
의도한 차이).
"""

from __future__ import annotations

from dataclasses import dataclass

#: 한 노트의 tool/concept 투영 상한 — ingest.rs:150 (FrontmatterGraphExtractor::new 의 cap 6).
PROJECT_CAP = 6

#: said_by 가 낼 수 있는 화자 — SaidBy enum 이 Owner 하나뿐이라(frontmatter.rs:88-92)
#: 화자 노드도 이 하나다(store.rs:344 OWNER_SPEAKER_NODE).
OWNER_SPEAKER_NODE = "person:owner"

#: claim 의 value 가 「할 일이 없음」을 뜻할 때 next/blocked 를 fact 로 강등하는 어휘
#: (frontmatter.rs:152-172 WORK_DENIALS).
WORK_DENIALS = frozenset(
    (
        "none",
        "nothing",
        "n/a",
        "na",
        "not applicable",
        "no",
        "-",
        "--",
        "없음",
        "없다",
        "없습니다",
        "없어요",
        "해당 없음",
        "해당없음",
        "남은 작업 없음",
        "남은 작업이 없음",
        "なし",
        "無し",
        "該当なし",
    )
)


@dataclass(frozen=True)
class ClaimRow:
    """claim 테이블 한 행의 대조용 뷰 — 슬롯은 이미 canon 형태(DB 가 그렇게 쓴다)."""

    source_path: str
    subject: str
    predicate: str
    kind: str
    sealed: bool  # superseded_at IS NOT NULL


def has_han(text: str) -> bool:
    """한자(U+4E00..U+9FFF) 하나라도 있으면 참 — 모형이 중국어로 새어 나온 흔적을 걸러낸다
    (ingest.rs:319-321). 한글은 한자가 아니라 통과한다."""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def slugify(text: str) -> str:
    """소문자로 + 별칭(c++→cpp, c#→csharp, .net→dotnet) + [a-z0-9] 만 남긴다
    (ingest.rs:325-335). 구분자를 다 지워 철자 변형이 노드를 갈라놓는 일이 없게 한다."""
    normalized = text.lower().replace("c++", "cpp").replace("c#", "csharp").replace(".net", "dotnet")
    return "".join(ch for ch in normalized if ch.isascii() and ch.isalnum())


def canon(text: str) -> str:
    """claim 슬롯의 표기 정규화 — 소문자, 공백/_/- 뭉치를 하나의 - 로 접는다
    (ingest.rs:340-356). 「foo bar」와 「foo-bar」가 다른 슬롯이 되는 일이 없게 한다."""
    out: list[str] = []
    prev_sep = False
    for ch in text.lower():
        if ch.isspace() or ch in "_-":
            prev_sep = True
        else:
            if prev_sep and out:
                out.append("-")
            prev_sep = False
            out.append(ch)
    return "".join(out)


#: Rust 의 char::is_whitespace(White_Space 코드포인트)를 풀어 쓴 클래스 — `\s` 는 서버 로캘을 따른다.
_CANON_SEP = r"[\t-\r \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_-]+"


def canon_sql(column: str) -> str:
    """canon 의 SQL 짝 (store.rs canon_sql). lower() 는 İ·끝 시그마 특수 대소문자에서만 canon 과 다르다."""
    return f"btrim(regexp_replace(lower({column}), '{_CANON_SEP}', '-', 'g'), '-')"


def is_canon_sql(alias: str) -> str:
    """저장된 subject·predicate 가 이미 canon 철자인 행 — canon 칸 안에서 옛 철자 행보다 앞선다."""
    return f"({alias}.subject = {canon_sql(f'{alias}.subject')} AND {alias}.predicate = {canon_sql(f'{alias}.predicate')})"


def denies_work(value: str) -> bool:
    """value 가 「할 일 없음」 계열인지 — frontmatter.rs:174-181."""
    v = value.strip().rstrip(".。").strip().lower()
    return v in WORK_DENIALS


def claim_kind(raw_kind: str, value: str) -> str:
    """Claim::kind() 그대로 (frontmatter.rs:135-144) — 빈 kind 는 fact, next/blocked 가
    작업 부정의 값을 쥐면 fact 로 강등."""
    kind = raw_kind.strip()
    if not kind:
        return "fact"
    if kind in ("next", "blocked") and denies_work(value):
        return "fact"
    return kind


def expected_seal_states(
    claims: tuple[ClaimRow, ...],
    new_path: str,
    new_slots: frozenset[tuple[str, str]],
    superseded_paths: frozenset[str],
) -> list[tuple[ClaimRow, bool]]:
    """각 claim 행의 목표 봉인 상태 — 부분 닫기 의도.

    새 노트가 쓴 행은 산다. 대첻된 옛 노트의 행은 새 노트가 다시 말한 슬롯만 닫힌다 —
    엔진의 통째 봉인(store.rs:2898)과의 어긋남이 여기서 나오고, 그림자는 그걸 사유 (나)로
    남긴다. 두 노트 모두 아닌 행(함께 조회된 다른 대상 등)은 대상이 아니라 비교에 안 낸다.
    """
    out: list[tuple[ClaimRow, bool]] = []
    for row in claims:
        if row.source_path == new_path:
            out.append((row, False))
        elif row.source_path in superseded_paths:
            out.append((row, (row.subject, row.predicate) in new_slots))
    return out
