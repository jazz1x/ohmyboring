//! Frontmatter entity — parse raw `.md` into a typed form once at the boundary (parse-don't-validate).
//!
//! Cross-reference: ENFORCEMENT.md §A (PDV/boundary) · PHILOSOPHY.md Layer 1.
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
    /// Source artifacts this distilled note is grounded in. Wiki lint requires these to point at
    /// vault-local evidence paths such as `raw/...`, not transient host paths.
    pub sources: Vec<String>,
    /// Ephemeral ingestion queue marker. Not part of the semantic graph; carried only so the
    /// hermes/cron worker can confirm that a specific session was remembered. May be absent.
    pub omb_session_id: Option<String>,
}

/// One temporal fact — `(subject, predicate, value)` plus `kind` and `confidence`.
/// A new value supersedes the old (see `store::upsert_claim`).
/// Agent-provided in note frontmatter; drudge embeds the value (bge-m3) and stores it. No LLM extraction in the kernel.
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
    /// Normalized kind, or `"fact"` when absent/unknown.
    ///
    /// A `next` or `blocked` claim whose value is a denial -- `none`, `없음`, `なし` -- is
    /// downgraded to `fact`, because it is one: the note is reporting that there is no next
    /// step, and that report is true. Read as `next` it becomes its own opposite, and the
    /// stalled register spent months telling the owner every morning to go finish
    /// `fds-17084 next-step: none`. 75 of 645 current next/blocked claims said this
    /// (measured 2026-09-09); `k6/k7`, `pr-232` and `omcr-completed-form` were among the
    /// twelve slots the register actually renders.
    ///
    /// Downgrading rather than dropping, because "nothing is left here" is worth keeping and
    /// worth being asked about -- it just is not work. The vocabulary is deliberately literal
    /// and whole-value only: `pending`, `unrelated` and `미정` stay `next`, since those name a
    /// state the work is in rather than its absence.
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

    /// Normalized confidence, or `"unknown"` when the note did not say.
    ///
    /// It used to default to `"certain"`, which promoted silence to the strongest claim the
    /// vocabulary has: 24 vault claims carry `confidence: ''` — benchmark numbers the distiller
    /// declined to vouch for — and they were being stored, ranked and rendered as certain.
    /// `kind` keeps its `"fact"` default because a claim genuinely is a fact unless it says
    /// otherwise; confidence has no such neutral reading. Not saying is not the same as being sure.
    pub fn confidence(&self) -> &str {
        let c = self.confidence.trim();
        if c.is_empty() { "unknown" } else { c }
    }
}

/// Whole-value denials of work, in the three languages the corpus is written in.
///
/// Whole-value only, and lowercased after trimming a single trailing `.`: a value that merely
/// *contains* one of these is a real next step ("none of the retries landed"), and only a value
/// that *is* one is the note saying there is nothing to do.
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
    let v = value.trim().trim_end_matches(['.', '。']).trim().to_lowercase();
    WORK_DENIALS.contains(&v.as_str())
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

/// The `<proj>` in `…/projects/<proj>/…`, and nothing else.
///
/// This used to fall back to the file's parent directory, which reads a project name out of a
/// path that never had one. Every note under `vault/wiki/` came out belonging to a project called
/// `wiki` -- 60 documents, including the briefing's own daily notes, so the briefing was filing
/// its output under a project, that project was appearing in the corpus's project list, and the
/// list is what the next briefing checks its headings against. The distiller already decides this
/// (`distill_core.repo_slug`, which returns empty rather than inventing a folder name); a second
/// writer with a different rule is how one axis comes to mean two things.
///
/// No project is a real answer for a note that belongs to none.
fn derive_project(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    if let Some(i) = parts.iter().position(|&p| p == "projects")
        && let Some(proj) = parts.get(i + 1)
    {
        return (*proj).to_owned();
    }
    String::new()
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
    use super::{Claim, FrontMatter, WORK_DENIALS, parse, render};
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

    /// A claim the distiller declined to vouch for must not be stored as the strongest thing the
    /// vocabulary can say. 24 vault claims carry `confidence: ''` and were reading as `certain`.
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
        // `kind` keeps its default on purpose: a claim is a fact unless it says otherwise.
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
        // ROP: broken frontmatter goes to Err (not a silent fallback)
        let raw = "---\norigin: [unclosed\n---\n본문";
        assert!(parse(raw, "/p.md", &test_cfg()).is_err());
    }

    /// A path is not a project. The `projects/<name>/` convention is a real declaration and
    /// survives; the parent directory is not one, and reading it as a project name is what put a
    /// project called `wiki` in the corpus for every note under `vault/wiki/` -- the briefing's
    /// own output included, which then fed the list the next briefing validates against.
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

        // A declared project still wins over any derivation.
        let raw = "---\norigin: personal\nproject: foodspring-front\n---\n본문";
        let (fm, _) = parse(raw, "/vault/wiki/note.md", &test_cfg()).unwrap();
        assert_eq!(fm.project, "foodspring-front");

        // …and a note that declares none keeps none, rather than borrowing its folder's name.
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

    /// The bug this fixes: the stalled register renders twelve slots, and claims that denied
    /// their own existence were holding several of them every morning.
    #[test]
    fn a_next_step_of_none_is_a_fact_not_a_next_step() {
        for value in ["none", "None", " none. ", "없음", "남은 작업이 없음", "なし", "N/A"] {
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

    /// Naming the state the work is in is not the same as saying there is no work. These were
    /// live values in the corpus when the gate went in; downgrading them would lose real items.
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

    /// A `fact` or `decision` is never reinterpreted -- the downgrade only ever removes a claim
    /// from the work registers, and must not reach into the rest of the vocabulary.
    #[test]
    fn the_downgrade_only_touches_the_work_kinds() {
        assert_eq!(claim("decision", "none").kind(), "decision");
        assert_eq!(claim("risk", "없음").kind(), "risk");
        assert_eq!(claim("", "none").kind(), "fact", "absent kind still defaults");
    }

    /// SQL cannot call into Rust, so `store::open`'s one-time relabel repeats this vocabulary
    /// by hand. If someone adds a word here and not there, new notes are downgraded while the
    /// rows already in the database keep haunting the stalled register -- the exact
    /// one-value-in-two-places defect this repo keeps paying for. Fail loudly instead.
    #[test]
    fn test_work_denials_match_the_migration() {
        let store = include_str!("store.rs");
        let start = store
            .find("CREATE TEMP TABLE denied_claim")
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

    /// The scheduler writes a `brief_sources:` list into the brief note so `/brief` can serve it
    /// back without regenerating. It is deliberately not the governed `sources:` axis, and a
    /// parser that rejected the unknown key would stop the day's own briefing from being ingested
    /// -- silently, since sync is resilient and counts the failure rather than aborting.
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
}
