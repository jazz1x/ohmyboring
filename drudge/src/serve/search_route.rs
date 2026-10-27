//! POST /search — 문(agents/door/door.py `_search`, `_SearchBody`)과 같은 몸 파싱 400, 503,
//! handover 502 와 query_log 순서, 성공 헤더. 기준은 Python 출력이다.
//!
//! 몸 400 의 `str(ValidationError)` 문구는 pydantic 2.14.1 에서 받은 바이트다. 컨테이너의
//! pydantic 버전은 미확인이라 오류 URL 의 `2.14` 는 그 설치본 값이다.

use std::future::Future;

use axum::Json;
use axum::http::{HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;

use crate::serve::{SearchHit, SearchReq, SearchResp};
use crate::store::LoggedHit;

const NOT_JSON: &str = "body must be JSON";
const NOT_OBJECT: &str = "body must be a JSON object";
const PYDANTIC_ERRORS_URL: &str = "https://errors.pydantic.dev/2.14/v";
/// pydantic-core 가 input_value 의 repr 을 이 바이트 수까지만 싣는다 (넘으면 앞 25 · 뒤 24 바이트).
const REPR_MAX_BYTES: usize = 50;

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

pub(crate) fn log_row(hits: &[SearchHit]) -> LogRow {
    LogRow {
        hits: hits
            .iter()
            .map(|h| match (h.dist, h.dist_kind) {
                (Some(dist), Some(kind)) => {
                    LoggedHit::with_distance(h.source_path.clone(), dist, kind)
                }
                _ => LoggedHit::without_distance(h.source_path.clone()),
            })
            .collect(),
        answer_snippet: hits
            .first()
            .map(|h| h.snippet.chars().take(200).collect())
            .unwrap_or_default(),
    }
}

/// 검색 → (세션이 있으면) handover → query_log. 앞 단계가 실패하면 뒷 단계는 부르지 않는다:
/// handover 가 실패한 요청은 query_log 를 남기지 않아 5xx 재시도가 이중 기록을 내지 않는다.
pub(crate) async fn search_flow<S, H, HFut, L, LFut>(
    session: Option<&str>,
    search: S,
    handover: H,
    log: L,
) -> Result<Vec<SearchHit>, SearchFlowError>
where
    S: Future<Output = Result<Vec<SearchHit>, String>>,
    H: FnOnce(String, Vec<String>) -> HFut,
    HFut: Future<Output = Result<(), String>>,
    L: FnOnce(LogRow) -> LFut,
    LFut: Future<Output = ()>,
{
    let hits = search.await.map_err(SearchFlowError::Search)?;
    if let Some(session) = session {
        let paths = hits.iter().map(|h| h.source_path.clone()).collect();
        handover(session.to_owned(), paths)
            .await
            .map_err(SearchFlowError::Handover)?;
    }
    log(log_row(&hits)).await;
    Ok(hits)
}

pub(crate) fn search_ok_response(hits: Vec<SearchHit>) -> Response {
    (
        [(
            HeaderName::from_static("x-boring-search"),
            HeaderValue::from_static("engine"),
        )],
        Json(SearchResp { hits }),
    )
        .into_response()
}

/// 파이썬 `str.isspace()` — 러스트 `char::is_whitespace` 에 \x1c-\x1f 가 더해진다.
fn py_is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// 파이썬 `str.strip()`.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(py_is_space)
}

/// 파이썬 `str.isprintable()` 가 거짓인 코드포인트 (제어 문자 외). Zs·Zl·Zp·Cf·Co 범위를
/// 파이썬 unicodedata 에서 뽑았다. 미배정(Cn) 코드포인트는 유니코드 버전이 달라 미확인.
fn py_is_printable(c: char) -> bool {
    let cp = u32::from(c);
    let non_printable = c.is_control()
        || matches!(
            cp,
            0xA0 | 0xAD
                | 0x600..=0x605
                | 0x61C
                | 0x6DD
                | 0x70F
                | 0x890..=0x891
                | 0x8E2
                | 0x1680
                | 0x180E
                | 0x2000..=0x200F
                | 0x2028..=0x202F
                | 0x205F..=0x2064
                | 0x2066..=0x206F
                | 0x3000
                | 0xE000..=0xF8FF
                | 0xFEFF
                | 0xFFF9..=0xFFFB
                | 0x110BD
                | 0x110CD
                | 0x13430..=0x1343F
                | 0x1BCA0..=0x1BCA3
                | 0x1D173..=0x1D17A
                | 0xE0001
                | 0xE0020..=0xE007F
                | 0x000F_0000..=0x000F_FFFD
                | 0x0010_0000..=0x0010_FFFD
        );
    !non_printable
}

