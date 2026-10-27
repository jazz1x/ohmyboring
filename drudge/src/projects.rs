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

pub fn parse_query(_raw_query: Option<&str>) -> ProjectsQuery {
    ProjectsQuery::Names
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
