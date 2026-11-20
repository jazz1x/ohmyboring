"""파이썬 쪽 Surreal 읽기 문 — 엔진(drudge/src/store/vector_surreal.rs)과 같은 와이어.

`POST /signin` 으로 토큰을 받고 `POST /rpc`(method `query`)로 SurrealQL 을 보낸다. 값은 바인드 변수가
아니라 리터럴로 질의문에 넣는다: 서버가 레코드 id·날짜처럼 보이는 문자열을 그 타입으로 읽기 때문이다
(엔진 `run` 의 주석). 설정은 boring.json `vector.surreal` + env 를 경계에서 한 번 읽어 `Backend` 로 접는다.
실패는 예외가 아니라 `Err(SurrealError)` 로 흐르고, pg 로 되돌아가는 길은 없다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ohmyboring.result import Either, Err, Ok

DEFAULT_URL = "http://localhost:8000"
DEFAULT_NAME = "boring"
DEFAULT_USER = "root"
DEFAULT_PASSWORD_ENV = "BORING_SURREAL_PASSWORD"
URL_ENV = "BORING_SURREAL_URL"
#: 엔진 REQUEST_TIMEOUT 와 같은 경계 타임아웃 — 무한 대기 방지(네트워크 I/O 경계).
REQUEST_TIMEOUT_SECONDS = 30.0
#: `IN [...]` 한 번에 싣는 값 수 — 본문 1 MiB 를 넘으면 서버가 413 으로 거절한다(엔진 IN_BATCH).
IN_BATCH = 500
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class SurrealError:
    message: str


@dataclass(frozen=True)
class SurrealSettings:
    url: str
    namespace: str
    database: str
    user: str
    password: str

    def __repr__(self) -> str:  # 비밀번호는 로그에 나가면 안 된다
        return f"SurrealSettings(url={self.url!r}, namespace={self.namespace!r}, database={self.database!r})"


@dataclass(frozen=True)
class OnPg:
    """vector.backend 가 surreal 이 아님 — 문은 pg 를 읽는다."""


@dataclass(frozen=True)
class OnSurreal:
    settings: SurrealSettings


Backend = OnPg | OnSurreal


def parse_backend(cfg: Mapping[str, Any], env: Mapping[str, str]) -> Either[Backend, SurrealError]:
    """boring.json + env → Backend. surreal 이 아닌 값은 모두 pg(엔진도 그렇게 읽는다). surreal 인데 이름·비밀번호가
    틀리면 pg 로 떨어지지 않고 Err — 비밀번호 없음은 기본값이 아니라 오류다(config.rs `settings`)."""
    vector = cfg.get("vector") or {}
    if vector.get("backend") != "surreal":
        return Ok(OnPg())
    given = vector.get("surreal") or {}
    namespace = given.get("namespace", DEFAULT_NAME)
    database = given.get("database", DEFAULT_NAME)
    for label, name in (("namespace", namespace), ("database", database)):
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return Err(SurrealError(f"vector.surreal.{label} 는 [A-Za-z0-9_-]+ : {name!r}"))
    password_env = given.get("password_env", DEFAULT_PASSWORD_ENV)
    password = env.get(password_env) if isinstance(password_env, str) else None
    if not password:
        return Err(SurrealError(f"vector.surreal: 비밀번호 env {password_env} 가 비어 있음"))
    return Ok(
        OnSurreal(
            SurrealSettings(
                url=env.get(URL_ENV) or given.get("url", DEFAULT_URL),
                namespace=namespace,
                database=database,
                user=given.get("user", DEFAULT_USER),
                password=password,
            )
        )
    )


def text(value: str) -> str:
    """SurrealQL 문자열 리터럴 — 엔진 `text()` 와 같은 이스케이프(따옴표·역슬래시·제어문자)."""
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def texts(values: list[str]) -> str:
    return "[" + ",".join(text(v) for v in values) + "]"


def batches(values: list[str]) -> list[list[str]]:
    return [values[i : i + IN_BATCH] for i in range(0, len(values), IN_BATCH)]


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def micros_to_time(us: int) -> datetime:
    """`*_us`(epoch 마이크로초) → 시각. pg timestamptz 를 UTC 세션으로 읽은 것과 같은 값."""
    return _EPOCH + timedelta(microseconds=us)


class SurrealClient:
    """한 설정에 묶인 동기 HTTP 클라이언트. 토큰은 한 번 받아 쓰고 401 이면 한 번만 다시 받는다."""

    def __init__(self, settings: SurrealSettings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._http = httpx.Client(
            base_url=settings.url.rstrip("/"), timeout=REQUEST_TIMEOUT_SECONDS, transport=transport
        )
        self._token: str | None = None

    def _sign_in(self) -> Either[str, SurrealError]:
        try:
            response = self._http.post(
                "/signin",
                headers={"Accept": "application/json"},
                json={"user": self._settings.user, "pass": self._settings.password},
            )
        except httpx.HTTPError as e:
            return Err(SurrealError(f"surreal signin request: {e}"))
        token = _json_field(response, "token")
        if response.is_success and isinstance(token, str):
            self._token = token
            return Ok(token)
        return Err(SurrealError(f"surreal signin (http {response.status_code}): {response.text[:200]}"))

    def _rpc(self, token: str, sql: str) -> Either[httpx.Response, SurrealError]:
        try:
            return Ok(
                self._http.post(
                    "/rpc",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Surreal-NS": self._settings.namespace,
                        "Surreal-DB": self._settings.database,
                        "Accept": "application/json",
                    },
                    json={"id": 1, "method": "query", "params": [sql]},
                )
            )
        except httpx.HTTPError as e:
            return Err(SurrealError(f"surreal request: {e}"))

    def _send(self, sql: str) -> Either[httpx.Response, SurrealError]:
        token = Ok(self._token) if self._token else self._sign_in()
        match token:
            case Err():
                return token
            case Ok(t):
                first = self._rpc(t, sql)
        match first:
            case Ok(response) if response.status_code == 401:
                match self._sign_in():
                    case Err() as failed:
                        return failed
                    case Ok(fresh):
                        return self._rpc(fresh, sql)
            case _:
                return first

    def query(self, sql: str) -> Either[list[list[dict[str, Any]]], SurrealError]:
        """SurrealQL 을 보내 문장마다 행 목록 하나를 돌려준다. OK 가 아닌 문장이 하나라도 있으면 통째로 Err."""
        match self._send(sql):
            case Err() as failed:
                return failed
            case Ok(response):
                return _statements(response)


def _json_field(response: httpx.Response, key: str) -> Any:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get(key) if isinstance(body, dict) else None


def _statements(response: httpx.Response) -> Either[list[list[dict[str, Any]]], SurrealError]:
    try:
        body = response.json()
    except ValueError:
        return Err(SurrealError(f"surreal reply (http {response.status_code}) is not json"))
    if not isinstance(body, dict):
        return Err(SurrealError(f"surreal reply (http {response.status_code}) is not an rpc reply"))
    if (error := body.get("error")) is not None:
        message = error.get("message") if isinstance(error, dict) else error
        return Err(SurrealError(f"surreal: {message}"))
    statements = body.get("result")
    if not isinstance(statements, list):
        return Err(SurrealError(f"surreal reply (http {response.status_code}) has neither result nor error"))
    out: list[list[dict[str, Any]]] = []
    for s in statements:
        if s.get("status") != "OK":
            return Err(SurrealError(f"surreal statement {s.get('status')}: {s.get('result')}"))
        rows = s.get("result")
        out.append(rows if isinstance(rows, list) else [])
    return Ok(out)