/// 파이썬 repr 의 `\xNN` · `\uNNNN` · `\UNNNNNNNN`.
fn hex_escape(cp: u32) -> String {
    match cp {
        0..=0xFF => format!("\\x{cp:02x}"),
        0x100..=0xFFFF => format!("\\u{cp:04x}"),
        _ => format!("\\U{cp:08x}"),
    }
}

/// 파이썬 `repr(str)`.
fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        let cp = u32::from(c);
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if py_is_printable(c) => out.push(c),
            _ => out.push_str(&hex_escape(cp)),
        }
    }
    out.push(quote);
    out
}

/// 파이썬 `repr(float)` — 10진 지수 -4 < decpt <= 16 이면 고정 표기, 아니면 `1e+16` 꼴.
fn py_float_repr(x: f64) -> String {
    if x.is_infinite() {
        return if x < 0.0 { "-inf" } else { "inf" }.to_owned();
    }
    let sign = if x.is_sign_negative() { "-" } else { "" };
    if x == 0.0 {
        return format!("{sign}0.0");
    }
    let sci = format!("{:e}", x.abs());
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let decpt = exp + 1;
    let body = if decpt > 16 || decpt <= -4 {
        let (first, rest) = digits.split_at(1);
        let frac = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        format!(
            "{first}{frac}e{}{:02}",
            if exp < 0 { '-' } else { '+' },
            exp.abs()
        )
    } else if decpt <= 0 {
        format!(
            "0.{}{digits}",
            "0".repeat(usize::try_from(-decpt).unwrap_or(0))
        )
    } else {
        let point = usize::try_from(decpt).unwrap_or(0);
        if point >= digits.len() {
            format!("{digits}{}.0", "0".repeat(point - digits.len()))
        } else {
            format!("{}.{}", &digits[..point], &digits[point..])
        }
    };
    format!("{sign}{body}")
}

/// `-?\d+` 이면 (음수 여부, 자릿수 문자열) — 파이썬 json 이 int 로 읽는 토큰.
fn int_literal(raw: &str) -> Option<(bool, &str)> {
    let (negative, digits) = raw.strip_prefix('-').map_or((false, raw), |d| (true, d));
    let all_digits = !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit());
    all_digits.then_some((negative && digits != "0", digits))
}

type Entry = (String, Box<RawValue>);

/// 파이썬 dict 의미: 중복 키는 처음 자리에 마지막 값.
fn python_dict(entries: Vec<Entry>) -> Vec<Entry> {
    let mut out: Vec<Entry> = Vec::with_capacity(entries.len());
    for (key, value) in entries {
        match out.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = value,
            None => out.push((key, value)),
        }
    }
    out
}

struct Entries(Vec<Entry>);

impl<'de> Deserialize<'de> for Entries {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = Entries;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Entries, A::Error> {
                let mut out = Vec::new();
                while let Some(entry) = map.next_entry::<String, Box<RawValue>>()? {
                    out.push(entry);
                }
                Ok(Entries(out))
            }
        }
        deserializer.deserialize_map(Visitor)
    }
}

fn dict_entries(raw: &str) -> Vec<Entry> {
    serde_json::from_str::<Entries>(raw).map_or_else(|_| Vec::new(), |Entries(e)| python_dict(e))
}

/// 파이썬 `repr(json.loads(raw))`. `raw` 는 `body_entries` 가 이미 검증한 JSON 조각이다.
fn py_repr(raw: &str) -> String {
    match raw.as_bytes().first() {
        Some(b'"') => {
            serde_json::from_str::<String>(raw).map_or_else(|_| String::new(), |s| py_str_repr(&s))
        }
        Some(b't') => "True".to_owned(),
        Some(b'f') => "False".to_owned(),
        Some(b'n') => "None".to_owned(),
        Some(b'[') => {
            let items = serde_json::from_str::<Vec<Box<RawValue>>>(raw).unwrap_or_default();
            let parts: Vec<String> = items.iter().map(|v| py_repr(v.get())).collect();
            format!("[{}]", parts.join(", "))
        }
        Some(b'{') => dict_repr(&dict_entries(raw)),
        _ => match int_literal(raw) {
            Some((negative, digits)) => format!("{}{digits}", if negative { "-" } else { "" }),
            None => py_float_repr(raw.parse::<f64>().unwrap_or(f64::NAN)),
        },
    }
}

fn dict_repr(entries: &[Entry]) -> String {
    let parts: Vec<String> = entries
        .iter()
        .map(|(k, v)| format!("{}: {}", py_str_repr(k), py_repr(v.get())))
        .collect();
    format!("{{{}}}", parts.join(", "))
}

