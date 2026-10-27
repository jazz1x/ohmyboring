//! `GET /claim-source` — a register subject's current claim, as the note path a card cites.
//! Same contract as `agents/door/claim_source.py::pick_current`: pure, rows in, struct out.
//! Struct field order is the JSON key order, so bytes match Python's insertion order.

use chrono::{DateTime, FixedOffset};
use serde::Serialize;

/// Verbatim from `agents/door/claim_source.py::SQL` (`%s` -> `$1`).
pub const SQL: &str = "select c.source_path, c.valid_from, c.value, coalesce(d.author, 'unknown') from claim c \
    left join document d on d.source_path = c.source_path \
    where c.subject = $1 and c.superseded_at is null \
    order by c.valid_from desc";

#[derive(Debug, Clone)]
pub struct ClaimRow {
    pub source_path: String,
    pub valid_from: DateTime<FixedOffset>,
    pub value: String,
    pub author: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Candidate {
    pub note: String,
    pub valid_from: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ClaimSource {
    pub subject: String,
    pub note: String,
    pub valid_from: String,
    pub value: String,
    pub candidates: Vec<Candidate>,
}

/// What the `subject` query param turned out to be; the handler folds it into a status.
#[derive(Debug, PartialEq, Eq)]
pub enum SubjectCheck {
    Blank,
    Nul,
    Valid(String),
}

/// Python `str.isspace()` — Unicode White_Space plus U+001C..U+001F, which `strip()` also eats.
fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn hex_val(b: u8) -> Option<u8> {
    (b as char).to_digit(16).and_then(|d| u8::try_from(d).ok())
}

/// `urllib.parse.unquote(s.replace("+", " "), errors="replace")`: a `%` not followed by two hex
/// digits stays literal; decoded bytes that are not UTF-8 become U+FFFD.
fn decode_component(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let hi = bytes.get(i + 1).copied().and_then(hex_val);
        let lo = bytes.get(i + 2).copied().and_then(hex_val);
        match (bytes[i], hi, lo) {
            (b'%', Some(h), Some(l)) => {
                out.push(h * 16 + l);
                i += 3;
            }
            (b'+', _, _) => {
                out.push(b' ');
                i += 1;
            }
            (b, _, _) => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Starlette `request.query_params.get("subject", "")`: `parse_qsl(keep_blank_values=True)`,
/// empty pairs skipped, a pair without `=` has an empty value, the last duplicate wins.
fn last_subject(raw_query: Option<&str>) -> String {
    raw_query
        .unwrap_or("")
        .split('&')
        .filter(|pair| !pair.is_empty())
        .filter_map(|pair| {
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            (decode_component(key) == "subject").then(|| decode_component(value))
        })
        .next_back()
        .unwrap_or_default()
}

pub fn check_subject(raw_query: Option<&str>) -> SubjectCheck {
    let subject = last_subject(raw_query);
    if subject.trim_matches(is_py_space).is_empty() {
        SubjectCheck::Blank
    } else if subject.contains('\0') {
        SubjectCheck::Nul
    } else {
        SubjectCheck::Valid(subject)
    }
}

/// Python renders `valid_from.isoformat(timespec="seconds")`: fractions truncated, `+HH:MM` offset.
fn iso_seconds(at: &DateTime<FixedOffset>) -> String {
    at.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

/// Owner-written first, then `valid_from` descending, then `source_path` ascending (code point,
/// which is UTF-8 byte order). Duplicates of one note stay candidates, as in Python.
pub fn pick_current(subject: &str, mut rows: Vec<ClaimRow>) -> Option<ClaimSource> {
    rows.sort_by(|a, b| {
        let owner = |r: &ClaimRow| r.author == "owner";
        owner(b)
            .cmp(&owner(a))
            .then_with(|| b.valid_from.cmp(&a.valid_from))
            .then_with(|| a.source_path.cmp(&b.source_path))
    });
    let candidates = rows
        .iter()
        .map(|r| Candidate {
            note: r.source_path.clone(),
            valid_from: iso_seconds(&r.valid_from),
        })
        .collect();
    let win = rows.into_iter().next()?;
    Some(ClaimSource {
        subject: subject.to_string(),
        valid_from: iso_seconds(&win.valid_from),
        note: win.source_path,
        value: win.value,
        candidates,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use chrono::{TimeZone, Utc};

    fn at(y: i32, mo: u32, d: u32, h: u32, mi: u32, s: u32) -> DateTime<FixedOffset> {
        Utc.with_ymd_and_hms(y, mo, d, h, mi, s)
            .unwrap()
            .fixed_offset()
    }

    fn row(path: &str, valid_from: DateTime<FixedOffset>, value: &str, author: &str) -> ClaimRow {
        ClaimRow {
            source_path: path.into(),
            valid_from,
            value: value.into(),
            author: author.into(),
        }
    }

    fn body(subject: &str, rows: Vec<ClaimRow>) -> String {
        serde_json::to_string(&pick_current(subject, rows).unwrap()).unwrap()
    }

    // Expected values: Python claim_source.pick_current + Starlette JSONResponse
    // (ensure_ascii=False, separators=(",", ":")) run with the repo .venv `python -I`, 2026-10-28.
    #[test]
    fn owner_older_beats_newer_other() {
        let rows = vec![
            row("/b.md", at(2026, 9, 20, 0, 0, 0), "new", "agent:x"),
            row("/a.md", at(2026, 9, 1, 0, 0, 0), "mine", "owner"),
        ];
        assert_eq!(
            body("s", rows),
            r#"{"subject":"s","note":"/a.md","valid_from":"2026-09-01T00:00:00+00:00","value":"mine","candidates":[{"note":"/a.md","valid_from":"2026-09-01T00:00:00+00:00"},{"note":"/b.md","valid_from":"2026-09-20T00:00:00+00:00"}]}"#
        );
    }

    #[test]
    fn newest_wins_among_several_and_input_order_is_irrelevant() {
        let rows = vec![
            row("/1.md", at(2026, 9, 1, 0, 0, 0), "v1", "unknown"),
            row("/2.md", at(2026, 9, 20, 3, 4, 5), "v2", "inferred"),
            row("/3.md", at(2026, 9, 10, 0, 0, 0), "v3", "agent:hermes"),
        ];
        let want = r#"{"subject":"s","note":"/2.md","valid_from":"2026-09-20T03:04:05+00:00","value":"v2","candidates":[{"note":"/2.md","valid_from":"2026-09-20T03:04:05+00:00"},{"note":"/3.md","valid_from":"2026-09-10T00:00:00+00:00"},{"note":"/1.md","valid_from":"2026-09-01T00:00:00+00:00"}]}"#;
        let mut rev = rows.clone();
        rev.reverse();
        assert_eq!(body("s", rows), want);
        assert_eq!(body("s", rev), want);
    }

    #[test]
    fn ties_break_by_source_path_code_point_ascending() {
        let t = at(2026, 9, 1, 0, 0, 0);
        let rows = vec![
            row("/vault/b.md", t, "vb", "unknown"),
            row("/vault/a.md", t, "va", "unknown"),
            row("/vault/가.md", t, "vg", "unknown"),
        ];
        let want = r#"{"subject":"s","note":"/vault/a.md","valid_from":"2026-09-01T00:00:00+00:00","value":"va","candidates":[{"note":"/vault/a.md","valid_from":"2026-09-01T00:00:00+00:00"},{"note":"/vault/b.md","valid_from":"2026-09-01T00:00:00+00:00"},{"note":"/vault/가.md","valid_from":"2026-09-01T00:00:00+00:00"}]}"#;
        let mut rev = rows.clone();
        rev.reverse();
        assert_eq!(body("s", rows), want);
        assert_eq!(body("s", rev), want);
    }

    #[test]
    fn fractional_seconds_truncate_and_escapes_stay_python_shaped() {
        let frac = Utc
            .with_ymd_and_hms(2026, 9, 1, 12, 30, 45)
            .unwrap()
            .fixed_offset()
            + chrono::Duration::microseconds(999_999);
        let rows = vec![row("/f.md", frac, "f \"q\" \\ \n 한글 🙂", "unknown")];
        assert_eq!(
            body("s", rows),
            r#"{"subject":"s","note":"/f.md","valid_from":"2026-09-01T12:30:45+00:00","value":"f \"q\" \\ \n 한글 🙂","candidates":[{"note":"/f.md","valid_from":"2026-09-01T12:30:45+00:00"}]}"#
        );
    }

    #[test]
    fn duplicate_notes_stay_as_candidates() {
        let rows = vec![
            row("/same.md", at(2026, 9, 2, 0, 0, 0), "v", "unknown"),
            row("/same.md", at(2026, 9, 1, 0, 0, 0), "old", "unknown"),
        ];
        assert_eq!(
            body("s", rows),
            r#"{"subject":"s","note":"/same.md","valid_from":"2026-09-02T00:00:00+00:00","value":"v","candidates":[{"note":"/same.md","valid_from":"2026-09-02T00:00:00+00:00"},{"note":"/same.md","valid_from":"2026-09-01T00:00:00+00:00"}]}"#
        );
    }

    #[test]
    fn subject_is_echoed_raw() {
        let rows = vec![row("/a.md", at(2026, 9, 1, 0, 0, 0), "v", "unknown")];
        assert_eq!(
            body("한 글 %+", rows),
            r#"{"subject":"한 글 %+","note":"/a.md","valid_from":"2026-09-01T00:00:00+00:00","value":"v","candidates":[{"note":"/a.md","valid_from":"2026-09-01T00:00:00+00:00"}]}"#
        );
    }

    #[test]
    fn empty_rows_is_none_the_404() {
        assert_eq!(pick_current("x", vec![]), None);
    }

    fn valid(s: &str) -> SubjectCheck {
        SubjectCheck::Valid(s.into())
    }

    // Expected values: Starlette QueryParams(q).get("subject", "") run with the repo .venv, 2026-10-28.
    #[test]
    fn query_decoding_matches_starlette() {
        let cases: [(&str, SubjectCheck); 12] = [
            ("subject=%ED%95%9C%EA%B8%80", valid("한글")),
            ("subject=a+b%20c%25d%2Be", valid("a b c%d+e")),
            ("subject=a&subject=b", valid("b")),
            ("subject=a&subject=", SubjectCheck::Blank),
            ("subject=&subject=z", valid("z")),
            ("&&subject=q&", valid("q")),
            ("subject=a=b", valid("a=b")),
            ("subject=%zz%4", valid("%zz%4")),
            ("subject=%FFx", valid("\u{fffd}x")),
            ("subject", SubjectCheck::Blank),
            ("x=1", SubjectCheck::Blank),
            ("Subject=a", SubjectCheck::Blank),
        ];
        for (q, want) in cases {
            assert_eq!(check_subject(Some(q)), want, "{q}");
        }
        assert_eq!(check_subject(None), SubjectCheck::Blank);
    }

    // Python str.strip() whitespace: the 29 code points `str.isspace()` accepts, incl. \x1c-\x1f.
    #[test]
    fn blank_means_python_strip_is_empty() {
        for q in [
            "subject=%1F",
            "subject=%1f%20",
            "subject=%E3%80%80",
            "subject=%C2%85",
            "subject=+%09%0A",
            "subject=%1C%1D%1E",
        ] {
            assert_eq!(check_subject(Some(q)), SubjectCheck::Blank, "{q}");
        }
        assert_eq!(check_subject(Some("subject=%C2%85x")), valid("\u{85}x"));
    }

    #[test]
    fn nul_is_its_own_verdict() {
        assert_eq!(check_subject(Some("subject=a%00b")), SubjectCheck::Nul);
        assert_eq!(check_subject(Some("subject=%00")), SubjectCheck::Nul);
    }
}
