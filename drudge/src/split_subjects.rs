//! `/repairs/split-subjects` — claim subjects that differ only in spelling, grouped by `ingest::canon`.
//! Same wire as the Python door (`agents/door/door.py` `_repairs_split_subjects_*`): rows in, structs
//! out, struct field order is the JSON key order.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

use crate::approved::py_int;
use crate::claim_source::{all_params, is_py_space, last_param_opt};
use crate::ingest::canon;

/// Live claim rows, one `(subject, source_path)` per claim.
pub const SUBJECTS_SQL: &str = "select subject, source_path from claim where superseded_at is null";

pub const DEFAULT_LIMIT: usize = 3;
pub const LIMIT_ERROR: &str = "limit must be an integer 1..50";
pub const VARIANT_ERROR: &str = "variant must be given 1..10 times";
const MIN_LIMIT: i128 = 1;
const MAX_LIMIT: i128 = 50;
const MAX_VARIANTS: usize = 10;
pub const SAMPLE_LIMIT: i64 = 5;
pub const SAMPLE_VALUE_CHARS: i32 = 160;

/// `left(value, 160)` of the longest value, per predicate, most rows first.
pub const SAMPLES_SQL: &str = "select predicate, count(*), \
    (array_agg(left(value, $1::int) order by length(value) desc, value))[1] \
    from claim where subject = any($2) and superseded_at is null \
    group by predicate order by 2 desc, 1 limit $3::bigint";

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Group {
    pub subject: String,
    pub variants: Vec<String>,
    pub rows: i64,
    pub notes: usize,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Groups {
    pub groups: Vec<Group>,
    pub total_groups: usize,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Sample {
    pub predicate: String,
    pub rows: i64,
    pub value: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Samples {
    pub samples: Vec<Sample>,
}

/// What the `variant` query values turned out to be.
#[derive(Debug, PartialEq, Eq)]
pub enum Variants {
    Valid(Vec<String>),
    /// Not 1..=10 non-blank values: the 400.
    WrongCount,
    /// A value holds NUL: Postgres refuses it and the Python door answers a bare 500.
    Nul,
}

/// Buckets by `ingest::canon`; a bucket with 2+ distinct raw spellings is a repair candidate.
/// Most rows first; a tie goes to the smaller canon in code point order (Rust `str` order is the
/// UTF-8 byte order, which is code point order).
pub fn group_split_subjects(rows: &[(String, String)]) -> Vec<Group> {
    type Bucket<'a> = (BTreeMap<&'a str, i64>, BTreeSet<&'a str>);
    let mut buckets: BTreeMap<String, Bucket<'_>> = BTreeMap::new();
    for (subject, source_path) in rows {
        let (spellings, notes) = buckets.entry(canon(subject)).or_default();
        *spellings.entry(subject).or_insert(0) += 1;
        notes.insert(source_path);
    }
    let mut groups: Vec<Group> = buckets
        .into_iter()
        .filter(|(_, (spellings, _))| spellings.len() >= 2)
        .map(|(subject, (spellings, notes))| Group {
            subject,
            rows: spellings.values().sum(),
            notes: notes.len(),
            variants: spellings.into_keys().map(str::to_owned).collect(),
        })
        .collect();
    groups.sort_by(|a, b| b.rows.cmp(&a.rows).then_with(|| a.subject.cmp(&b.subject)));
    groups
}

/// `limit` as the door reads it: absent means 3, otherwise Python `int()` then 1..=50.
pub fn parse_limit(raw_query: Option<&str>) -> Result<usize, &'static str> {
    let Some(raw) = last_param_opt(raw_query, "limit") else {
        return Ok(DEFAULT_LIMIT);
    };
    py_int(&raw)
        .filter(|n| (MIN_LIMIT..=MAX_LIMIT).contains(n))
        .and_then(|n| usize::try_from(n).ok())
        .ok_or(LIMIT_ERROR)
}

/// Every `variant` value, in order, minus the blank ones (`str.strip()` empty); the kept values go
/// to SQL as sent. Starlette `getlist("variant")`.
pub fn parse_variants(raw_query: Option<&str>) -> Variants {
    let kept: Vec<String> = all_params(raw_query, "variant")
        .into_iter()
        .filter(|v| !v.trim_matches(is_py_space).is_empty())
        .collect();
    if !(1..=MAX_VARIANTS).contains(&kept.len()) {
        return Variants::WrongCount;
    }
    if kept.iter().any(|v| v.contains('\0')) {
        return Variants::Nul;
    }
    Variants::Valid(kept)
}

pub fn page(groups: Vec<Group>, limit: usize) -> Groups {
    let total_groups = groups.len();
    Groups {
        groups: groups.into_iter().take(limit).collect(),
        total_groups,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(subject: &str, path: &str) -> (String, String) {
        (subject.to_owned(), path.to_owned())
    }

    // d06f3bf on the door: rows descending, then canon code point order — 'mid' < 'zeta' < 'éclair'.
    #[test]
    fn tied_groups_order_by_canon_code_point() {
        let rows = vec![
            row("éclair", "a"),
            row("Éclair", "b"),
            row("zeta", "c"),
            row("Zeta", "d"),
            row("mid", "e"),
            row("Mid", "f"),
            row("big", "g"),
            row("Big", "h"),
            row("BIG", "i"),
        ];
        let order: Vec<String> = group_split_subjects(&rows)
            .into_iter()
            .map(|g| g.subject)
            .collect();
        assert_eq!(order, ["big", "mid", "zeta", "éclair"]);
    }

    // Python int() on the limit edge inputs, measured on the door (fixture split_subjects_python.json).
    #[test]
    fn limit_reads_like_python_int() {
        let ok = |q| parse_limit(Some(q));
        assert_eq!(parse_limit(None), Ok(3));
        assert_eq!(ok("limit=%203%20"), Ok(3));
        assert_eq!(ok("limit=%2B3"), Ok(3));
        assert_eq!(ok("limit=+3"), Ok(3));
        assert_eq!(ok("limit=%D9%A3"), Ok(3));
        assert_eq!(ok("limit=1_0"), Ok(10));
        assert_eq!(ok("limit=1&limit=2"), Ok(2));
        for bad in [
            "limit=0",
            "limit=51",
            "limit=",
            "limit=2.0",
            "limit=-1",
            "limit=abc",
        ] {
            assert_eq!(ok(bad), Err(LIMIT_ERROR), "{bad}");
        }
    }

    #[test]
    fn variants_keep_repeats_and_the_raw_text_but_drop_blanks() {
        assert_eq!(
            parse_variants(Some("variant=%20&variant=%20a&variant=a")),
            Variants::Valid(vec![" a".to_owned(), "a".to_owned()])
        );
        assert_eq!(parse_variants(Some("variant")), Variants::WrongCount);
        assert_eq!(parse_variants(Some("variant=a%00b")), Variants::Nul);
        let eleven = (0..11)
            .map(|i| format!("variant=v{i}"))
            .collect::<Vec<_>>()
            .join("&");
        assert_eq!(parse_variants(Some(&eleven)), Variants::WrongCount);
    }
}
