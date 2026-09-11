use std::collections::HashSet;
use std::fmt;

const SOURCE_EXTS: [&str; 12] = [
    "rs", "py", "ts", "tsx", "js", "go", "sh", "sql", "toml", "yaml", "yml", "md",
];

const ANCHOR_CAP: usize = 20;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LineSpan {
    pub start: u32,
    pub end: u32,
}

impl fmt::Display for LineSpan {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if self.end > self.start {
            write!(f, "L{}-L{}", self.start, self.end)
        } else {
            write!(f, "L{}", self.start)
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Anchor {
    pub project: String,
    pub path: String,
    pub span: Option<LineSpan>,
}

impl Anchor {
    pub fn to_db_string(&self) -> String {
        match &self.span {
            Some(span) => format!("{}:{}:{}", self.project, self.path, span),
            None => format!("{}:{}", self.project, self.path),
        }
    }

    pub fn from_db_string(db: &str) -> Option<Anchor> {
        let (project, rest) = db.split_once(':')?;
        let (path, span) = match rest.rsplit_once(':') {
            Some((path, span)) => (path, Some(parse_db_span(span)?)),
            None => (rest, None),
        };
        if project.is_empty() || path.is_empty() {
            return None;
        }
        Some(Anchor {
            project: project.to_owned(),
            path: path.to_owned(),
            span,
        })
    }
}

fn parse_db_span(span: &str) -> Option<LineSpan> {
    let span = span.strip_prefix('L')?;
    if let Some((start, end)) = span.split_once("-L") {
        return Some(LineSpan {
            start: start.parse().ok()?,
            end: end.parse().ok()?,
        });
    }
    let line: u32 = span.parse().ok()?;
    Some(LineSpan {
        start: line,
        end: line,
    })
}

pub fn anchor_for_claim(note_anchors: &[Anchor], subject: &str, value: &str) -> Option<Anchor> {
    let first = note_anchors.first()?;
    let haystack = format!("{} {}", subject.to_lowercase(), value.to_lowercase());
    let matched = note_anchors.iter().find(|a| {
        haystack.contains(&a.path.to_lowercase()) || {
            let stem = file_stem(&a.path).to_lowercase();
            !stem.is_empty() && haystack.contains(&stem)
        }
    });
    Some(matched.unwrap_or(first).clone())
}

fn file_stem(path: &str) -> &str {
    let file = path.rsplit('/').next().unwrap_or(path);
    file.split_once('.').map_or(file, |(stem, _)| stem)
}

pub fn from_note_body(project: &str, body: &str) -> Vec<Anchor> {
    let mut found: Vec<(usize, Anchor)> = t1_mentions(project, body);
    found.extend(t1b_mentions(project, body));
    found.sort_by_key(|(pos, _)| *pos);

    let mut seen: HashSet<String> = HashSet::new();
    let mut out = Vec::new();
    for (_, anchor) in found {
        if seen.insert(anchor.to_db_string()) {
            out.push(anchor);
            if out.len() >= ANCHOR_CAP {
                break;
            }
        }
    }
    out
}

fn t1_mentions(project: &str, body: &str) -> Vec<(usize, Anchor)> {
    let bytes = body.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i + 1 < bytes.len() {
        if bytes[i] == b':'
            && bytes[i + 1].is_ascii_digit()
            && let Some((start, anchor, resume)) = t1_at(project, body, i)
        {
            out.push((start, anchor));
            i = resume;
            continue;
        }
        i += 1;
    }
    out
}

fn t1_at(project: &str, body: &str, colon: usize) -> Option<(usize, Anchor, usize)> {
    let bytes = body.as_bytes();
    let mut start = colon;
    while start > 0 && is_path_char(bytes[start - 1]) {
        start -= 1;
    }
    if start > 0 && bytes[start - 1] == b':' && bytes.get(start) == Some(&b'/') {
        return None;
    }
    let path = normalize_path(&body[start..colon])?;
    if !(path.contains('/') || has_source_ext(&path))
        || path.starts_with('~')
        || path.starts_with("/tmp")
    {
        return None;
    }
    let (span, resume) = parse_span(bytes, colon + 1)?;
    Some((
        start,
        Anchor {
            project: project.to_owned(),
            path,
            span: Some(span),
        },
        resume,
    ))
}

fn parse_span(bytes: &[u8], from: usize) -> Option<(LineSpan, usize)> {
    let digits_end = |from: usize| {
        let mut e = from;
        while e < bytes.len() && bytes[e].is_ascii_digit() {
            e += 1;
        }
        e
    };
    let end = digits_end(from);
    if end == from {
        return None;
    }
    let start: u32 = body_num(bytes, from, end)?;
    if bytes.get(end) == Some(&b'-') {
        let end2 = digits_end(end + 1);
        if end2 > end + 1 {
            let to: u32 = body_num(bytes, end + 1, end2)?;
            return Some((LineSpan { start, end: to }, end2));
        }
    }
    Some((LineSpan { start, end: start }, end))
}

fn body_num(bytes: &[u8], from: usize, to: usize) -> Option<u32> {
    std::str::from_utf8(bytes.get(from..to)?).ok()?.parse().ok()
}

fn t1b_mentions(project: &str, body: &str) -> Vec<(usize, Anchor)> {
    let mut out = Vec::new();
    for (offset, token) in split_tokens(body) {
        let token = trim_decoration(token);
        if token.contains(':') || !token.contains('/') || !has_source_ext(token) {
            continue;
        }
        if token.starts_with('~') || token.starts_with("/tmp") || token.contains("://") {
            continue;
        }
        if let Some(path) = normalize_path(token) {
            out.push((
                offset,
                Anchor {
                    project: project.to_owned(),
                    path,
                    span: None,
                },
            ));
        }
    }
    out
}

fn split_tokens(body: &str) -> Vec<(usize, &str)> {
    body.split_whitespace()
        .map(|t| (t.as_ptr() as usize - body.as_ptr() as usize, t))
        .collect()
}

fn trim_decoration(token: &str) -> &str {
    let open = |c: char| matches!(c, '`' | '"' | '\'' | '(' | '[' | '{');
    let close = |c: char| {
        matches!(
            c,
            '`' | '"' | '\'' | ')' | ']' | '}' | '.' | ',' | ';' | ':' | '!' | '?'
        )
    };
    token.trim_start_matches(open).trim_end_matches(close)
}

fn normalize_path(path: &str) -> Option<String> {
    let mut p = path;
    while let Some(rest) = p.strip_prefix("./") {
        p = rest;
    }
    if p.is_empty() {
        None
    } else {
        Some(p.to_owned())
    }
}

fn has_source_ext(path: &str) -> bool {
    let Some((_, ext)) = path.rsplit_once('.') else {
        return false;
    };
    SOURCE_EXTS.contains(&ext.to_ascii_lowercase().as_str())
}

fn is_path_char(b: u8) -> bool {
    matches!(b, b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'-' | b'.' | b'/' | b'~')
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::{Anchor, LineSpan, anchor_for_claim, from_note_body};

    const PROJECT: &str = "ohmyboring";

    fn anchor(body: &str) -> Vec<Anchor> {
        from_note_body(PROJECT, body)
    }

    fn a(path: &str, span: impl Into<Option<LineSpan>>) -> Anchor {
        Anchor {
            project: PROJECT.to_owned(),
            path: path.to_owned(),
            span: span.into(),
        }
    }

    fn ls(start: u32, end: u32) -> LineSpan {
        LineSpan { start, end }
    }

    #[test]
    fn t1_path_line() {
        let got = anchor("see agents/shared/uptake_core.py:283 for the uptake loop");
        assert_eq!(got, vec![a("agents/shared/uptake_core.py", ls(283, 283))]);
        assert_eq!(
            got[0].to_db_string(),
            "ohmyboring:agents/shared/uptake_core.py:L283"
        );
    }

    #[test]
    fn t1_path_line_range() {
        let got = anchor("drudge/src/store.rs:1212-1240 upserts the claim");
        assert_eq!(got, vec![a("drudge/src/store.rs", ls(1212, 1240))]);
        assert_eq!(
            got[0].to_db_string(),
            "ohmyboring:drudge/src/store.rs:L1212-L1240"
        );
    }

    #[test]
    fn t1_backticked() {
        let got = anchor("the fix landed in `drudge/src/store.rs:1212` yesterday");
        assert_eq!(got, vec![a("drudge/src/store.rs", ls(1212, 1212))]);
    }

    #[test]
    fn t1_korean_sentence_with_backticked_path_line() {
        let got = anchor("오류는 `drudge/src/ingest.rs:88`에서 시작됐다");
        assert_eq!(got, vec![a("drudge/src/ingest.rs", ls(88, 88))]);
    }

    #[test]
    fn t1_strips_leading_dot_slash_and_trailing_punctuation() {
        let got = anchor("./scripts/guard.sh:7,");
        assert_eq!(got, vec![a("scripts/guard.sh", ls(7, 7))]);
    }

    #[test]
    fn t1_extension_without_slash_is_valid() {
        let got = anchor("(uptake_core.py:42)");
        assert_eq!(got, vec![a("uptake_core.py", ls(42, 42))]);
    }

    #[test]
    fn t1_requires_slash_or_source_extension() {
        assert!(anchor("value: 10").is_empty());
        assert!(anchor("foo:10").is_empty());
        assert!(anchor("key: value").is_empty());
    }

    #[test]
    fn t1_ignores_urls() {
        assert!(anchor("https://github.com/org/repo/blob/main/x.py:12").is_empty());
        assert!(anchor("see http://example.com/a/b.md:3 now").is_empty());
    }

    #[test]
    fn t1_ignores_tmp_and_home_paths() {
        assert!(anchor("/tmp/scratch/x.py:5").is_empty());
        assert!(anchor("~/notes/plan.md:9").is_empty());
    }

    #[test]
    fn t1b_bare_dir_file() {
        let got = anchor("the extractor lives in agents/shared/uptake_core.py today");
        assert_eq!(got, vec![a("agents/shared/uptake_core.py", None)]);
        assert_eq!(
            got[0].to_db_string(),
            "ohmyboring:agents/shared/uptake_core.py"
        );
    }

    #[test]
    fn t1b_requires_a_slash() {
        assert!(anchor("see uptake_core.py for details").is_empty());
    }

    #[test]
    fn t1b_requires_a_source_extension() {
        assert!(anchor("config in dir/settings.json is ignored").is_empty());
    }

    #[test]
    fn t1b_strips_leading_dot_slash() {
        let got = anchor("run ./scripts/e2e.sh first");
        assert_eq!(got, vec![a("scripts/e2e.sh", None)]);
    }

    #[test]
    fn t1b_ignores_urls_tmp_and_home() {
        assert!(anchor("https://github.com/org/repo/blob/main/a/b.py").is_empty());
        assert!(anchor("/tmp/scratch/x.py").is_empty());
        assert!(anchor("~/notes/plan.md").is_empty());
    }

    #[test]
    fn dedup_keeps_first_appearance_order() {
        let got = anchor(
            "a/b.rs:10 then agents/shared/uptake_core.py:5 and a/b.rs:10 again, plus c/d.ts",
        );
        assert_eq!(
            got,
            vec![
                a("a/b.rs", ls(10, 10)),
                a("agents/shared/uptake_core.py", ls(5, 5)),
                a("c/d.ts", None),
            ]
        );
    }

    #[test]
    fn same_path_with_distinct_spans_are_distinct_anchors() {
        let got = anchor("a/b.rs:10 vs a/b.rs:20");
        assert_eq!(got, vec![a("a/b.rs", ls(10, 10)), a("a/b.rs", ls(20, 20))]);
    }

    #[test]
    fn caps_at_twenty() {
        let body = (0..30)
            .map(|i| format!("dir/f{i:02}.rs:{i}"))
            .collect::<Vec<_>>()
            .join(" ");
        let got = anchor(&body);
        assert_eq!(got.len(), 20);
        assert_eq!(got[0].path, "dir/f00.rs");
        assert_eq!(got[19].path, "dir/f19.rs");
    }

    #[test]
    fn anchor_for_claim_matches_path_or_stem() {
        let anchors = anchor("drudge/src/store.rs:1212 and agents/shared/uptake_core.py:283");
        let by_stem = anchor_for_claim(&anchors, "store claim rows", "x").unwrap();
        assert_eq!(by_stem.path, "drudge/src/store.rs");
        let by_path =
            anchor_for_claim(&anchors, "subj", "see agents/shared/uptake_core.py").unwrap();
        assert_eq!(by_path.path, "agents/shared/uptake_core.py");
    }

    #[test]
    fn anchor_for_claim_falls_back_to_first() {
        let anchors = anchor("a/b.rs:10 c/d.py:20");
        let got = anchor_for_claim(&anchors, "unrelated subject", "unrelated value").unwrap();
        assert_eq!(got.path, "a/b.rs");
    }

    #[test]
    fn anchor_for_claim_without_anchors_is_none() {
        assert!(anchor_for_claim(&[], "subject", "value").is_none());
        assert!(anchor_for_claim(&anchor("nothing here"), "subject", "value").is_none());
    }

    #[test]
    fn from_db_string_round_trips_every_stored_shape() {
        use super::Anchor;

        for db in [
            "ohmyboring:agents/shared/uptake_core.py",
            "ohmyboring:agents/shared/uptake_core.py:L283",
            "ohmyboring:drudge/src/store.rs:L1212-L1240",
        ] {
            let parsed = Anchor::from_db_string(db).unwrap_or_else(|| panic!("parse {db}"));
            assert_eq!(parsed.to_db_string(), db);
        }
        let abs = Anchor::from_db_string("proj:/var/tmp/repo/src/x.rs:L3-L5")
            .unwrap_or_else(|| panic!("parse absolute path anchor"));
        assert_eq!(abs.project, "proj");
        assert_eq!(abs.path, "/var/tmp/repo/src/x.rs");
        assert_eq!(abs.span, Some(ls(3, 5)));
        assert!(Anchor::from_db_string("no-project-separator").is_none());
        assert!(Anchor::from_db_string("p:path:not-a-span").is_none());
    }
}
