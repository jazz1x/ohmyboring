use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::config;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct FrontMatter {
    pub origin: String,
    pub project: String,
    pub date: String,
    pub kind: String,
    pub source_path: String,
    pub title: Option<String>,
    pub tags: Vec<String>,
    pub tools: Vec<String>,
    pub concepts: Vec<String>,
    pub claims: Vec<Claim>,
    pub sources: Vec<String>,
    pub omb_session_id: Option<String>,
    pub author: Author,
}

/// Who wrote a note — the same vocabulary as an edge's `judge`, plus `unknown` for every note
/// written before the field existed or by a caller that names nobody.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub enum Author {
    Owner,
    Inferred,
    Agent(String),
    #[default]
    Unknown,
}

impl Author {
    /// The judge a write by this author carries onto its edges; `unknown` names nobody.
    #[must_use]
    pub fn as_judge(&self) -> Option<String> {
        match self {
            Self::Unknown => None,
            known => Some(String::from(known.clone())),
        }
    }
}

impl std::str::FromStr for Author {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        match raw.trim() {
            "owner" => Ok(Self::Owner),
            "inferred" => Ok(Self::Inferred),
            "unknown" => Ok(Self::Unknown),
            other => other
                .strip_prefix("agent:")
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .map(|name| Self::Agent(name.to_owned()))
                .ok_or_else(|| {
                    format!("author must be owner | inferred | unknown | agent:<name>, got {raw:?}")
                }),
        }
    }
}

impl TryFrom<String> for Author {
    type Error = String;

    fn try_from(raw: String) -> Result<Self, Self::Error> {
        raw.parse()
    }
}

