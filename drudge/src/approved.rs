//! `GET /approved` — what the morning card's sessions judged, read from `session:slack:…` edges.
//! Same contract as `agents/door/approved.py` (`payload`, `select_approved`): pure, rows and a
//! clock in, a struct out. Struct field order is the JSON key order, so bytes match Python.
//!
//! Python's `int()` / `float()` / `datetime.fromtimestamp` decide which session names count,
//! so the small parsers below copy their grammar instead of approximating it.

use chrono::DateTime;
use serde::Serialize;

/// `int()` / `float()` strip ASCII whitespace plus non-ASCII White_Space, but not the
/// `\x1c..\x1f` that `str.strip()` also eats (measured: `int("\x1c5\x1f")` raises).
fn is_num_space(c: char) -> bool {
    if c.is_ascii() {
        matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r')
    } else {
        c.is_whitespace()
    }
}

/// Verbatim from `agents/door/door.py::_EDGES_SQL`.
pub const SQL: &str = "select src, dst, kind from edge where src like 'session:slack:%' and kind in ('used', 'contested')";

const SINCE_HOURS_MIN: i128 = 1;
const SINCE_HOURS_MAX: i128 = 24 * 365;
const SINCE_HOURS_ERROR: &str = "since_hours must be an integer in [1, 8760]";
const MICROS: i64 = 1_000_000;

/// Seconds `datetime.fromtimestamp(ts, tz=Asia/Seoul)` accepts (after rounding to µs):
/// year 1 UTC .. 9999-12-31T23:59:59+09:00. Measured on CPython 3.14, 2026-10-28.
const TS_MIN_SECS: f64 = -62_135_596_800.0;
const TS_MAX_SECS: f64 = 253_402_268_399.0;

/// First code point of each run of ten Unicode `Nd` decimal digits (Unicode 16, as CPython 3.14).
const DIGIT_ZEROS: [u32; 76] = [
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
    0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
    0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10,
    0x104a0, 0x10d30, 0x10d40, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
    0x11650, 0x116c0, 0x116d0, 0x116da, 0x11730, 0x118e0, 0x11950, 0x11bf0, 0x11c50, 0x11d50,
    0x11da0, 0x11f50, 0x16130, 0x16a60, 0x16ac0, 0x16b50, 0x16d70, 0x1ccf0, 0x1d7ce, 0x1d7d8,
    0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e5f1, 0x1e950, 0x1fbf0,
];

/// Python `unicodedata.decimal(c)`: the value of a Unicode decimal digit, if it is one.
pub(crate) fn decimal_value(c: char) -> Option<u32> {
    let cp = u32::from(c);
    DIGIT_ZEROS
        .iter()
        .find(|zero| (**zero..**zero + 10).contains(&cp))
        .map(|zero| cp - zero)
}

/// Python `int(s)` for a `str`: surrounding whitespace, one sign, Unicode decimal digits, single
/// underscores strictly between digits. Digits beyond i128 saturate (Python would also be out of range).
pub fn py_int(raw: &str) -> Option<i128> {
    let trimmed = raw.trim_matches(is_num_space);
    let (negative, body) = match trimmed.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let mut value: i128 = 0;
    let mut after_digit = false;
    for c in body.chars() {
        if c == '_' {
            if !after_digit {
                return None;
            }
            after_digit = false;
        } else {
            let digit = decimal_value(c)?;
            value = value.saturating_mul(10).saturating_add(i128::from(digit));
            after_digit = true;
        }
    }
    after_digit.then_some(if negative { -value } else { value })
}

/// Python `float(s)` for a `str`: whitespace strip, Unicode digits to ASCII, underscores only
/// between digits, then the usual decimal / `inf` / `infinity` / `nan` grammar (any case, signed).
pub fn py_float(raw: &str) -> Option<f64> {
    let trimmed = raw.trim_matches(is_num_space);
    let mut ascii = String::with_capacity(trimmed.len());
    for c in trimmed.chars() {
        if c.is_ascii() {
            ascii.push(c);
        } else {
            ascii.push(char::from_digit(decimal_value(c)?, 10)?);
        }
    }
    let bytes = ascii.as_bytes();
    let underscores_ok = bytes.iter().enumerate().all(|(i, b)| {
        *b != b'_'
            || (i > 0
                && bytes.get(i + 1).is_some_and(u8::is_ascii_digit)
                && bytes[i - 1].is_ascii_digit())
    });
    if !underscores_ok {
        return None;
    }
    ascii.replace('_', "").parse::<f64>().ok()
}