fn py_type(raw: &str) -> &'static str {
    match raw.as_bytes().first() {
        Some(b'"') => "str",
        Some(b't' | b'f') => "bool",
        Some(b'n') => "NoneType",
        Some(b'[') => "list",
        Some(b'{') => "dict",
        _ if int_literal(raw).is_some() => "int",
        _ => "float",
    }
}

/// pydantic-core `truncate_safe_repr`: 50 바이트를 넘으면 앞 25 바이트(문자 경계로 내림) +
/// `...` + 뒤 24 바이트(문자 경계로 올림).
fn truncated(repr: &str) -> String {
    if repr.len() <= REPR_MAX_BYTES {
        return repr.to_owned();
    }
    let mut head = REPR_MAX_BYTES / 2;
    while !repr.is_char_boundary(head) {
        head -= 1;
    }
    let mut tail = repr.len() - (REPR_MAX_BYTES / 2 - 1);
    while !repr.is_char_boundary(tail) {
        tail += 1;
    }
    format!("{}...{}", &repr[..head], &repr[tail..])
}

/// pydantic ValidationError 한 줄 (`str(e)` 의 한 필드 블록).
struct Problem {
    field: &'static str,
    message: &'static str,
    kind: &'static str,
    input: String,
    input_type: &'static str,
}

impl Problem {
    fn on_raw(field: &'static str, message: &'static str, kind: &'static str, raw: &str) -> Self {
        Self {
            field,
            message,
            kind,
            input: truncated(&py_repr(raw)),
            input_type: py_type(raw),
        }
    }

    fn block(&self) -> String {
        format!(
            "{}\n  {} [type={}, input_value={}, input_type={}]\n    For further information visit {PYDANTIC_ERRORS_URL}/{}",
            self.field, self.message, self.kind, self.input, self.input_type, self.kind
        )
    }
}

fn format_problems(problems: &[Problem]) -> String {
    let n = problems.len();
    let plural = if n == 1 { "" } else { "s" };
    let blocks: Vec<String> = problems.iter().map(Problem::block).collect();
    format!(
        "{n} validation error{plural} for _SearchBody\n{}",
        blocks.join("\n")
    )
}

/// 큰 정수는 u128 로 포화 — 문은 임의 정밀도 int 를 리트리버에 넘기고 리트리버가 깎으므로
/// 포화해도 깎인 값은 같다.
struct Int {
    negative: bool,
    magnitude: u128,
}

impl Int {
    fn usize(&self) -> usize {
        usize::try_from(self.magnitude).unwrap_or(usize::MAX)
    }

    fn u32(&self) -> u32 {
        u32::try_from(self.magnitude).unwrap_or(u32::MAX)
    }

    fn i32(&self) -> i32 {
        let magnitude = i64::try_from(self.magnitude).unwrap_or(i64::MAX);
        let signed = if self.negative { -magnitude } else { magnitude };
        i32::try_from(signed).unwrap_or(if self.negative { i32::MIN } else { i32::MAX })
    }
}

fn lookup<'a>(entries: &'a [Entry], key: &str) -> Option<&'a str> {
    entries.iter().find(|(k, _)| k == key).map(|(_, v)| v.get())
}

/// StrictInt (+ ge=0 이면 음수 거절). 없으면 `default`, `nullable` 이면 null 도 `Ok(None)`.
fn int_field(
    entries: &[Entry],
    field: &'static str,
    non_negative: bool,
    nullable: bool,
) -> Result<Option<Int>, Problem> {
    match lookup(entries, field) {
        None => Ok(None),
        Some("null") if nullable => Ok(None),
        Some(raw) => match int_literal(raw) {
            None => Err(Problem::on_raw(
                field,
                "Input should be a valid integer",
                "int_type",
                raw,
            )),
            Some((true, _)) if non_negative => Err(Problem::on_raw(
                field,
                "Input should be greater than or equal to 0",
                "greater_than_equal",
                raw,
            )),
            Some((negative, digits)) => Ok(Some(Int {
                negative,
                magnitude: digits.parse::<u128>().unwrap_or(u128::MAX),
            })),
        },
    }
}

/// StrictStr (nullable 이면 null 은 `Ok(None)`).
fn str_field(
    entries: &[Entry],
    field: &'static str,
    nullable: bool,
) -> Result<Option<String>, Problem> {
    match lookup(entries, field) {
        None => Ok(None),
        Some("null") if nullable => Ok(None),
        Some(raw) if raw.starts_with('"') => Ok(serde_json::from_str::<String>(raw).ok()),
        Some(raw) => Err(Problem::on_raw(
            field,
            "Input should be a valid string",
            "string_type",
            raw,
        )),
    }
}

