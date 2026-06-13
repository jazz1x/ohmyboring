//! Frontmatter entity — parse raw `.md` into a typed form once at the boundary (parse-don't-validate).
//! If YAML frontmatter (`--- ... ---`) is present, parse it; otherwise infer origin/kind/project from the path.
//! Parse failure goes on the `Result` rail rather than a silent fallback (ROP) — the caller decides the graceful boundary.
use anyhow::Result;
use serde::{Deserialize, Serialize};

/// Structured metadata for an ingested document — the basis (SSOT) for audit · filtering · graph edges.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct FrontMatter {
    pub origin: String, // personal | company
    pub project: String,
    pub date: String,
    pub kind: String, // note | memory | doc  (value produced by enrich; "session" exists only as a reserved word)
    pub source_path: String,
    pub title: Option<String>,
    pub tags: Vec<String>,
}

/// Path tokens that classify a document as origin=company — env `DRUDGE_COMPANY_SUBSTR` (':'-separated).
/// Default empty = no tokens → every document is origin=personal (company concept unused).
/// Downstream, just plugging tokens into `.env` turns on the company layer without code changes (SSOT).
pub fn company_substrs() -> Vec<String> {
    std::env::var("DRUDGE_COMPANY_SUBSTR")
        .unwrap_or_default()
        .split(':')
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

/// True if the path contains any of the configured company tokens. Always false if no tokens are set.
pub fn is_company_path(path: &str) -> bool {
    company_substrs().iter().any(|s| path.contains(s))
}

impl FrontMatter {
    /// Fill empty fields via path heuristics (part of constructing the typed value).
    fn enrich(&mut self, path: &str) {
        if self.source_path.is_empty() {
            self.source_path.push_str(path);
        }
        if self.origin.is_empty() {
            self.origin.push_str(if is_company_path(path) {
                "company"
            } else {
                "personal"
            });
        }
        if self.kind.is_empty() {
            self.kind.push_str(if path.contains("/notes/") {
                "note"
            } else if path.contains("/memory") {
                "memory"
            } else {
                "doc"
            });
        }
        if self.project.is_empty() {
            self.project = derive_project(path);
        }
    }
}

/// The `<proj>` in `…/projects/<proj>/…`, or the parent directory name.
fn derive_project(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    if let Some(i) = parts.iter().position(|&p| p == "projects")
        && let Some(proj) = parts.get(i + 1)
    {
        return (*proj).to_owned();
    }
    // fallback: the file's parent directory
    parts
        .iter()
        .rev()
        .nth(1)
        .map_or_else(|| "unknown".to_owned(), |s| (*s).to_owned())
}

/// raw `.md` → (frontmatter, body). Err if frontmatter YAML parsing fails.
pub fn parse(raw: &str, fallback_path: &str) -> Result<(FrontMatter, String)> {
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(raw); // strip BOM
    let mut front = if let Some(rest) = raw.strip_prefix("---\n") {
        if let Some(end) = rest.find("\n---\n") {
            let yaml = &rest[..end];
            let body = rest[end + 5..].to_owned();
            let front: FrontMatter = serde_yaml::from_str(yaml)?;
            front_enriched(front, fallback_path, &body)
        } else {
            front_enriched(FrontMatter::default(), fallback_path, raw)
        }
    } else {
        front_enriched(FrontMatter::default(), fallback_path, raw)
    };
    let body = std::mem::take(&mut front.1);
    Ok((front.0, body))
}

fn front_enriched(mut fm: FrontMatter, path: &str, body: &str) -> (FrontMatter, String) {
    fm.enrich(path);
    (fm, body.trim_start().to_owned())
}

/// FrontMatter + body → `.md` text (`--- yaml --- body`).
#[allow(dead_code)] // S8: used when frontmatter-izing the distill hook output
pub fn render(front: &FrontMatter, body: &str) -> Result<String> {
    let yaml = serde_yaml::to_string(front)?;
    Ok(format!("---\n{yaml}---\n{body}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{FrontMatter, parse, render};

    #[test]
    fn parse_with_frontmatter() {
        let raw = "---\norigin: company\nproject: demo\ntags:\n  - rust\n  - rop\n---\n본문 시작\n둘째 줄";
        let (fm, body) = parse(raw, "/x/y.md").unwrap();
        assert_eq!(fm.origin, "company");
        assert_eq!(fm.project, "demo");
        assert_eq!(fm.tags, vec!["rust", "rop"]);
        assert_eq!(body, "본문 시작\n둘째 줄");
    }

    #[test]
    fn parse_without_frontmatter_infers_from_path() {
        let (fm, body) = parse(
            "그냥 본문",
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md",
        )
        .unwrap();
        assert_eq!(fm.origin, "personal"); // no company token set → personal
        assert_eq!(fm.kind, "note"); // /notes/ path
        assert_eq!(fm.project, "oh-my-boring"); // projects/<proj>
        assert_eq!(
            fm.source_path,
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md"
        );
        assert_eq!(body, "그냥 본문");
    }

    #[test]
    fn round_trip_render_then_parse() {
        let fm = FrontMatter {
            origin: "personal".to_owned(),
            project: "oh-my-boring".to_owned(),
            kind: "note".to_owned(),
            tags: vec!["a".to_owned(), "b".to_owned()],
            ..Default::default()
        };
        let md = render(&fm, "본문").unwrap();
        let (back, body) = parse(&md, "/p.md").unwrap();
        assert_eq!(back.origin, "personal");
        assert_eq!(back.project, "oh-my-boring");
        assert_eq!(back.tags, vec!["a", "b"]);
        assert_eq!(body, "본문");
    }

    #[test]
    fn malformed_yaml_is_error_not_silent() {
        // ROP: broken frontmatter goes to Err (not a silent fallback)
        let raw = "---\norigin: [unclosed\n---\n본문";
        assert!(parse(raw, "/p.md").is_err());
    }
}
