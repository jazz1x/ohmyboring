"""조각 열쇠 등록부 — slice/<계약> 칸의 run claim 이력 전체를 판마다 n·a→b 로 읽는다 (C-SLICEKEY1 1단계).

쓰는 자리는 허가·판정 노트의 claim 한 줄(subject=slice/<계약>, predicate=run,
value=`end=<a>/<전체> ledger=<줄>` | `end=unmeasured ledger=<줄>` | `end=<전체>/<전체> closed`)이고
여기는 읽기뿐이다. n 은 아무도 안 쓴다 — 이력 안의 1-based 위치다. 장부(~/.claude/heavy-approvals.tsv)는
컨테이너 밖 호스트 파일이라 읽지 않는다: 부르는 쪽이 센 줄 범위를 ledger_from·ledger_to 로 받는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from ohmyboring.registers.pg import Rejected, _attempt, _exec, rfc3339
from ohmyboring.result import Either, Err, Ok
from ohmyboring.search.pg import PgError

KEY_PREFIX = "slice/"
RUN_PREDICATE = "run"
NOT_YET = "아직"
MAX_LEDGER_SPAN = 10_000

TOOL = {
    "name": "slice_history",
    "description": (
        "Slice run history for one key (subject slice/<contract name lowercase>, predicate run): every run "
        "claim in valid_from order, superseded values included, with n (1-based position), a, b (= next "
        "run's a; the last run is '아직'), closed and ledger line. 'unmeasured' stays unmeasured, never 0. "
        "Also returns (가) ledger lines without any slice claim / ledger lines in range, from the range "
        "you count on the host (ledger_from, ledger_to; both or neither - missing range is 'unmeasured'), "
        "and (나) slice/* keys with exactly one run (typo detector)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Subject, e.g. slice/c-514b."},
            "ledger_from": {"type": "integer", "description": "First ledger line you count (host file)."},
            "ledger_to": {"type": "integer", "description": "Last ledger line you count (host file)."},
        },
        "required": ["key"],
    },
}

_RUNS_SQL = (
    "SELECT c.subject, c.value, c.valid_from, c.superseded_at IS NOT NULL AS superseded, c.source_path"
    " FROM claim c"
    " JOIN document d ON d.source_path = c.source_path"
    " WHERE c.predicate = %(predicate)s"
    " AND starts_with(c.subject, %(prefix)s)"
    " AND NOT (d.origin = ANY(%(exclude_origins)s))"
    ' ORDER BY c.subject COLLATE "C", c.valid_from ASC, c.source_path COLLATE "C"'
)


@dataclass(frozen=True)
class Measured:
    done: int
    total: int


@dataclass(frozen=True)
class Unmeasured:
    pass


@dataclass(frozen=True)
class Unparsed:
    pass


End = Measured | Unmeasured | Unparsed


@dataclass(frozen=True)
class Run:
    subject: str
    end: End
    closed: bool
    ledger: int | None
    valid_from: datetime
    superseded: bool
    source_path: str
    raw: str


@dataclass(frozen=True)
class SliceArgs:
    key: str
    ledger_range: tuple[int, int] | None


_END_RE = re.compile(r"^end=(?:(\d+)/(\d+)|(unmeasured))$")
_LEDGER_RE = re.compile(r"^ledger=(\d+)$")


def _end_of(token: str) -> End:
    m = _END_RE.match(token)
    if m is None:
        return Unparsed()
    if m.group(3):
        return Unmeasured()
    return Measured(int(m.group(1)), int(m.group(2)))


def run_of(row: tuple) -> Run:
    subject, value, valid_from, superseded, source_path = row
    tokens = value.split()
    ledgers = [m.group(1) for t in tokens if (m := _LEDGER_RE.match(t))]
    end = next((_end_of(t) for t in tokens if t.startswith("end=")), Unparsed())
    return Run(
        subject=subject,
        end=end,
        closed="closed" in tokens,
        ledger=int(ledgers[0]) if ledgers else None,
        valid_from=valid_from,
        superseded=superseded,
        source_path=source_path,
        raw=value,
    )


def parse_args(arguments: Any) -> Either[SliceArgs, Rejected]:
    args = arguments if isinstance(arguments, dict) else {}
    key = args.get("key")
    if not isinstance(key, str) or not key.strip():
        return Err(Rejected("missing argument: key"))
    lo, hi = args.get("ledger_from"), args.get("ledger_to")
    if lo is None and hi is None:
        return Ok(SliceArgs(key.strip(), None))
    is_line = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 1  # noqa: E731
    if not (is_line(lo) and is_line(hi)) or lo > hi:
        return Err(Rejected("ledger_from and ledger_to must be integers with 1 <= ledger_from <= ledger_to"))
    if hi - lo + 1 > MAX_LEDGER_SPAN:
        return Err(Rejected(f"ledger range spans more than {MAX_LEDGER_SPAN} lines"))
    return Ok(SliceArgs(key.strip(), (lo, hi)))


def _a_of(end: End) -> int | str:
    match end:
        case Measured(done, _):
            return done
        case Unmeasured():
            return "unmeasured"
        case Unparsed():
            return "unparsed"


def _total_of(end: End) -> int | str:
    match end:
        case Measured(_, total):
            return total
        case Unmeasured():
            return "unmeasured"
        case Unparsed():
            return "unparsed"


def history_of(runs: list[Run]) -> list[dict[str, Any]]:
    """한 열쇠의 이력 → 판마다 n·a·b·closed·ledger. b 는 다음 판의 a, 마지막 판은 「아직」."""
    out = []
    for i, run in enumerate(runs):
        out.append(
            {
                "n": i + 1,
                "a": _a_of(run.end),
                "total": _total_of(run.end),
                "b": _a_of(runs[i + 1].end) if i + 1 < len(runs) else NOT_YET,
                "closed": run.closed,
                "ledger": run.ledger,
                "valid_from": rfc3339(run.valid_from),
                "superseded": run.superseded,
                "source_path": run.source_path,
                "value": run.raw,
            }
        )
    return out


def ledger_gap_of(runs: list[Run], ledger_range: tuple[int, int] | None) -> dict[str, Any]:
    """(가) 범위 안에서 어떤 slice run claim 도 안 인용한 장부 줄 수 / 범위 안 줄 수. 범위가 없으면 못 잰 것."""
    if ledger_range is None:
        return {
            "uncited": "unmeasured",
            "in_range": "unmeasured",
            "reason": "ledger_from/ledger_to not given",
        }
    lo, hi = ledger_range
    cited = {r.ledger for r in runs if r.ledger is not None}
    missing = [n for n in range(lo, hi + 1) if n not in cited]
    return {"uncited": len(missing), "in_range": hi - lo + 1, "uncited_lines": missing}


def single_run_keys_of(runs: list[Run]) -> list[str]:
    counts: dict[str, int] = {}
    for run in runs:
        counts[run.subject] = counts.get(run.subject, 0) + 1
    return sorted(k for k, n in counts.items() if n == 1)


def payload_of(args: SliceArgs, runs: list[Run]) -> dict[str, Any]:
    own = [r for r in runs if r.subject == args.key]
    return {
        "key": args.key,
        "runs": history_of(own),
        "ledger_without_slice_claim": ledger_gap_of(runs, args.ledger_range),
        "single_run_keys": single_run_keys_of(runs),
    }


def all_runs(conn: psycopg.Connection, exclude_origins: tuple[str, ...]) -> Either[list[Run], PgError]:
    params = {"predicate": RUN_PREDICATE, "prefix": KEY_PREFIX, "exclude_origins": list(exclude_origins)}
    match _attempt(lambda: _exec(conn, _RUNS_SQL, params)):
        case Err(e):
            return Err(e)
        case Ok(rows):
            return Ok([run_of(row) for row in rows])


def answer(conn: psycopg.Connection, args: SliceArgs, exclude_origins: tuple[str, ...]) -> Either[dict, str]:
    match all_runs(conn, exclude_origins):
        case Err(e):
            return Err(str(e))
        case Ok(runs):
            return Ok(payload_of(args, runs))
