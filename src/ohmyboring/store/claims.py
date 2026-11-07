"""claim 읽기 — pg 질의 하나하나의 Surreal 짝(엔진 claim_reads.rs 를 조건 그대로 옮김).

읽는 표는 `claim_row`(열린 행은 `superseded_us = NONE`)와 작성자를 위한 `document`. pg 가 join·group·"행당 첫 줄"로
하던 선택은 여기서 파이썬이 한다 — 정렬 키가 같은 행끼리는 어느 쪽도 순서가 정해져 있지 않다. 반환 행의 모양은
pg 쪽 `fetchall()` 과 같아서 `claim_source.pick_current` · `rules.group_rules` 가 그대로 받는다.
`value` 는 표 별칭 없이 맨 이름으로 쓴다(엔진 register_select 와 같은 꼴 — `c.value` 는 라이브에서 시험한 적 없음).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ohmyboring.result import Either, Err, Ok
from ohmyboring.store import surreal
from ohmyboring.store.surreal import SurrealClient, SurrealError

SAMPLE_LIMIT = 5
SAMPLE_VALUE_CHARS = 160
UNKNOWN_AUTHOR = "unknown"


def _first(client: SurrealClient, sql: str) -> Either[list[dict[str, Any]], SurrealError]:
    match client.query(sql):
        case Err() as failed:
            return failed
        case Ok(statements):
            return Ok(statements[0] if statements else [])


def authors_of(client: SurrealClient, paths: list[str]) -> Either[dict[str, str], SurrealError]:
    """노트별 작성자. document 사본에 없는 노트는 키가 없다(부르는 쪽이 `unknown` 으로 읽는다 — pg left join+coalesce)."""
    found: dict[str, str] = {}
    for batch in surreal.batches(sorted(set(paths))):
        match _first(
            client, f"SELECT source_path, author FROM document WHERE source_path IN {surreal.texts(batch)};"
        ):
            case Err() as failed:
                return failed
            case Ok(rows):
                found.update({r["source_path"]: r["author"] for r in rows})
    return Ok(found)


def _with_authors(client: SurrealClient, rows: list[dict[str, Any]]) -> Either[dict[str, str], SurrealError]:
    return authors_of(client, [r["source_path"] for r in rows])


def claim_source_rows(
    client: SurrealClient, subject: str
) -> Either[list[tuple[str, datetime, str, str]], SurrealError]:
    """`claim_source.SQL` 의 짝: (source_path, valid_from, value, author) — 한 주어의 현재 claim."""
    sql = (
        "SELECT subject, value, source_path, valid_from_us FROM claim_row "
        f"WHERE subject = {surreal.text(subject)} AND superseded_us = NONE "
        "ORDER BY valid_from_us DESC, source_path ASC;"
    )
    match _first(client, sql):
        case Err() as failed:
            return failed
        case Ok(rows):
            match _with_authors(client, rows):
                case Err() as failed:
                    return failed
                case Ok(authors):
                    return Ok(
                        [
                            (
                                r["source_path"],
                                surreal.micros_to_time(r["valid_from_us"]),
                                r["value"],
                                authors.get(r["source_path"], UNKNOWN_AUTHOR),
                            )
                            for r in rows
                        ]
                    )


def claim_source_list_rows(
    client: SurrealClient, predicate: str
) -> Either[list[tuple[str, str, datetime, str, str]], SurrealError]:
    """`claim_source.LIST_SQL` 의 짝: (subject, source_path, valid_from, value, author) — 한 술어의 현재 claim."""
    sql = (
        "SELECT subject, value, source_path, valid_from_us FROM claim_row "
        f"WHERE predicate = {surreal.text(predicate)} AND superseded_us = NONE "
        "ORDER BY valid_from_us DESC, source_path ASC, subject ASC;"
    )
    match _first(client, sql):
        case Err() as failed:
            return failed
        case Ok(rows):
            match _with_authors(client, rows):
                case Err() as failed:
                    return failed
                case Ok(authors):
                    return Ok(
                        [
                            (
                                r["subject"],
                                r["source_path"],
                                surreal.micros_to_time(r["valid_from_us"]),
                                r["value"],
                                authors.get(r["source_path"], UNKNOWN_AUTHOR),
                            )
                            for r in rows
                        ]
                    )


def rule_rows(client: SurrealClient, kind: str) -> Either[list[tuple[str, str, str, str]], SurrealError]:
    """`rules.SQL` 의 짝: (subject, predicate, value, source_path) — kind 의 현재 claim, 주어별로 묶여 최신 먼저."""
    sql = (
        "SELECT subject, predicate, value, source_path, valid_from_us FROM claim_row "
        f"WHERE kind = {surreal.text(kind)} AND superseded_us = NONE "
        "ORDER BY valid_from_us DESC, predicate ASC, source_path ASC;"
    )
    match _first(client, sql):
        case Err() as failed:
            return failed
        case Ok(rows):
            # 안정 정렬: 주어 안에서 최신 먼저 순서가 남는다. 주어 순서는 group_rules 가 다시 정렬한다.
            ordered = sorted(rows, key=lambda r: r["subject"])
            return Ok([(r["subject"], r["predicate"], r["value"], r["source_path"]) for r in ordered])


def split_subject_rows(client: SurrealClient) -> Either[list[tuple[str, str]], SurrealError]:
    """`door._SPLIT_SUBJECTS_SQL` 의 짝: 현재 claim 마다 (subject, source_path)."""
    match _first(client, "SELECT subject, source_path FROM claim_row WHERE superseded_us = NONE;"):
        case Err() as failed:
            return failed
        case Ok(rows):
            return Ok([(r["subject"], r["source_path"]) for r in rows])


def split_samples(
    client: SurrealClient, variants: list[str]
) -> Either[list[tuple[str, int, str]], SurrealError]:
    """`door._SPLIT_SAMPLES_SQL` 의 짝: 술어별 (술어, 행 수, 가장 긴 값의 앞 160자). 행 많은 순, 5개.
    가장 긴 값이 같으면 코드포인트 순 첫 값(pg 는 열 collation 순 — 라이브 대조 전 미확인)."""
    sql = (
        f"SELECT predicate, value FROM claim_row WHERE subject IN {surreal.texts(variants)} "
        "AND superseded_us = NONE;"
    )
    match _first(client, sql):
        case Err() as failed:
            return failed
        case Ok(rows):
            by_predicate: dict[str, tuple[int, str]] = {}
            for r in rows:
                count, best = by_predicate.get(r["predicate"], (0, ""))
                value = r["value"]
                if _longer(value, best, count):
                    best = value
                by_predicate[r["predicate"]] = (count + 1, best)
            ranked = sorted(by_predicate.items(), key=lambda kv: (-kv[1][0], kv[0]))[:SAMPLE_LIMIT]
            return Ok([(p, n, v[:SAMPLE_VALUE_CHARS]) for p, (n, v) in ranked])


def _longer(value: str, best: str, seen: int) -> bool:
    """첫 행이면 무조건 채택, 아니면 더 길거나 같은 길이에서 코드포인트가 앞서면 채택."""
    if seen == 0:
        return True
    return len(value) > len(best) or (len(value) == len(best) and value < best)
