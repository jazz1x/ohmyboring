//! POST /search — 문(agents/door/door.py `_search`, `_SearchBody`)과 같은 몸 파싱 400, 503,
//! handover 502 와 query_log 순서, 성공 헤더. 기준은 Python 출력이다.

#![allow(dead_code)]

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::Serialize;

use crate::serve::{SearchHit, SearchReq, SearchResp};
use crate::store::LoggedHit;

#[derive(Serialize)]
struct ErrorBody<'a> {
    error: &'a str,
}

/// 오류 응답 몸 — `{"error": …}` 한 키.
pub(crate) fn error_json(message: &str) -> String {
    serde_json::to_string(&ErrorBody { error: message }).unwrap_or_default()
}

pub(crate) fn error_response(status: StatusCode, message: &str) -> Response {
    (
        status,
        [(axum::http::header::CONTENT_TYPE, "application/json")],
        error_json(message),
    )
        .into_response()
}

/// 검색 흐름이 접히는 두 실패. 둘 다 502.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum SearchFlowError {
    Search(String),
    Handover(String),
}

impl SearchFlowError {
    pub(crate) fn message(&self) -> String {
        match self {
            Self::Search(detail) => format!("search failed: {detail}"),
            Self::Handover(detail) => format!("handover failed: {detail}"),
        }
    }
}

/// query_log 한 줄 — hit 마다 한 줄, 첫 snippet 이 답 미리보기.
pub(crate) struct LogRow {
    pub(crate) hits: Vec<LoggedHit>,
    pub(crate) answer_snippet: String,
}

pub(crate) fn log_row(_hits: &[SearchHit]) -> LogRow {
    LogRow {
        hits: Vec::new(),
        answer_snippet: String::new(),
    }
}

pub(crate) fn parse_search_body(_body: &[u8]) -> Result<SearchReq, String> {
    Err(String::new())
}

pub(crate) async fn search_flow<S, H, HFut, L, LFut>(
    _session: Option<&str>,
    search: S,
    _handover: H,
    _log: L,
) -> Result<Vec<SearchHit>, SearchFlowError>
where
    S: std::future::Future<Output = Result<Vec<SearchHit>, String>>,
    H: FnOnce(String, Vec<String>) -> HFut,
    HFut: std::future::Future<Output = Result<(), String>>,
    L: FnOnce(LogRow) -> LFut,
    LFut: std::future::Future<Output = ()>,
{
    search.await.map_err(SearchFlowError::Search)
}

