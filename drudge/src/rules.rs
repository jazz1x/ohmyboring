//! `GET /rules` body — the owner's standing corrections, grouped by subject.
//! Same contract as `agents/door/rules.py::group_rules`: pure, rows in, struct out.
//! Struct field order is the JSON key order, so bytes match Python's insertion order.

use std::collections::BTreeMap;

use serde::Serialize;

/// Verbatim from `agents/door/rules.py::SQL` (`%s` -> `$1`, param "rule").
pub const SQL: &str = "select subject, predicate, value, source_path from claim \
    where kind = $1 and superseded_at is null \
    order by subject, valid_from desc";

/// One `claim` row, newest first per subject.
#[derive(Debug, Clone)]
pub struct RuleRow {
    pub subject: String,
    pub predicate: String,
    pub value: String,
    pub source_path: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Rule {
    pub subject: String,
    pub rule: String,
    pub trigger: String,
    pub source_path: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Rules {
    pub rules: Vec<Rule>,
    pub incomplete: usize,
}

pub fn group_rules(rows: Vec<RuleRow>) -> Rules {
    // (rule, source_path) and trigger; first seen = newest. BTreeMap<String> iterates in
    // byte order, which equals Python's code-point `sorted()`.
    type Entry = (Option<(String, String)>, Option<String>);
    let mut by_subject: BTreeMap<String, Entry> = BTreeMap::new();
    for r in rows {
        let entry = by_subject.entry(r.subject).or_default();
        match r.predicate.as_str() {
            "rule" if entry.0.is_none() => entry.0 = Some((r.value, r.source_path)),
            "trigger" if entry.1.is_none() => entry.1 = Some(r.value),
            _ => {}
        }
    }
    let mut rules = Vec::new();
    let mut incomplete = 0;
    for (subject, entry) in by_subject {
        match entry {
            (Some((rule, source_path)), Some(trigger)) => rules.push(Rule {
                subject,
                rule,
                trigger,
                source_path,
            }),
            _ => incomplete += 1,
        }
    }
    Rules { rules, incomplete }
}

/// Python `re.split(r"[|\s]+")` treats these as whitespace: `str.isspace()` also covers the
/// separators U+001C..U+001F, which Rust's `char::is_whitespace` does not.
fn is_separator(c: char) -> bool {
    c == '|' || c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Same as `agents/shared/rules_core.py::parse_trigger`: groups split on `" + "`, alternatives
/// on `|` or whitespace, empty alternatives dropped.
pub fn parse_trigger(trigger: &str) -> Vec<Vec<&str>> {
    trigger
        .split(" + ")
        .map(|group| {
            group
                .split(is_separator)
                .filter(|a| !a.is_empty())
                .collect()
        })
        .collect()
}

/// Same as `rules_core.py::fired`: the rules whose every group has an alternative that is a
/// case-insensitive substring of the prompt, in input order. A group with no alternative
/// (blank trigger included) never matches, so the rule never fires.
pub fn fired(rules: Vec<Rule>, prompt: &str) -> Vec<Rule> {
    let needle = prompt.to_lowercase();
    rules
        .into_iter()
        .filter(|r| {
            parse_trigger(&r.trigger)
                .iter()
                .all(|group| group.iter().any(|alt| needle.contains(&alt.to_lowercase())))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn rule(trigger: &str) -> Rule {
        Rule {
            subject: "s".into(),
            rule: "r".into(),
            trigger: trigger.into(),
            source_path: "x".into(),
        }
    }

    // Expected values: Python rules_core.fired on the same inputs (repo .venv, 2026-11-01).
    #[test]
    fn fired_matches_python_trigger_semantics() {
        let hermes = "hermes 헤르메스 + 제거|빼|내리|remove|drop";
        let hit = |t: &str, p: &str| fired(vec![rule(t)], p).len();
        assert_eq!(hit(hermes, "hermes 좀 봐줘"), 0);
        assert_eq!(hit(hermes, "제거해줘"), 0);
        assert_eq!(hit(hermes, "Hermes 좀 DROP 해줘"), 1);
        assert_eq!(hit("hermes + ", "hermes whatever"), 0);
        assert_eq!(hit("", "anything 제거"), 0);
        assert_eq!(hit("   ", "anything 제거"), 0);
        assert_eq!(hit("aaa bbb", "xx bbb yy"), 1);
        assert_eq!(hit("aaa|bbb", "xx aa yy"), 0);
        assert_eq!(hit("a\u{1c}b", "xx b"), 1, "U+001C splits like Python \\s");
    }

    #[test]
    fn fired_keeps_input_order() {
        let mut a = rule("x");
        a.subject = "a".into();
        let mut b = rule("x");
        b.subject = "b".into();
        let out = fired(vec![b, a], "X");
        assert_eq!(out[0].subject, "b");
        assert_eq!(out[1].subject, "a");
    }

    fn row(s: &str, p: &str, v: &str, f: &str) -> RuleRow {
        RuleRow {
            subject: s.into(),
            predicate: p.into(),
            value: v.into(),
            source_path: f.into(),
        }
    }

    fn body(rows: Vec<RuleRow>) -> String {
        serde_json::to_string(&group_rules(rows)).unwrap()
    }

    // Expected values: Python rules.group_rules + Starlette JSONResponse
    // (ensure_ascii=False, separators=(",", ":")) run with the repo .venv, 2026-10-28.
    #[test]
    fn subjects_sort_by_code_point_not_collation() {
        let rows = vec![
            row("b", "rule", "r1", "p_b"),
            row("b", "trigger", "t1", "f"),
            row("B", "rule", "r2", "p_B"),
            row("B", "trigger", "t2", "f"),
            row("가", "rule", "r3", "p_ga"),
            row("가", "trigger", "t3", "f"),
            row("a b", "rule", "r4", "p_ab"),
            row("a b", "trigger", "t4", "f"),
            row("_x", "rule", "r5", "p_x"),
            row("_x", "trigger", "t5", "f"),
        ];
        assert_eq!(
            body(rows),
            r#"{"rules":[{"subject":"B","rule":"r2","trigger":"t2","source_path":"p_B"},{"subject":"_x","rule":"r5","trigger":"t5","source_path":"p_x"},{"subject":"a b","rule":"r4","trigger":"t4","source_path":"p_ab"},{"subject":"b","rule":"r1","trigger":"t1","source_path":"p_b"},{"subject":"가","rule":"r3","trigger":"t3","source_path":"p_ga"}],"incomplete":0}"#
        );
    }

    #[test]
    fn newest_duplicate_wins_and_unrelated_predicate_is_ignored() {
        let rows = vec![
            row("s", "rule", "new", "p_new"),
            row("s", "rule", "old", "p_old"),
            row("s", "trigger", "tnew", "f"),
            row("s", "trigger", "told", "f"),
            row("s", "note", "ignored", "f"),
        ];
        assert_eq!(
            body(rows),
            r#"{"rules":[{"subject":"s","rule":"new","trigger":"tnew","source_path":"p_new"}],"incomplete":0}"#
        );
    }

    #[test]
    fn half_pairs_and_unrelated_only_subjects_count_as_incomplete() {
        let rows = vec![
            row("a", "rule", "only", "f"),
            row("b", "trigger", "only", "f"),
            row("c", "note", "x", "f"),
            row("d", "rule", "r", "pd"),
            row("d", "trigger", "t", "f"),
        ];
        assert_eq!(
            body(rows),
            r#"{"rules":[{"subject":"d","rule":"r","trigger":"t","source_path":"pd"}],"incomplete":3}"#
        );
    }

    #[test]
    fn korean_emoji_and_escapes_stay_raw() {
        let rows = vec![
            row("한글", "rule", "\"따옴표\" \\ 줄\n바꿈 🙂", "경로/한글.md"),
            row("한글", "trigger", "트리거 😀, 쉼표", "f"),
        ];
        assert_eq!(
            body(rows),
            r#"{"rules":[{"subject":"한글","rule":"\"따옴표\" \\ 줄\n바꿈 🙂","trigger":"트리거 😀, 쉼표","source_path":"경로/한글.md"}],"incomplete":0}"#
        );
    }

    #[test]
    fn empty_rows() {
        assert_eq!(body(vec![]), r#"{"rules":[],"incomplete":0}"#);
    }
}