impl From<Author> for String {
    fn from(author: Author) -> Self {
        match author {
            Author::Owner => "owner".to_owned(),
            Author::Inferred => "inferred".to_owned(),
            Author::Agent(name) => format!("agent:{name}"),
            Author::Unknown => "unknown".to_owned(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct Claim {
    pub subject: String,
    pub predicate: String,
    pub value: String,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub confidence: String,
}

impl Claim {
    pub fn kind(&self) -> &str {
        let k = self.kind.trim();
        if k.is_empty() {
            return "fact";
        }
        if (k == "next" || k == "blocked") && denies_work(&self.value) {
            return "fact";
        }
        k
    }

    pub fn confidence(&self) -> &str {
        let c = self.confidence.trim();
        if c.is_empty() { "unknown" } else { c }
    }
}

const WORK_DENIALS: &[&str] = &[
    "none",
    "nothing",
    "n/a",
    "na",
    "not applicable",
    "no",
    "-",
    "--",
    "없음",
    "없다",
    "없습니다",
    "없어요",
    "해당 없음",
    "해당없음",
    "남은 작업 없음",
    "남은 작업이 없음",
    "なし",
    "無し",
    "該当なし",
];

fn denies_work(value: &str) -> bool {
    let v = value
        .trim()
        .trim_end_matches(['.', '。'])
        .trim()
        .to_lowercase();
    WORK_DENIALS.contains(&v.as_str())
}

impl FrontMatter {
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

fn derive_project(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    if let Some(i) = parts.iter().position(|&p| p == "projects")
        && let Some(proj) = parts.get(i + 1)
    {
        return (*proj).to_owned();
    }
    String::new()
}

const BOM: char = '\u{feff}';

pub fn parse(
    raw: &str,
    fallback_path: &str,
    cfg: &config::BoringConfig,
) -> Result<(FrontMatter, String)> {
    let raw = raw.strip_prefix(BOM).unwrap_or(raw);
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

#[allow(dead_code)]
pub fn render(front: &FrontMatter, body: &str) -> Result<String> {
    let yaml = serde_yaml::to_string(front)?;
    Ok(format!("---\n{yaml}---\n{body}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{Author, Claim, FrontMatter, WORK_DENIALS, parse, render};
    use crate::config::BoringConfig;

    fn test_cfg() -> BoringConfig {
        BoringConfig::default()
    }

    #[test]
    fn author_is_parsed_once_at_the_note_boundary() {
        let author_of = |line: &str| {
            parse(
                &format!("---\n{line}---\n본문"),
                "/vault/wiki/wiki-0001.md",
                &test_cfg(),
            )
            .map(|(fm, _)| fm.author)
        };
        assert_eq!(
            author_of("title: t\n").unwrap(),
            Author::Unknown,
            "legacy note"
        );
        assert_eq!(author_of("author: owner\n").unwrap(), Author::Owner);
        assert_eq!(author_of("author: inferred\n").unwrap(), Author::Inferred);
        assert_eq!(
            author_of("author: agent:hermes\n").unwrap(),
            Author::Agent("hermes".to_owned())
        );
        for bad in ["author: admin\n", "author: 'agent:'\n", "author: Owner\n"] {
            assert!(author_of(bad).is_err(), "{bad:?} must be refused");
        }
        assert_eq!(
            Author::Agent("hermes".to_owned()).as_judge().as_deref(),
            Some("agent:hermes")
        );
        assert_eq!(Author::Unknown.as_judge(), None);
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
        assert_eq!(fm.origin, "personal");
        assert_eq!(fm.kind, "note");
        assert_eq!(fm.project, "oh-my-boring");
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
    fn absent_confidence_is_unknown_not_certain() {
        let claim = super::Claim {
            subject: "recall_core.py".to_owned(),
            predicate: "threshold-value".to_owned(),
            value: "0.514".to_owned(),
            kind: String::new(),
            confidence: String::new(),
        };
        assert_eq!(claim.confidence(), "unknown");
        assert_ne!(
            claim.confidence(),
            "certain",
            "silence must not become certainty"
        );
        assert_eq!(claim.kind(), "fact");

        let spoken = super::Claim {
            confidence: "  likely  ".to_owned(),
            ..claim
        };
        assert_eq!(
            spoken.confidence(),
            "likely",
            "a stated value is still honoured"
        );
    }

    #[test]
    fn malformed_yaml_is_error_not_silent() {
        let raw = "---\norigin: [unclosed\n---\n본문";
        assert!(parse(raw, "/p.md", &test_cfg()).is_err());
    }

    #[test]
    fn derive_project_reads_a_declaration_and_never_invents_one() {
        assert_eq!(
            super::derive_project("/vault/projects/omb/notes/a.md"),
            "omb"
        );
        assert_eq!(
            super::derive_project("/vault/wiki/daily-brief-2026-09-02.md"),
            ""
        );
        assert_eq!(super::derive_project("/vault/wiki/wiki-0001.md"), "");
        assert_eq!(super::derive_project("a.md"), "");
        assert_eq!(super::derive_project(""), "");

        let raw = "---\norigin: personal\nproject: foodspring-front\n---\n본문";
        let (fm, _) = parse(raw, "/vault/wiki/note.md", &test_cfg()).unwrap();
        assert_eq!(fm.project, "foodspring-front");

        let raw = "---\norigin: personal\n---\n본문";
        let (fm, _) = parse(raw, "/vault/wiki/note.md", &test_cfg()).unwrap();
        assert_eq!(fm.project, "");
    }

    fn claim(kind: &str, value: &str) -> Claim {
        Claim {
            subject: "fds-17084".into(),
            predicate: "next-step".into(),
            value: value.into(),
            kind: kind.into(),
            confidence: "certain".into(),
        }
    }

    #[test]
    fn a_next_step_of_none_is_a_fact_not_a_next_step() {
        for value in [
            "none",
            "None",
            " none. ",
            "없음",
            "남은 작업이 없음",
            "なし",
            "N/A",
        ] {
            assert_eq!(
                claim("next", value).kind(),
                "fact",
                "{value:?} denies that there is a next step"
            );
            assert_eq!(
                claim("blocked", value).kind(),
                "fact",
                "{value:?} denies that there is a blocker"
            );
        }
    }

    #[test]
    fn a_next_step_that_names_a_state_stays_a_next_step() {
        for value in [
            "pending",
            "required",
            "필요",
            "미정",
            "unavailable",
            "docker rerun",
            "none of the retries landed",
            "확인 결과 해당 없음으로 판명되어 재작업 필요",
        ] {
            assert_eq!(
                claim("next", value).kind(),
                "next",
                "{value:?} names a state, not an absence"
            );
        }
    }

    #[test]
    fn the_downgrade_only_touches_the_work_kinds() {
        assert_eq!(claim("decision", "none").kind(), "decision");
        assert_eq!(claim("risk", "없음").kind(), "risk");
        assert_eq!(
            claim("", "none").kind(),
            "fact",
            "absent kind still defaults"
        );
    }

    #[test]
    fn test_work_denials_match_the_migration() {
        let store = include_str!("store.rs");
        let start = store
            .find("CREATE TEMP TABLE denied_now")
            .expect("the relabel migration is gone -- was it dropped, or renamed?");
        let end = store[start..]
            .find("]);")
            .map(|i| start + i)
            .expect("migration array is unterminated");
        let sql = &store[start..end];
        for word in WORK_DENIALS {
            assert!(
                sql.contains(&format!("'{word}'")),
                "{word:?} is in WORK_DENIALS but not in the migration, so notes written from \
                 now on are downgraded while the rows already stored are not"
            );
        }
    }

    #[test]
    fn a_brief_note_with_its_sources_block_still_parses() {
        let raw = "---\ntitle: \"Daily Brief — 2026-09-03\"\norigin: personal\ndate: 2026-09-03\nkind: note\ntags: [daily-brief]\nbrief_sources:\n  - /vault/wiki/wiki-1.md\n---\n\n## proj\n- Next: x\n";
        let (fm, body) = parse(raw, "/vault/wiki/daily-brief-2026-09-03.md", &test_cfg()).unwrap();
        assert_eq!(fm.kind, "note");
        assert_eq!(fm.project, "", "a brief note declares no project");
        assert!(
            fm.sources.is_empty(),
            "the governed sources axis stays untouched"
        );
        assert!(
            body.trim_start().starts_with("## proj"),
            "body was {body:?}"
        );
    }

    #[test]
    fn a_work_denial_survives_any_run_of_trailing_stops() {
        for value in ["none", "none.", "none...", "없음.", "없음。。", "없음 ."] {
            let denial = Claim {
                subject: "s".to_owned(),
                predicate: "next".to_owned(),
                value: value.to_owned(),
                kind: "next".to_owned(),
                confidence: String::new(),
            };
            assert_eq!(
                denial.kind(),
                "fact",
                "{value:?} denies work, so it is not a next step"
            );
        }
    }
}