fn query_field(entries: &[Entry]) -> Result<String, Problem> {
    match str_field(entries, "query", false)? {
        None => Err(Problem {
            field: "query",
            message: "Field required",
            kind: "missing",
            input: truncated(&dict_repr(entries)),
            input_type: "dict",
        }),
        Some(query) if py_strip(&query).is_empty() => Err(Problem {
            field: "query",
            message: "Value error, query is required",
            kind: "value_error",
            input: truncated(&py_str_repr(&query)),
            input_type: "str",
        }),
        Some(query) => Ok(query),
    }
}

/// 파이썬 json 은 짝 없는 서로게이트 이스케이프(`"\ud800"`)를 받지만 serde_json 의 문자열
/// 해독은 거절한다 — 그런 본문은 파싱 전에 "body must be JSON" 으로 접는다 (문과 다른 자리).
fn has_lone_surrogate(text: &str) -> bool {
    let bytes = text.as_bytes();
    let hex4 = |at: usize| {
        bytes
            .get(at..at + 4)
            .and_then(|h| std::str::from_utf8(h).ok())
            .and_then(|h| u32::from_str_radix(h, 16).ok())
    };
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] != b'\\' {
            i += 1;
        } else if bytes.get(i + 1) == Some(&b'u') {
            let unit = hex4(i + 2).unwrap_or(0);
            let low_follows = bytes.get(i + 6..i + 8) == Some(b"\\u".as_slice())
                && hex4(i + 8).is_some_and(|l| (0xDC00..0xE000).contains(&l));
            match unit {
                0xD800..0xDC00 if low_follows => i += 12,
                0xD800..0xE000 => return true,
                _ => i += 6,
            }
        } else {
            i += 2;
        }
    }
    false
}

fn body_entries(body: &[u8]) -> Result<Vec<Entry>, String> {
    let text = std::str::from_utf8(body).map_err(|_| NOT_JSON.to_owned())?;
    let whole: Box<RawValue> = serde_json::from_str(text).map_err(|_| NOT_JSON.to_owned())?;
    if has_lone_surrogate(whole.get()) {
        return Err(NOT_JSON.to_owned());
    }
    if !whole.get().starts_with('{') {
        return Err(NOT_OBJECT.to_owned());
    }
    serde_json::from_str::<Entries>(whole.get())
        .map(|Entries(entries)| python_dict(entries))
        .map_err(|_| NOT_JSON.to_owned())
}

/// 몸을 경계에서 한 번 파싱한다 — 문의 `_SearchBody` 와 같은 거절. `Err` 는 400 의 `error` 문자열.
pub(crate) fn parse_search_body(body: &[u8]) -> Result<SearchReq, String> {
    let entries = body_entries(body)?;
    let query = query_field(&entries);
    let max_results = int_field(&entries, "max_results", true, false);
    let max_tokens = int_field(&entries, "max_tokens", true, false);
    let claims = int_field(&entries, "claims", true, false);
    let related = int_field(&entries, "related", true, false);
    let related_heads = int_field(&entries, "related_heads", true, false);
    let since_hours = int_field(&entries, "since_hours", false, true);
    let project = str_field(&entries, "project", true);
    let session_id = str_field(&entries, "session_id", true);
    match (
        query,
        max_results,
        max_tokens,
        claims,
        related,
        related_heads,
        since_hours,
        project,
        session_id,
    ) {
        (
            Ok(query),
            Ok(max_results),
            Ok(max_tokens),
            Ok(claims),
            Ok(related),
            Ok(related_heads),
            Ok(since_hours),
            Ok(project),
            Ok(session_id),
        ) => Ok(SearchReq {
            query,
            max_results: max_results.map_or(5, |v| v.usize()),
            max_tokens: max_tokens.map_or(2000, |v| v.usize()),
            project,
            since_hours: since_hours.map(|v| v.i32()),
            related: related.map_or(0, |v| v.usize()),
            related_heads: related_heads.map_or(2, |v| v.usize()),
            claims: claims.map(|v| v.u32()),
            session_id,
        }),
        (
            query,
            max_results,
            max_tokens,
            claims,
            related,
            related_heads,
            since_hours,
            project,
            session_id,
        ) => {
            let problems: Vec<Problem> = [
                query.err(),
                max_results.err(),
                max_tokens.err(),
                claims.err(),
                related.err(),
                related_heads.err(),
                since_hours.err(),
                project.err(),
                session_id.err(),
            ]
            .into_iter()
            .flatten()
            .collect();
            Err(format_problems(&problems))
        }
    }
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
