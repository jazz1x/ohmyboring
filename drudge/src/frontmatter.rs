//! Frontmatter entity — parse raw `.md` into a typed form once at the boundary (parse-don't-validate).
//! If YAML frontmatter (`--- ... ---`) is present, parse it; otherwise infer origin/kind/project from the path.
//! Parse failure goes on the `Result` rail rather than a silent fallback (ROP) — the caller decides the graceful boundary.
use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::config;

/// Structured metadata for an ingested document — the basis (SSOT) for audit · filtering · graph edges.
///
/// Honest disclosure: `origin`/`kind` are `String`, not enums — unlike `vault::{Origin,Kind}`,
/// which ARE enums. This is deliberate, not an oversight. These are ingest *boundary* fields parsed
/// from arbitrary markdown (Claude Code transcripts, freeform notes); their only consumers are
/// audit tally (distribution counts) and a Postgres `text` column bind — nothing re-derives domain
/// meaning from them, so there is no parse-don't-validate smell to close. vault's enums cover a
/// different, curated value set (note/memory/session/decision) where exhaustive matching matters.
/// Forcing an enum here would mean code changes for any new ingest kind and a second near-duplicate
/// enum — escalation the rule-of-three doesn't justify (§C "simplest thing that works").
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
    // Agent-curated semantic ontology (kernel A): the deterministic source of the graph.
    // The agent (reasoner) extracts these; drudge (kernel) only stores/links them — no LLM extraction.
    // Absent in legacy/source-walk markdown → default empty (serde default), so those docs simply have no semantic graph.
    pub tools: Vec<String>,
    pub concepts: Vec<String>,
    pub claims: Vec<Claim>,
    /// Ephemeral ingestion queue marker. Not part of the semantic graph; carried only so the
    /// hermes/cron worker can confirm that a specific session was remembered. May be absent.
    pub omb_session_id: Option<String>,
}

/// One temporal fact — `(subject, predicate, value)`. A new value supersedes the old (see `store::upsert_claim`).
/// Agent-provided in note frontmatter; drudge embeds the value (bge-m3) and stores it. No LLM extraction in the kernel.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct Claim {
    pub subject: String,
    pub predicate: String,
    pub value: String,
}

impl FrontMatter {
    /// Fill empty fields via path heuristics (part of constructing the typed value).
    fn enrich(&mut self, path: &str, cfg: &config::BoringConfig) {
        if self.source_path.is_empty() {
            self.source_path.push_str(path);
        }
        if self.origin.is_empty() {
            let (origin, _rule) = cfg.classify(path, None);
            self.origin.push_str(match origin {
                config::Origin::Personal => "personal",
                config::Origin::Company => "company",
                config::Origin::Mirror => "mirror",
                config::Origin::Community => "community",
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
pub fn parse(
    raw: &str,
    fallback_path: &str,
    cfg: &config::BoringConfig,
) -> Result<(FrontMatter, String)> {
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(raw); // strip BOM
    let mut front = if let Some(rest) = raw.strip_prefix("---\n") {
        if let Some(end) = rest.find("\n---\n") {
            let yaml = &rest[..end];
            let body = rest[end + 5..].to_owned();
            let front: FrontMatter = serde_yaml::from_str(yaml)?;
            front_enriched(front, fallback_path, &body, cfg)
        } else {
            front_enriched(FrontMatter::default(), fallback_path, raw, cfg)
        }
    } else {
        front_enriched(FrontMatter::default(), fallback_path, raw, cfg)
    };
    let body = std::mem::take(&mut front.1);
    Ok((front.0, body))
}

fn front_enriched(
    mut fm: FrontMatter,
    path: &str,
    body: &str,
    cfg: &config::BoringConfig,
) -> (FrontMatter, String) {
    fm.enrich(path, cfg);
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
    use crate::config::BoringConfig;

    fn test_cfg() -> BoringConfig {
        BoringConfig::default()
    }

    #[test]
    fn parse_with_frontmatter() {
        let raw = "---\norigin: company\nproject: demo\ntags:\n  - rust\n  - rop\n---\n본문 시작\n둘째 줄";
        let (fm, body) = parse(raw, "/x/y.md", &test_cfg()).unwrap();
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
            &test_cfg(),
        )
        .unwrap();
        assert_eq!(fm.origin, "personal"); // no company rule → personal
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
        let (back, body) = parse(&md, "/p.md", &test_cfg()).unwrap();
        assert_eq!(back.origin, "personal");
        assert_eq!(back.project, "oh-my-boring");
        assert_eq!(back.tags, vec!["a", "b"]);
        assert_eq!(body, "본문");
    }

    #[test]
    fn malformed_yaml_is_error_not_silent() {
        // ROP: broken frontmatter goes to Err (not a silent fallback)
        let raw = "---\norigin: [unclosed\n---\n본문";
        assert!(parse(raw, "/p.md", &test_cfg()).is_err());
    }
}
