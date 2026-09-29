"""결과 타입 한 벌 — 실패를 예외 대신 값으로 흐르게 한다.

omcr 의 src/omcr/result.py 와 같은 이름·같은 의미: Ok 가 값을, Err 가 실패를 담고,
bind·map_ok 로 계산을 잇는다. 갈래는 부르는 쪽에서 match 한 자리에서 소진한다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
U = TypeVar("U")
E = TypeVar("E")


@dataclass(frozen=True)
class Ok(Generic[T]):
    """성공한 계산의 값."""

    value: T


@dataclass(frozen=True)
class Err(Generic[E]):
    """실패한 계산의 실패 값 — 예외가 아니라 데이터."""

    error: E


#: 계산의 결과 — Ok[값] 이거나 Err[실패]. 공개 함수는 Either 를 돌려주고 부르는 쪽이 접는다.
Either = Ok[T] | Err[E]


def bind(result: Either[T, E], f: Callable[[T], Either[U, E]]) -> Either[U, E]:
    """Ok 면 그 값으로 f 를 잇고, Err 면 f 를 걸치지 않고 그대로 건다."""
    match result:
        case Ok(value):
            return f(value)
        case Err(error):
            return Err(error)


def map_ok(result: Either[T, E], f: Callable[[T], U]) -> Either[U, E]:
    """Ok 안의 값만 f 로 바꾼다. Err 는 그대로 돌아온다."""
    match result:
        case Ok(value):
            return Ok(f(value))
        case Err(error):
            return Err(error)