/// `since_hours` from the raw query: missing means "24" (Starlette `get("since_hours", "24")`),
/// anything else must be a Python-`int()` integer in [1, 8760].
pub fn check_since_hours(raw_query: Option<&str>) -> Result<i64, &'static str> {
    let raw = crate::claim_source::last_param_opt(raw_query, "since_hours")
        .unwrap_or_else(|| "24".to_string());
    match py_int(&raw) {
        Some(hours) if (SINCE_HOURS_MIN..=SINCE_HOURS_MAX).contains(&hours) => {
            i64::try_from(hours).map_err(|_| SINCE_HOURS_ERROR)
        }
        _ => Err(SINCE_HOURS_ERROR),
    }
}

#[derive(Debug, Clone)]
pub struct EdgeRow {
    pub src: String,
    pub dst: String,
    pub kind: String,
}

/// What a session name's tail turned out to be.
#[derive(Debug, PartialEq, Eq)]
enum Ts {
    NotNumber,
    OutOfRange,
    /// Microseconds since the Unix epoch.
    At(i64),
}

/// `datetime.fromtimestamp(ts)` rounding: split off the fraction, round it to µs half-even,
/// carry into the seconds — then the seconds must be a date Python can hold.
fn ts_from_float(ts: f64) -> Ts {
    if !ts.is_finite() {
        return Ts::OutOfRange;
    }
    let mut secs = ts.trunc();
    let mut micros = ((ts - secs) * 1e6).round_ties_even();
    if micros >= 1e6 {
        micros -= 1e6;
        secs += 1.0;
    } else if micros < 0.0 {
        micros += 1e6;
        secs -= 1.0;
    }
    if !(TS_MIN_SECS..=TS_MAX_SECS).contains(&secs) {
        return Ts::OutOfRange;
    }
    // secs is a whole number inside ±3e11 and micros inside [0, 1e6): both casts are exact.
    #[allow(clippy::cast_possible_truncation)]
    Ts::At(secs as i64 * MICROS + micros as i64)
}

fn parse_session_ts(name: &str) -> Ts {
    let tail = name.rsplit(':').next().unwrap_or(name);
    py_float(tail).map_or(Ts::NotNumber, ts_from_float)
}