pub(crate) fn search_ok_response(hits: Vec<SearchHit>) -> Response {
    Json(SearchResp { hits }).into_response()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;
    use std::cell::RefCell;

    // 기대값: 문 `_search` + pydantic 2.14.1 (이 저장소 .venv 설치본) + Starlette JSONResponse,
    // 2026-10-28 에 TestClient 로 받은 몸. 컨테이너의 pydantic 버전은 미확인(URL 의 2.14 부분).
    const BAD_BODIES: &[(&str, &str)] = &[
        (r"{", r#"{"error":"body must be JSON"}"#),
        (r"", r#"{"error":"body must be JSON"}"#),
        (r"   ", r#"{"error":"body must be JSON"}"#),
        (r"[]", r#"{"error":"body must be a JSON object"}"#),
        (r"null", r#"{"error":"body must be a JSON object"}"#),
        (r#""x""#, r#"{"error":"body must be a JSON object"}"#),
        (r"1", r#"{"error":"body must be a JSON object"}"#),
        (r"{} x", r#"{"error":"body must be JSON"}"#),
        (
            r"{}",
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Field required [type=missing, input_value={}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/missing"}"#,
        ),
        (
            r#"{"query":""}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Value error, query is required [type=value_error, input_value='', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/value_error"}"#,
        ),
        (
            r#"{"query":"  "}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Value error, query is required [type=value_error, input_value='  ', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/value_error"}"#,
        ),
        (
            r#"{"query":"\u001c\u00a0\n"}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Value error, query is required [type=value_error, input_value='\\x1c\\xa0\\n', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/value_error"}"#,
        ),
        (
            r#"{"query":5}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=5, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":null}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":["a"]}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=['a'], input_type=list]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":{"a":1}}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value={'a': 1}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":true}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=True, input_type=bool]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":true}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=True, input_type=bool]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1.5}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1.5, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"3"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='3', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":-1}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","max_results":null}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=None, input_type=NoneType]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1.0}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1.0, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1e2}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=100.0, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":[]}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=[], input_type=list]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":{}}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value={}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_tokens":-1}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_tokens\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","max_tokens":"x"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_tokens\n  Input should be a valid integer [type=int_type, input_value='x', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","claims":false}"#,
            r#"{"error":"1 validation error for _SearchBody\nclaims\n  Input should be a valid integer [type=int_type, input_value=False, input_type=bool]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","claims":-9}"#,
            r#"{"error":"1 validation error for _SearchBody\nclaims\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-9, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","related":"1"}"#,
            r#"{"error":"1 validation error for _SearchBody\nrelated\n  Input should be a valid integer [type=int_type, input_value='1', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","related":-1}"#,
            r#"{"error":"1 validation error for _SearchBody\nrelated\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","related_heads":-2}"#,
            r#"{"error":"1 validation error for _SearchBody\nrelated_heads\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-2, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","related_heads":2.5}"#,
            r#"{"error":"1 validation error for _SearchBody\nrelated_heads\n  Input should be a valid integer [type=int_type, input_value=2.5, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","since_hours":1.5}"#,
            r#"{"error":"1 validation error for _SearchBody\nsince_hours\n  Input should be a valid integer [type=int_type, input_value=1.5, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","since_hours":"1"}"#,
            r#"{"error":"1 validation error for _SearchBody\nsince_hours\n  Input should be a valid integer [type=int_type, input_value='1', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","since_hours":true}"#,
            r#"{"error":"1 validation error for _SearchBody\nsince_hours\n  Input should be a valid integer [type=int_type, input_value=True, input_type=bool]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","project":5}"#,
            r#"{"error":"1 validation error for _SearchBody\nproject\n  Input should be a valid string [type=string_type, input_value=5, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":"q","project":[1]}"#,
            r#"{"error":"1 validation error for _SearchBody\nproject\n  Input should be a valid string [type=string_type, input_value=[1], input_type=list]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":"q","session_id":5}"#,
            r#"{"error":"1 validation error for _SearchBody\nsession_id\n  Input should be a valid string [type=string_type, input_value=5, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":"q","session_id":1.5}"#,
            r#"{"error":"1 validation error for _SearchBody\nsession_id\n  Input should be a valid string [type=string_type, input_value=1.5, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"query":5,"max_results":-1,"claims":"x"}"#,
            r#"{"error":"3 validation errors for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=5, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type\nmax_results\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal\nclaims\n  Input should be a valid integer [type=int_type, input_value='x', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"max_results":-1,"query":" "}"#,
            r#"{"error":"2 validation errors for _SearchBody\nquery\n  Value error, query is required [type=value_error, input_value=' ', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/value_error\nmax_results\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"session_id":1,"project":2,"since_hours":"a","related_heads":-1,"related":"r","claims":1.5,"max_tokens":null,"max_results":false,"query":0}"#,
            r#"{"error":"9 validation errors for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=0, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type\nmax_results\n  Input should be a valid integer [type=int_type, input_value=False, input_type=bool]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type\nmax_tokens\n  Input should be a valid integer [type=int_type, input_value=None, input_type=NoneType]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type\nclaims\n  Input should be a valid integer [type=int_type, input_value=1.5, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type\nrelated\n  Input should be a valid integer [type=int_type, input_value='r', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type\nrelated_heads\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal\nsince_hours\n  Input should be a valid integer [type=int_type, input_value='a', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type\nproject\n  Input should be a valid string [type=string_type, input_value=2, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type\nsession_id\n  Input should be a valid string [type=string_type, input_value=1, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"max_results":3}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Field required [type=missing, input_value={'max_results': 3}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/missing"}"#,
        ),
        (
            r#"{"max_results":3,"x":"é","y":[1,2]}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Field required [type=missing, input_value={'max_results': 3, 'x': 'é', 'y': [1, 2]}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/missing"}"#,
        ),
        (
            r#"{"zzz":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Field required [type=missing, input_value={'zzz': 'bbbbbbbbbbbbbbbb...bbbbbbbbbbbbbbbbbbbbbb'}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/missing"}"#,
        ),
        (
            r#"{"a":{"b":[1,{"c":null}]},"d":1.5e300,"e":true}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Field required [type=missing, input_value={'a': {'b': [1, {'c': Non...d': 1.5e+300, 'e': True}, input_type=dict]\n    For further information visit https://errors.pydantic.dev/2.14/v/missing"}"#,
        ),
        (
            r#"{"query":"q","max_results":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='aaaaaaaaaaaaaaaaaaaaaaaa...aaaaaaaaaaaaaaaaaaaaaaa', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='aaaaaaaaaaaaaaaaaaaaaaaa...aaaaaaaaaaaaaaaaaaaaaaa', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"가가가가가가가가가가가가가가가가가"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='가가가가가가가가...가가가가가가가', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"éééééééééééééééééééééééééééééé"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='éééééééééééé...ééééééééééé', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"ab가가가가가가가가가가가가가가가가가가가가가가가가가가가가가가"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='ab가가가가가가가...가가가가가가가', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"it's"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=\"it's\", input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"a\"b'c"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='a\"b\\'c', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"é\n\t\r\u0001\u007f\u00a0\u200b😀\u2028"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='é\\n\\t\\r\\x01\\x7f\\xa0\\u200b😀\\u2028', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":"\\"}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value='\\\\', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1e400}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=inf, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":-1e400}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=-inf, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1e16}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1e+16, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1e15}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1000000000000000.0, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1e-5}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1e-05, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":0.0001}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=0.0001, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":123456789.125}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=123456789.125, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":-0.0}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=-0.0, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":0.0}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=0.0, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":1.7976931348623157e308}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1.7976931348623157e+308, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":5e-324}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=5e-324, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":123456789012345680.0}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=1.2345678901234568e+17, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":0.1}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=0.1, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":100.0}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=100.0, input_type=float]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","max_results":-1180591620717411303424}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1180591620717411303424, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","claims":-1099511627776}"#,
            r#"{"error":"1 validation error for _SearchBody\nclaims\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1099511627776, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
        (
            r#"{"query":"q","max_results":[1,"a",null,true,1.5,{"k":"v"}]}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be a valid integer [type=int_type, input_value=[1, 'a', None, True, 1.5, {'k': 'v'}], input_type=list]\n    For further information visit https://errors.pydantic.dev/2.14/v/int_type"}"#,
        ),
        (
            r#"{"query":"q","query":5}"#,
            r#"{"error":"1 validation error for _SearchBody\nquery\n  Input should be a valid string [type=string_type, input_value=5, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/string_type"}"#,
        ),
        (
            r#"{"max_results":-1,"query":"q","max_results":-2}"#,
            r#"{"error":"1 validation error for _SearchBody\nmax_results\n  Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-2, input_type=int]\n    For further information visit https://errors.pydantic.dev/2.14/v/greater_than_equal"}"#,
        ),
    ];

    #[test]
    fn bad_bodies_answer_with_the_door_bytes() {
        for (input, expected) in BAD_BODIES {
            let got = parse_search_body(input.as_bytes())
                .err()
                .map(|m| error_json(&m));
            assert_eq!(got.as_deref(), Some(*expected), "input: {input}");
        }
    }

    #[test]
    fn accepted_bodies_keep_the_door_values() {
        let parse = |s: &str| parse_search_body(s.as_bytes()).expect(s);
        for ok in [
            r#"{"query":"q"}"#,
            r#"{"query":"q","bogus":1}"#,
            r#"{"query":"q","query":"r"}"#,
            r#"{"query":"q","since_hours":null}"#,
            r#"{"query":"q","project":null,"session_id":null}"#,
            r#"{"query":"q","max_tokens":0}"#,
        ] {
            parse(ok);
        }
        assert_eq!(parse(r#"{"query":"q","query":"r"}"#).query, "r");
        let defaults = parse(r#"{"query":"q"}"#);
        assert_eq!(
            (
                defaults.max_results,
                defaults.max_tokens,
                defaults.claims(),
                defaults.related()
            ),
            (5, 2000, 0, 0)
        );
        assert_eq!(defaults.related_heads(50), 2);
        assert_eq!(
            parse(r#"{"query":"q","since_hours":-5}"#).since_hours,
            Some(-5)
        );
        assert_eq!(
            parse(r#"{"query":"q","since_hours":null}"#).since_hours,
            None
        );
        assert_eq!(
            parse(r#"{"query":"q","session_id":"  "}"#).session_id(),
            None
        );
        assert_eq!(
            parse("{\"query\":\"q\",\"session_id\":\"\\u001c\"}").session_id(),
            None
        );
        assert_eq!(
            parse(r#"{"query":"q","session_id":" s "}"#).session_id(),
            Some("s")
        );
    }

    // 문은 파싱된 값을 그대로 리트리버에 넘기고, 리트리버가 min(max(x,1), 50) 으로 깎는다
    // (src/ohmyboring/search/retriever.py:317). 엔진은 같은 값에서 같은 결과여야 한다.
    // 2**40 · 2**70 은 pydantic 이 받는다 — 엔진은 u64 를 넘어도 상한으로 포화한다.
    #[test]
    fn range_values_clamp_into_one_to_max_like_the_retriever() {
        let clamped = |v: &str| {
            let req = parse_search_body(format!(r#"{{"query":"q","max_results":{v}}}"#).as_bytes())
                .expect(v);
            req.max_results.clamp(1, crate::serve::MCP_MAX_RESULTS)
        };
        assert_eq!(clamped("0"), 1);
        assert_eq!(clamped("-0"), 1);
        assert_eq!(clamped("1"), 1);
        assert_eq!(clamped("50"), 50);
        assert_eq!(clamped("51"), 50);
        assert_eq!(clamped("9999"), 50);
        assert_eq!(clamped("1099511627776"), 50);
        assert_eq!(clamped("1180591620717411303424"), 50);
        let tokens = parse_search_body(br#"{"query":"q","max_tokens":1180591620717411303424}"#)
            .expect("huge max_tokens");
        assert_eq!(
            tokens.max_tokens.clamp(1, crate::serve::MCP_MAX_TOKENS),
            16_384
        );
        let claims = parse_search_body(br#"{"query":"q","claims":1099511627776}"#).expect("claims");
        assert_eq!(claims.claims(), crate::serve::SEARCH_MAX_CLAIMS);
    }

    fn hit(path: &str) -> SearchHit {
        SearchHit {
            id: "1".into(),
            origin: "wiki".into(),
            project: "p".into(),
            source_path: path.into(),
            snippet: format!("snippet of {path}"),
            dist: Some(0.25),
            dist_kind: Some(crate::store::DistKind::VectorCosine),
            related: vec![],
            superseded_by: vec![],
            used_count: 0,
            contested_count: 0,
            said_by_owner: 0,
            claims: vec![],
            claims_total: None,
        }
    }

    type Calls = RefCell<Vec<String>>;

    async fn run_flow(
        session: Option<&str>,
        search: Result<Vec<SearchHit>, String>,
        handover: Result<(), String>,
        calls: &Calls,
    ) -> Result<Vec<SearchHit>, SearchFlowError> {
        search_flow(
            session,
            async {
                calls.borrow_mut().push("search".into());
                search
            },
            |session, paths| async move {
                calls
                    .borrow_mut()
                    .push(format!("handover {session} {paths:?}"));
                handover
            },
            |row| async move {
                let paths: Vec<_> = row.hits.iter().map(|h| h.path.clone()).collect();
                calls
                    .borrow_mut()
                    .push(format!("log {paths:?} {}", row.answer_snippet));
            },
        )
        .await
    }

    #[tokio::test]
    async fn success_searches_then_hands_over_then_logs() {
        let calls = Calls::default();
        let got = run_flow(
            Some("s1"),
            Ok(vec![hit("/a.md"), hit("/b.md")]),
            Ok(()),
            &calls,
        )
        .await;
        assert_eq!(got.map(|h| h.len()), Ok(2));
        assert_eq!(
            *calls.borrow(),
            vec![
                "search".to_string(),
                r#"handover s1 ["/a.md", "/b.md"]"#.to_string(),
                r#"log ["/a.md", "/b.md"] snippet of /a.md"#.to_string(),
            ]
        );
    }

    #[tokio::test]
    async fn no_session_skips_handover_but_still_logs() {
        let calls = Calls::default();
        let got = run_flow(
            None,
            Ok(vec![hit("/a.md")]),
            Err("must not run".into()),
            &calls,
        )
        .await;
        assert_eq!(got.map(|h| h.len()), Ok(1));
        assert_eq!(calls.borrow().len(), 2);
        assert!(calls.borrow()[1].starts_with("log "));
    }

    // 계약 「이미 아는 것 6」: handover 가 실패하면 query_log 를 쓰지 않는다 — 5xx 재시도가
    // 이중 기록을 내지 않는다.
    #[tokio::test]
    async fn handover_failure_is_502_and_writes_no_query_log() {
        let calls = Calls::default();
        let got = run_flow(
            Some("s1"),
            Ok(vec![hit("/a.md")]),
            Err("db down".into()),
            &calls,
        )
        .await;
        let Err(err) = got else {
            panic!("handover failure must fail the flow")
        };
        assert_eq!(err, SearchFlowError::Handover("db down".into()));
        assert_eq!(err.message(), "handover failed: db down");
        assert!(
            calls.borrow().iter().all(|c| !c.starts_with("log")),
            "{:?}",
            calls.borrow()
        );
    }

    #[tokio::test]
    async fn search_failure_is_502_and_touches_neither_handover_nor_log() {
        let calls = Calls::default();
        let got = run_flow(Some("s1"), Err("embedding: boom".into()), Ok(()), &calls).await;
        let Err(err) = got else {
            panic!("search failure must fail the flow")
        };
        assert_eq!(err.message(), "search failed: embedding: boom");
        assert_eq!(*calls.borrow(), vec!["search".to_string()]);
    }

    #[test]
    fn log_row_has_one_line_per_hit_and_the_first_snippet() {
        let row = log_row(&[hit("/a.md"), hit("/b.md")]);
        let paths: Vec<_> = row.hits.iter().map(|h| h.path.as_str()).collect();
        assert_eq!(paths, ["/a.md", "/b.md"]);
        assert_eq!(row.answer_snippet, "snippet of /a.md");
        assert_eq!(log_row(&[]).answer_snippet, "");
    }

    #[tokio::test]
    async fn success_response_carries_the_engine_header_and_hits_body() {
        let res = search_ok_response(vec![]);
        assert_eq!(res.status(), StatusCode::OK);
        assert_eq!(
            res.headers()
                .get("x-boring-search")
                .map(axum::http::HeaderValue::as_bytes),
            Some(&b"engine"[..])
        );
        let body = axum::body::to_bytes(res.into_body(), 1024).await.unwrap();
        assert_eq!(&body[..], br#"{"hits":[]}"#);
    }

    fn state_without_store() -> crate::serve::AppState {
        let cfg = crate::config::BoringConfig::default();
        crate::serve::AppState {
            store: None,
            code_index: None,
            llm: std::sync::Arc::new(crate::llm::Llm::from_config(&cfg)),
            vault_dir: std::sync::Arc::new(None),
            pii: std::sync::Arc::new(None),
            cfg: std::sync::Arc::new(cfg),
            cfg_path: std::sync::Arc::new(None),
            sync_lock: std::sync::Arc::default(),
            wiki_index: std::sync::Arc::default(),
            last_compact: std::sync::Arc::default(),
            compact_failure: std::sync::Arc::default(),
            db_healthy_last: std::sync::Arc::default(),
            owner_token: std::sync::Arc::new(None),
        }
    }

    async fn post_search(body: &str) -> (StatusCode, Vec<u8>) {
        use tower::ServiceExt;
        let req = axum::extract::Request::builder()
            .method("POST")
            .uri("/search")
            .body(axum::body::Body::from(body.to_owned()))
            .unwrap();
        let res = crate::serve::build_router(state_without_store())
            .oneshot(req)
            .await
            .unwrap();
        let status = res.status();
        let bytes = axum::body::to_bytes(res.into_body(), 1 << 20)
            .await
            .unwrap();
        (status, bytes.to_vec())
    }

    // 라우터를 거친다: content-type 이 없어도(문은 request.json() 이라 안 본다) 같은 400 몸.
    #[tokio::test]
    async fn router_answers_400_bytes_before_the_store_check() {
        for (input, expected) in BAD_BODIES {
            let (status, body) = post_search(input).await;
            assert_eq!(status, StatusCode::BAD_REQUEST, "input: {input}");
            assert_eq!(
                std::str::from_utf8(&body).unwrap(),
                *expected,
                "input: {input}"
            );
        }
    }

    #[tokio::test]
    async fn router_without_store_is_503_not_the_wiki_fallback() {
        let (status, body) = post_search(r#"{"query":"q"}"#).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(&body[..], br#"{"error":"store not configured"}"#);
    }
}
