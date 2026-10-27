//! `GET /projects` query — the Python door's `_projects` / `_check_active_days`.
//!
//! The query is parsed once into a `ProjectsQuery`; the handler folds it into a response.

/// What a `/projects` request asked for.
#[derive(Debug, PartialEq, Eq)]
pub enum ProjectsQuery {
    /// No `active_days` key: the plain project-name list.
    Names,
    /// `active_days` in 1..=365.
    ActiveDays(i32),
    /// `active_days` present but unusable: the 400 message.
    Rejected(String),
}

const MIN_ACTIVE_DAYS: i32 = 1;
const MAX_ACTIVE_DAYS: i32 = 365;
const NOT_AN_INTEGER: &str = "active_days must be an integer 1..365";
/// CPython refuses `int()` on more digits than this (leading zeros count).
const PY_INT_MAX_DIGITS: usize = 4300;

/// Door SQL (`_ACTIVE_PROJECTS_SQL`) plus a `, 1` tie key so equal counts come back in a fixed order.
pub const ACTIVE_PROJECTS_SQL: &str = "select project, count(*) from document where project <> '' \
and updated_at > now() - make_interval(days => $1) group by 1 order by 2 desc, 1";

/// The 200 body for `ActiveDays` — door `_active_projects_payload`.
pub fn active_payload(rows: Vec<(String, i64)>, active_days: i32) -> serde_json::Value {
    let projects: Vec<_> = rows
        .into_iter()
        .map(|(project, documents)| serde_json::json!({"project": project, "documents": documents}))
        .collect();
    serde_json::json!({"projects": projects, "active_days": active_days})
}

/// Parse the query once. Starlette `query_params.get`: the last duplicate wins, a bare key is "".
pub fn parse_query(raw_query: Option<&str>) -> ProjectsQuery {
    match crate::claim_source::last_param_opt(raw_query, "active_days") {
        None => ProjectsQuery::Names,
        Some(raw) => match check_active_days(&raw) {
            Ok(days) => ProjectsQuery::ActiveDays(days),
            Err(message) => ProjectsQuery::Rejected(message),
        },
    }
}

/// The door's `_check_active_days`: Python `int()` then 1..=365, messages verbatim.
fn check_active_days(raw: &str) -> Result<i32, String> {
    crate::approved::py_int(raw).ok_or_else(|| NOT_AN_INTEGER.to_string())?;
    // `py_int` accepted the text, so it is only whitespace, one sign, digits and underscores.
    let digits: String = raw
        .chars()
        .filter_map(crate::approved::decimal_value)
        .filter_map(|d| char::from_digit(d, 10))
        .collect();
    if digits.len() > PY_INT_MAX_DIGITS {
        return Err(NOT_AN_INTEGER.to_string());
    }
    let magnitude = digits.trim_start_matches('0');
    let shown = match (magnitude.is_empty(), raw.contains('-')) {
        (true, _) => "0".to_string(),
        (false, true) => format!("-{magnitude}"),
        (false, false) => magnitude.to_string(),
    };
    shown
        .parse::<i32>()
        .ok()
        .filter(|days| (MIN_ACTIVE_DAYS..=MAX_ACTIVE_DAYS).contains(days))
        .ok_or_else(|| {
            format!("active_days must be {MIN_ACTIVE_DAYS}..{MAX_ACTIVE_DAYS}, got {shown}")
        })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    const INT_MSG: &str = "active_days must be an integer 1..365";

    fn rejected(message: &str) -> ProjectsQuery {
        ProjectsQuery::Rejected(message.to_string())
    }

    fn range(got: &str) -> ProjectsQuery {
        rejected(&format!("active_days must be 1..365, got {got}"))
    }

    // Expected values: Python door `_check_active_days` behind Starlette QueryParams
    // (TestClient on the door app), 2026-10-28.
    #[test]
    fn active_projects_sql_is_door_sql_plus_tie_key() {
        assert_eq!(
            ACTIVE_PROJECTS_SQL,
            "select project, count(*) from document where project <> '' and updated_at > now() - make_interval(days => $1) group by 1 order by 2 desc, 1"
        );
    }

    // Door bytes: Starlette JSONResponse of `_active_projects_payload(7)` with these rows.
    #[test]
    fn active_payload_keeps_the_door_key_order() {
        let rows = vec![("omb".to_owned(), 12), ("ç\"x".to_owned(), 1)];
        assert_eq!(
            serde_json::to_string(&active_payload(rows, 7)).unwrap(),
            r#"{"projects":[{"project":"omb","documents":12},{"project":"ç\"x","documents":1}],"active_days":7}"#
        );
    }

    #[test]
    fn parse_query_matches_door() {
        let long_nines = format!("active_days={}", "9".repeat(4301));
        let long_ones = format!("active_days={}", "1".repeat(4300));
        let long_zeros = format!("active_days={}7", "0".repeat(4301));
        let cases: Vec<(String, ProjectsQuery)> = vec![
            ("active_days=14".into(), ProjectsQuery::ActiveDays(14)),
            ("active_days=%2014".into(), ProjectsQuery::ActiveDays(14)),
            ("active_days=+14".into(), ProjectsQuery::ActiveDays(14)),
            ("active_days=%2B14".into(), ProjectsQuery::ActiveDays(14)),
            ("active_days=1_0".into(), ProjectsQuery::ActiveDays(10)),
            ("active_days=0".into(), range("0")),
            ("active_days=-0".into(), range("0")),
            ("active_days=366".into(), range("366")),
            ("active_days=-1".into(), range("-1")),
            ("active_days=365".into(), ProjectsQuery::ActiveDays(365)),
            ("active_days=1".into(), ProjectsQuery::ActiveDays(1)),
            ("active_days=%0914%0A".into(), ProjectsQuery::ActiveDays(14)),
            ("active_days=abc".into(), rejected(INT_MSG)),
            ("active_days=".into(), rejected(INT_MSG)),
            ("active_days".into(), rejected(INT_MSG)),
            ("active_days=1.5".into(), rejected(INT_MSG)),
            ("active_days=1__0".into(), rejected(INT_MSG)),
            ("active_days=0x10".into(), rejected(INT_MSG)),
            ("active_days=%2B%2B1".into(), rejected(INT_MSG)),
            ("active_days=%E0%A4%A4".into(), rejected(INT_MSG)),
            (
                "active_days=%EF%BC%91%EF%BC%94".into(),
                ProjectsQuery::ActiveDays(14),
            ),
            (
                "active_days=99999999999999999999".into(),
                range("99999999999999999999"),
            ),
            (long_nines, rejected(INT_MSG)),
            (long_zeros, rejected(INT_MSG)),
            (long_ones, range(&"1".repeat(4300))),
            (
                "active_days=1&active_days=2".into(),
                ProjectsQuery::ActiveDays(2),
            ),
            (
                "active_days=0&active_days=2".into(),
                ProjectsQuery::ActiveDays(2),
            ),
            ("active_days=2&active_days=0".into(), range("0")),
            ("foo=1&active_days=7".into(), ProjectsQuery::ActiveDays(7)),
            ("foo=1".into(), ProjectsQuery::Names),
            (String::new(), ProjectsQuery::Names),
        ];
        for (query, want) in cases {
            let shown = &query[..query.len().min(40)];
            assert_eq!(parse_query(Some(&query)), want, "{shown}");
        }
        assert_eq!(parse_query(None), ProjectsQuery::Names);
    }
}