/// Only instants inside [now − since_hours, now] (≤ 1 year back) are printed, and Seoul has been
/// a fixed +09:00 there, so a fixed offset renders them as `datetime.isoformat(timespec="seconds")`.
/// `At` is bounded to years 1..9999, which chrono always holds, so the `None` arm is unreachable.
fn iso_seoul(at_micros: i64) -> String {
    DateTime::from_timestamp(at_micros.div_euclid(MICROS) + 9 * 3600, 0)
        .map(|local| format!("{}+09:00", local.format("%Y-%m-%dT%H:%M:%S")))
        .unwrap_or_default()
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Entry {
    pub session: String,
    pub note: String,
    pub at: String,
}

/// Field order is the wire order. `skipped_rows` counts skipped edge rows (duplicates included);
/// which rows they were goes to the server log, not the wire.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ApprovedPayload {
    pub since_hours: i64,
    pub approved: Vec<Entry>,
    pub contested: Vec<Entry>,
    pub skipped_rows: usize,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Skipped {
    pub session: String,
    pub reason: &'static str,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Selection {
    pub payload: ApprovedPayload,
    pub skipped: Vec<Skipped>,
}

struct Pick {
    at: i64,
    session: String,
    note: String,
}

/// `at` descending, then session ascending, then note ascending (code point = UTF-8 byte order).
fn into_entries(mut picks: Vec<Pick>) -> Vec<Entry> {
    picks.sort_by(|a, b| {
        b.at.cmp(&a.at)
            .then_with(|| a.session.cmp(&b.session))
            .then_with(|| a.note.cmp(&b.note))
    });
    picks
        .into_iter()
        .map(|p| Entry {
            at: iso_seoul(p.at),
            session: p.session,
            note: p.note,
        })
        .collect()
}

/// Partition edge rows into approved / contested inside `[now − since_hours, now]`, compared at
/// µs precision. `now_micros` is the clock seam: the handler passes the current time.
pub fn select_approved(rows: &[EdgeRow], since_hours: i64, now_micros: i64) -> Selection {
    let cutoff = now_micros - since_hours * 3_600 * MICROS;
    let mut approved = Vec::new();
    let mut contested = Vec::new();
    let mut skipped = Vec::new();
    for row in rows {
        let name = row.src.strip_prefix("session:").unwrap_or(&row.src);
        if !name.starts_with("slack:") {
            skipped.push(Skipped {
                session: row.src.clone(),
                reason: "not a slack card session",
            });
            continue;
        }
        let at = match parse_session_ts(name) {
            Ts::NotNumber => {
                skipped.push(Skipped {
                    session: row.src.clone(),
                    reason: "no parseable ts in session name",
                });
                continue;
            }
            Ts::OutOfRange => {
                skipped.push(Skipped {
                    session: row.src.clone(),
                    reason: "session ts out of range",
                });
                continue;
            }
            Ts::At(at) => at,
        };
        if !(cutoff..=now_micros).contains(&at) {
            continue;
        }
        let pick = Pick {
            at,
            session: name.to_string(),
            note: row.dst.clone(),
        };
        match row.kind.as_str() {
            "used" => approved.push(pick),
            "contested" => contested.push(pick),
            _ => {}
        }
    }
    Selection {
        payload: ApprovedPayload {
            since_hours,
            approved: into_entries(approved),
            contested: into_entries(contested),
            skipped_rows: skipped.len(),
        },
        skipped,
    }
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::unreadable_literal,
        clippy::type_complexity
    )]
    use super::*;

    // Expected values below come from running agents/door/approved.py (`payload`, `check_since_hours`,
    // `int`, `float`) with Starlette's JSON rendering and QueryParams on CPython 3.14, 2026-10-28.
    const HUGE: i64 = i64::MAX;

    const NOW_US: i64 = 1_793_156_400_250_000;
    const ORACLE: &[(&str, &[(&str, &str, &str)], i64, &str)] = &[
        (
            "window_edges",
            &[
                ("session:slack:C1:1793070000.25", "/at-cutoff.md", "used"),
                (
                    "session:slack:C1:1793070000.249999",
                    "/before-cutoff.md",
                    "used",
                ),
                ("session:slack:C1:1793156400.25", "/at-now.md", "contested"),
                (
                    "session:slack:C1:1793156400.250001",
                    "/after-now.md",
                    "contested",
                ),
                ("session:slack:C1:1793156340.5", "/inside.md", "used"),
            ],
            24,
            r#"{"since_hours":24,"approved":[{"session":"slack:C1:1793156340.5","note":"/inside.md","at":"2026-10-28T11:59:00+09:00"},{"session":"slack:C1:1793070000.25","note":"/at-cutoff.md","at":"2026-10-27T12:00:00+09:00"}],"contested":[{"session":"slack:C1:1793156400.25","note":"/at-now.md","at":"2026-10-28T12:00:00+09:00"}],"skipped_rows":0}"#,
        ),
        (
            "rounding",
            &[
                ("session:slack:C1:1793156300.9999996", "/a.md", "used"),
                ("session:slack:C1:1793156200.9999994", "/b.md", "used"),
                ("session:slack:C1:1793156100.0000005", "/c.md", "used"),
                ("session:slack:C1:1793156000.0000015", "/d.md", "contested"),
            ],
            24,
            r#"{"since_hours":24,"approved":[{"session":"slack:C1:1793156300.9999996","note":"/a.md","at":"2026-10-28T11:58:21+09:00"},{"session":"slack:C1:1793156200.9999994","note":"/b.md","at":"2026-10-28T11:56:40+09:00"},{"session":"slack:C1:1793156100.0000005","note":"/c.md","at":"2026-10-28T11:55:00+09:00"}],"contested":[{"session":"slack:C1:1793156000.0000015","note":"/d.md","at":"2026-10-28T11:53:20+09:00"}],"skipped_rows":0}"#,
        ),
        (
            "ties",
            &[
                ("session:slack:C2:1793156340.5", "/b.md", "used"),
                ("session:slack:C1:1793156340.5", "/z.md", "used"),
                ("session:slack:C1:1793156340.5", "/a.md", "used"),
                ("session:slack:C1:1793156340.5", "/a.md", "contested"),
                ("session:slack:C2:1793156340.5", "/a.md", "contested"),
                ("session:slack:C1:1793156370", "/newer.md", "used"),
                ("session:slack:한글:1793156340.5", "/한.md", "used"),
            ],
            24,
            r#"{"since_hours":24,"approved":[{"session":"slack:C1:1793156370","note":"/newer.md","at":"2026-10-28T11:59:30+09:00"},{"session":"slack:C1:1793156340.5","note":"/a.md","at":"2026-10-28T11:59:00+09:00"},{"session":"slack:C1:1793156340.5","note":"/z.md","at":"2026-10-28T11:59:00+09:00"},{"session":"slack:C2:1793156340.5","note":"/b.md","at":"2026-10-28T11:59:00+09:00"},{"session":"slack:한글:1793156340.5","note":"/한.md","at":"2026-10-28T11:59:00+09:00"}],"contested":[{"session":"slack:C1:1793156340.5","note":"/a.md","at":"2026-10-28T11:59:00+09:00"},{"session":"slack:C2:1793156340.5","note":"/a.md","at":"2026-10-28T11:59:00+09:00"}],"skipped_rows":0}"#,
        ),
        (
            "non_finite",
            &[
                ("session:slack:C1:inf", "/x.md", "used"),
                ("session:slack:C1:-inf", "/x.md", "used"),
                ("session:slack:C1:nan", "/x.md", "used"),
                ("session:slack:C1:1e400", "/x.md", "used"),
                ("session:slack:C1:1e20", "/x.md", "used"),
                ("session:slack:C1:-1e20", "/x.md", "used"),
                ("session:slack:C1:Infinity", "/x.md", "used"),
                ("session:slack:C1:+NaN", "/x.md", "used"),
                ("session:slack:C1:253402268400", "/x.md", "used"),
                ("session:slack:C1:-62135596801", "/x.md", "used"),
                ("session:slack:C1:1793156395", "/ok.md", "used"),
            ],
            24,
            r#"{"since_hours":24,"approved":[{"session":"slack:C1:1793156395","note":"/ok.md","at":"2026-10-28T11:59:55+09:00"}],"contested":[],"skipped_rows":10}"#,
        ),
        (
            "numeric_forms",
            &[
                ("session:slack:C1: 1793156390.5 ", "/ws.md", "used"),
                ("session:slack:C1:179315638_0", "/us.md", "used"),
                ("session:slack:C1:1793156370e0", "/exp.md", "used"),
                ("session:slack:C1:1793156360.", "/dot.md", "used"),
                ("session:slack:C1:1_0", "/old.md", "used"),
                ("session:slack:C1: 1.5 ", "/old2.md", "used"),
                ("session:slack:C1:١٧٩٣١٥٦٣٥٠", "/arabic.md", "used"),
                (
                    "session:slack:C1:１７９３１５６３４０",
                    "/fullwidth.md",
                    "used",
                ),
                ("session:slack:C1:1__0", "/bad1.md", "used"),
                ("session:slack:C1:_10", "/bad2.md", "used"),
                ("session:slack:C1:10_", "/bad3.md", "used"),
                ("session:slack:C1:1_.5", "/bad4.md", "used"),
                ("session:slack:C1:abc", "/bad5.md", "used"),
                ("session:slack:C1:0x10", "/bad6.md", "used"),
                ("session:slack:C1:", "/bad7.md", "used"),
            ],
            24,
            r#"{"since_hours":24,"approved":[{"session":"slack:C1: 1793156390.5 ","note":"/ws.md","at":"2026-10-28T11:59:50+09:00"},{"session":"slack:C1:179315638_0","note":"/us.md","at":"2026-10-28T11:59:40+09:00"},{"session":"slack:C1:1793156370e0","note":"/exp.md","at":"2026-10-28T11:59:30+09:00"},{"session":"slack:C1:1793156360.","note":"/dot.md","at":"2026-10-28T11:59:20+09:00"},{"session":"slack:C1:١٧٩٣١٥٦٣٥٠","note":"/arabic.md","at":"2026-10-28T11:59:10+09:00"},{"session":"slack:C1:１７９３１５６３４０","note":"/fullwidth.md","at":"2026-10-28T11:59:00+09:00"}],"contested":[],"skipped_rows":7}"#,
        ),
        (
            "channel_and_foreign",
            &[
                ("session:slack:C1", "/a.md", "used"),
                ("slack:C1", "/b.md", "used"),
                ("session:uuid-1", "/c.md", "used"),
                ("session:uuid-1", "/c.md", "contested"),
                ("session:uuid-0", "/c.md", "used"),
                ("session:slack:", "/d.md", "used"),
                ("session:slack:C1:1793156395", "/other-kind.md", "seen"),
            ],
            24,
            r#"{"since_hours":24,"approved":[],"contested":[],"skipped_rows":6}"#,
        ),
        (
            "empty",
            &[],
            24,
            r#"{"since_hours":24,"approved":[],"contested":[],"skipped_rows":0}"#,
        ),
    ];
    const SINCE_ORACLE: &[(&str, Option<i64>)] = &[
        ("", Some(24)),
        ("since_hours=24", Some(24)),
        ("since_hours=%2024%20", Some(24)),
        ("since_hours=+24", Some(24)),
        ("since_hours=2_4", Some(24)),
        ("since_hours=%D9%A3", Some(3)),
        ("since_hours=%EF%BC%92%EF%BC%94", Some(24)),
        ("since_hours=0", None),
        ("since_hours=1", Some(1)),
        ("since_hours=8760", Some(8760)),
        ("since_hours=8761", None),
        ("since_hours=abc", None),
        ("since_hours=", None),
        ("since_hours=1&since_hours=48", Some(48)),
        ("since_hours=48&since_hours=abc", None),
        ("since_hours=2__4", None),
        ("since_hours=_24", None),
        ("since_hours=24_", None),
        ("since_hours=1.0", None),
        ("since_hours=-5", None),
        ("since_hours=%2B1_0", Some(10)),
        ("since_hours=%E3%80%8024%E3%80%80", Some(24)),
        ("since_hours=%1C24%1F", None),
        (
            "since_hours=99999999999999999999999999999999999999999999999",
            None,
        ),
        ("since_hours=%C2%A024", Some(24)),
        ("since_hours", None),
        ("other=1", Some(24)),
        ("since_hours=0_0", None),
        ("since_hours=0%2024", None),
    ];
    const FLOAT_ORACLE: &[(&str, Option<u64>)] = &[
        ("1758470000.5", Some(0x41da3407dc200000)),
        ("  1.5  ", Some(0x3ff8000000000000)),
        ("1_0", Some(0x4024000000000000)),
        ("1__0", None),
        ("_1", None),
        ("1_", None),
        ("1_.5", None),
        ("1._5", None),
        ("1e5", Some(0x40f86a0000000000)),
        ("1E5", Some(0x40f86a0000000000)),
        ("1e+5", Some(0x40f86a0000000000)),
        ("1e-5", Some(0x3ee4f8b588e368f1)),
        ("1e5_0", Some(0x4a511b0ec57e649a)),
        ("1e_5", None),
        (".5", Some(0x3fe0000000000000)),
        ("5.", Some(0x4014000000000000)),
        ("+.5", Some(0x3fe0000000000000)),
        ("-.5", Some(0xbfe0000000000000)),
        (".", None),
        ("", None),
        ("e5", None),
        ("inf", Some(0x7ff0000000000000)),
        ("-inf", Some(0xfff0000000000000)),
        ("+Inf", Some(0x7ff0000000000000)),
        ("infinity", Some(0x7ff0000000000000)),
        ("-INFINITY", Some(0xfff0000000000000)),
        ("nan", Some(0x7ff8000000000000)),
        ("-nan", Some(0xfff8000000000000)),
        ("NaN", Some(0x7ff8000000000000)),
        ("infin", None),
        ("1e400", Some(0x7ff0000000000000)),
        ("-1e400", Some(0xfff0000000000000)),
        ("1e-400", Some(0x0000000000000000)),
        ("0x10", None),
        ("1 5", None),
        ("\u{a0}1\u{3000}", Some(0x3ff0000000000000)),
        ("\u{1c}1\u{1f}", None),
        ("٣.٥", Some(0x400c000000000000)),
        ("１０", Some(0x4024000000000000)),
        ("1,5", None),
        ("１", Some(0x3ff0000000000000)),
        ("٣e٢", Some(0x4072c00000000000)),
        ("1_0e1_0", Some(0x42374876e8000000)),
        ("0_0", Some(0x0000000000000000)),
        ("00012", Some(0x4028000000000000)),
        ("1.e2", Some(0x4059000000000000)),
        ("+", None),
        ("-", None),
        ("1e", None),
        ("++1", None),
        ("1\u{0}", None),
    ];
    const INT_ORACLE: &[(&str, Option<i64>)] = &[
        (" 24 ", Some(24)),
        ("+24", Some(24)),
        ("-24", Some(-24)),
        ("2_4", Some(24)),
        ("٣", Some(3)),
        ("２４", Some(24)),
        ("2__4", None),
        ("_24", None),
        ("24_", None),
        ("", None),
        ("1.0", None),
        ("  ", None),
        ("+", None),
        ("-", None),
        ("+ 1", None),
        ("1 2", None),
        ("0", Some(0)),
        ("007", Some(7)),
        ("0_0", Some(0)),
        ("1_000", Some(1000)),
        ("\u{a0}5\u{3000}", Some(5)),
        ("\u{1c}5\u{1f}", None),
        (
            "99999999999999999999999999999999999999999999999999",
            Some(HUGE),
        ),
        (
            "-99999999999999999999999999999999999999999999999999",
            Some(-HUGE),
        ),
        ("१२", Some(12)),
        ("1e3", None),
        ("0x1", None),
    ];

    fn rows(raw: &[(&str, &str, &str)]) -> Vec<EdgeRow> {
        raw.iter()
            .map(|(src, dst, kind)| EdgeRow {
                src: (*src).into(),
                dst: (*dst).into(),
                kind: (*kind).into(),
            })
            .collect()
    }

    #[test]
    fn payload_bytes_match_python() {
        for (name, raw, hours, want) in ORACLE {
            let got = select_approved(&rows(raw), *hours, NOW_US).payload;
            assert_eq!(serde_json::to_string(&got).unwrap(), *want, "{name}");
        }
    }

    #[test]
    fn input_order_does_not_change_the_bytes() {
        for (name, raw, hours, want) in ORACLE {
            let mut reversed = rows(raw);
            reversed.reverse();
            let got = select_approved(&reversed, *hours, NOW_US).payload;
            assert_eq!(serde_json::to_string(&got).unwrap(), *want, "{name}");
        }
    }

    // 1/128 s = 7812.5 µs is an exact f64 tie. Half-even gives 7812 (<= now, inside); half-up
    // gives 7813 (> now, outside). Expected bytes from Python approved.payload(), 2026-10-28.
    #[test]
    fn half_microsecond_tie_rounds_to_even_at_the_window_end() {
        let raw = [("session:slack:C1:1793156400.0078125", "/tie.md", "used")];
        let now = 1_793_156_400_007_812;
        let got = select_approved(&rows(&raw), 24, now).payload;
        assert_eq!(
            serde_json::to_string(&got).unwrap(),
            r#"{"since_hours":24,"approved":[{"session":"slack:C1:1793156400.0078125","note":"/tie.md","at":"2026-10-28T12:00:00+09:00"}],"contested":[],"skipped_rows":0}"#
        );
    }

    #[test]
    fn skipped_rows_are_reported_with_reasons() {
        let raw = [
            ("session:uuid-1", "/c.md", "used"),
            ("session:slack:C1:inf", "/c.md", "used"),
            ("session:slack:C1", "/c.md", "used"),
        ];
        let got = select_approved(&rows(&raw), 24, NOW_US);
        let pairs: Vec<_> = got
            .skipped
            .iter()
            .map(|s| (s.session.as_str(), s.reason))
            .collect();
        assert_eq!(
            pairs,
            [
                ("session:uuid-1", "not a slack card session"),
                ("session:slack:C1:inf", "session ts out of range"),
                ("session:slack:C1", "no parseable ts in session name"),
            ]
        );
        assert_eq!(got.payload.skipped_rows, 3);
    }

    #[test]
    fn since_hours_matches_python() {
        for (query, want) in SINCE_ORACLE {
            let got = check_since_hours(Some(*query).filter(|q| !q.is_empty())).ok();
            assert_eq!(got, *want, "{query:?}");
        }
    }

    #[test]
    fn py_float_matches_python() {
        for (text, want) in FLOAT_ORACLE {
            let got = py_float(text);
            match want {
                None => assert!(got.is_none(), "{text:?} -> {got:?}"),
                Some(bits) if f64::from_bits(*bits).is_nan() => {
                    assert!(got.is_some_and(f64::is_nan), "{text:?} -> {got:?}");
                }
                Some(bits) => assert_eq!(got.map(f64::to_bits), Some(*bits), "{text:?}"),
            }
        }
    }

    #[test]
    fn py_int_matches_python() {
        for (text, want) in INT_ORACLE {
            let got =
                py_int(text).map(|v| i64::try_from(v).unwrap_or(if v > 0 { HUGE } else { -HUGE }));
            assert_eq!(got, *want, "{text:?}");
        }
    }
}
