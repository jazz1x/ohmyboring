//! BoringConfig — personal memory policy SSOT (`boring.json`).
//!
//! Design:
//! - `.env` keeps secrets/runtime switches; `boring.json` keeps policy/metadata.
//! - Forward-compatible: newer `schema_version` loads with a warning, unknown fields are ignored.
//! - File absence is not an error — defaults are returned (env fallback is added in a later layer).
use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

const CURRENT_SCHEMA_VERSION: u32 = 1;
const KNOWN_TOP_LEVEL: &[&str] = &[
    "schema_version",
    "note_lang",
    "repos",
    "agents",
    "embed_model",
    "embed_dim",
];
/// Default embedder (bge-m3 = 1024-dim) — the kernel's sole model dependency.
const DEFAULT_EMBED_MODEL: &str = "bge-m3";
const DEFAULT_EMBED_DIM: u32 = 1024;

/// Personal memory policy configuration.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct BoringConfig {
    pub schema_version: u32,
    pub note_lang: NoteLang,
    pub repos: Vec<RepoRule>,
    pub agents: Vec<AgentSource>,
    /// Embedding model — the only model drudge itself calls (kernel A). `embed_dim` MUST match its
    /// output dimension (guarded at every upsert). Changing the dim needs a `make reset` (existing
    /// vectors are a different shape). base_url/api_key stay in env (.env = runtime/secret SSOT).
    pub embed_model: String,
    pub embed_dim: u32,
}

impl Default for BoringConfig {
    fn default() -> Self {
        Self {
            schema_version: CURRENT_SCHEMA_VERSION,
            note_lang: NoteLang::default(),
            repos: Vec::new(),
            agents: Vec::new(),
            embed_model: DEFAULT_EMBED_MODEL.to_owned(),
            embed_dim: DEFAULT_EMBED_DIM,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum NoteLang {
    #[default]
    Auto,
    Ko,
    En,
}

impl NoteLang {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Ko => "ko",
            Self::En => "en",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct RepoRule {
    /// Substring matched against cwd or git remote URL (case-insensitive).
    #[serde(rename = "match")]
    pub matcher: String,
    pub origin: Origin,
    #[serde(default)]
    pub name: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Origin {
    #[default]
    Personal,
    Company,
    Mirror,
    Community,
}

impl Origin {
    /// Render back to the lowercase string the DB / frontmatter expect (mirrors `NoteLang::as_str`).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Personal => "personal",
            Self::Company => "company",
            Self::Mirror => "mirror",
            Self::Community => "community",
        }
    }
}

/// The single SSOT `str -> Origin` boundary parse (parse-don't-validate). Reused by `remember` +
/// `classify_repo` so a typo'd origin is rejected once, not silently coerced to personal anywhere.
/// `config::Origin` is the SSOT — `vault.rs` has a duplicate enum; the parse lives only here.
impl std::str::FromStr for Origin {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim() {
            "personal" => Ok(Self::Personal),
            "company" => Ok(Self::Company),
            "mirror" => Ok(Self::Mirror),
            "community" => Ok(Self::Community),
            other => Err(format!("invalid origin: {other}")),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentSource {
    pub id: String,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default)]
    pub format: String,
    #[serde(default)]
    pub paths: Vec<String>,
}

impl Default for AgentSource {
    fn default() -> Self {
        Self {
            id: String::new(),
            enabled: true,
            format: String::new(),
            paths: Vec::new(),
        }
    }
}

const fn default_true() -> bool {
    true
}

impl BoringConfig {
    /// Load config from `path`, or discover it via `BORING_CONFIG` / `OMB_HOME` / cwd.
    /// Missing file falls back to legacy env vars (with deprecation warnings).
    pub fn load(path: Option<&Path>) -> Result<Self> {
        let path = match path {
            Some(p) => p.to_path_buf(),
            None => match discover_path() {
                Some(p) => p,
                None => return Ok(Self::from_env()),
            },
        };

        if !path.exists() {
            return Ok(Self::from_env());
        }

        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("read boring.json: {}", path.display()))?;
        Self::from_str(&raw)
    }

    /// Build config from the current process env vars. Emits deprecation warnings to stderr.
    pub fn from_env() -> Self {
        Self::from_env_map(&std::env::vars().collect())
    }

    /// Build config from an explicit map (testable). Emits deprecation warnings to stderr.
    pub fn from_env_map(vars: &std::collections::HashMap<String, String>) -> Self {
        let mut cfg = Self::default();

        if let Some(lang) = vars.get("DRUDGE_NOTE_LANG") {
            eprintln!("[config] deprecated: DRUDGE_NOTE_LANG is set; move it to boring.json");
            cfg.note_lang = match lang.to_lowercase().as_str() {
                "ko" => NoteLang::Ko,
                "en" => NoteLang::En,
                _ => NoteLang::Auto,
            };
        }

        if let Some(model) = vars.get("DRUDGE_EMBED_MODEL").filter(|s| !s.is_empty()) {
            eprintln!(
                "[config] deprecated: DRUDGE_EMBED_MODEL is set; move it to boring.json embed_model"
            );
            cfg.embed_model.clone_from(model);
        }

        let mut company_tokens: Vec<String> = Vec::new();
        if let Some(s) = vars.get("DRUDGE_COMPANY_SUBSTR") {
            eprintln!("[config] deprecated: DRUDGE_COMPANY_SUBSTR is set; move it to boring.json");
            company_tokens.extend(split_tokens(s));
        }
        if let Some(s) = vars.get("DISTILL_COMPANY_CWD") {
            eprintln!("[config] deprecated: DISTILL_COMPANY_CWD is set; move it to boring.json");
            company_tokens.extend(split_tokens(s));
        }
        for tok in company_tokens {
            cfg.repos.push(RepoRule {
                matcher: tok.clone(),
                origin: Origin::Company,
                name: String::new(),
            });
        }

        if let Some(s) = vars.get("DRUDGE_SOURCE_DIRS") {
            eprintln!("[config] deprecated: DRUDGE_SOURCE_DIRS is set; move it to boring.json");
            let paths = split_tokens(s);
            if !paths.is_empty() {
                cfg.agents.push(AgentSource {
                    id: "legacy-source-dirs".to_owned(),
                    enabled: true,
                    format: "claude-json".to_owned(),
                    paths,
                });
            }
        }

        cfg
    }

    /// Parse from JSON string. Checks schema_version and warns on unknown top-level fields.
    pub fn from_str(raw: &str) -> Result<Self> {
        let value: serde_json::Value =
            serde_json::from_str(raw).context("parse boring.json as JSON")?;

        let version = value
            .get("schema_version")
            .and_then(serde_json::Value::as_u64)
            .map(|v| u32::try_from(v).unwrap_or(0))
            .context("boring.json must have schema_version")?;

        if version > CURRENT_SCHEMA_VERSION {
            eprintln!(
                "[config] warning: boring.json schema_version {version} is newer than supported {CURRENT_SCHEMA_VERSION}; unknown fields will be ignored"
            );
        } else if version < CURRENT_SCHEMA_VERSION {
            eprintln!(
                "[config] warning: boring.json schema_version {version} is older than {CURRENT_SCHEMA_VERSION}"
            );
        }

        let known: HashSet<&str> = KNOWN_TOP_LEVEL.iter().copied().collect();
        if let Some(obj) = value.as_object() {
            for key in obj.keys() {
                let k = key.as_str();
                // $schema and other JSON-metadata keys starting with '$' are ignored silently.
                if k.starts_with('$') {
                    continue;
                }
                if !known.contains(k) {
                    eprintln!("[config] warning: unknown top-level field '{key}' in boring.json");
                }
            }
        }

        let config: BoringConfig =
            serde_json::from_value(value).context("deserialize boring.json")?;
        Ok(config)
    }

    /// Expand enabled agent paths and apply `~` → `$HOME` expansion.
    /// Graceful default: if no enabled agent contributes a path, fall back to `~/.claude/projects`
    /// rather than scanning nothing silently (a config with empty `agents` must not zero out ingest).
    pub fn source_dirs(&self) -> Vec<String> {
        let home = std::env::var("HOME").unwrap_or_default();
        let dirs: Vec<String> = self
            .agents
            .iter()
            .filter(|a| a.enabled)
            .flat_map(|a| &a.paths)
            .map(|p| expand_tilde(p, &home))
            .collect();
        if dirs.is_empty() {
            return vec![expand_tilde("~/.claude/projects", &home)];
        }
        dirs
    }

    /// Classify a session/working dir into (origin, optional repo name).
    pub fn classify(&self, cwd: &str, remote_url: Option<&str>) -> (Origin, Option<String>) {
        let haystack = match remote_url {
            Some(url) => format!("{cwd}\n{url}"),
            None => cwd.to_owned(),
        };
        let lowered = haystack.to_lowercase();

        for rule in &self.repos {
            if lowered.contains(&rule.matcher.to_lowercase()) {
                let name = if rule.name.is_empty() {
                    derive_name_from_match(&rule.matcher, cwd, remote_url)
                } else {
                    Some(rule.name.clone())
                };
                return (rule.origin, name);
            }
        }
        (Origin::Personal, None)
    }
}

fn split_tokens(s: &str) -> Vec<String> {
    s.split(':')
        .filter(|t| !t.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Agent-control write path: upsert a repo classification rule into a specific `boring.json`
/// path (matched by the `match` field). Takes effect on the next config load (sync/restart).
/// Loud (`Err`) on missing file / parse failure / write failure — never a silent swallow (ROP).
/// Edits via `serde_json::Value` so unknown fields (agents, schema, future keys) are preserved verbatim.
pub fn upsert_repo_rule_at(
    match_: &str,
    origin: &str,
    name: Option<&str>,
    path: &Path,
) -> Result<PathBuf> {
    let txt = std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    let mut v: serde_json::Value =
        serde_json::from_str(&txt).with_context(|| format!("parse {}", path.display()))?;
    let repos = v
        .get_mut("repos")
        .and_then(serde_json::Value::as_array_mut)
        .context("boring.json: repos[] is missing or not an array")?;
    let mut rule = serde_json::json!({ "match": match_, "origin": origin });
    if let Some(n) = name {
        rule["name"] = serde_json::Value::String(n.to_owned());
    }
    match repos
        .iter_mut()
        .find(|r| r.get("match").and_then(serde_json::Value::as_str) == Some(match_))
    {
        Some(existing) => *existing = rule,
        None => repos.push(rule),
    }
    let out = format!(
        "{}\n",
        serde_json::to_string_pretty(&v).context("serialize boring.json")?
    );
    std::fs::write(path, out).with_context(|| format!("write {}", path.display()))?;
    Ok(path.to_path_buf())
}

/// Discover the path to `boring.json` from the canonical env overrides.
pub fn discover_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("BORING_CONFIG") {
        return Some(PathBuf::from(p));
    }
    if let Ok(home) = std::env::var("OMB_HOME") {
        let p = PathBuf::from(home).join("boring.json");
        if p.exists() {
            return Some(p);
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        let p = cwd.join("boring.json");
        if p.exists() {
            return Some(p);
        }
    }
    None
}

fn expand_tilde(path: &str, home: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        format!("{home}/{rest}")
    } else if path == "~" {
        home.to_owned()
    } else {
        path.to_owned()
    }
}

fn derive_name_from_match(matcher: &str, cwd: &str, remote_url: Option<&str>) -> Option<String> {
    // If matcher looks like org/name, use the last two segments.
    if matcher.contains('/') {
        let parts: Vec<&str> = matcher.split('/').filter(|s| !s.is_empty()).collect();
        return parts.last().map(|s| (*s).to_owned());
    }
    // Otherwise try git remote slug, then cwd folder name.
    if let Some(url) = remote_url {
        let slug = url
            .trim_end_matches(".git")
            .split('/')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();
        if slug.len() >= 2 {
            return Some(format!("{}/{}", slug[slug.len() - 2], slug[slug.len() - 1]));
        }
    }
    cwd.split('/').rfind(|s| !s.is_empty()).map(str::to_owned)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::{AgentSource, BoringConfig, NoteLang, Origin, RepoRule};

    #[test]
    fn origin_parse_roundtrips_and_rejects_unknown() {
        use std::str::FromStr;
        // every valid variant parses and renders back to its lowercase string (the SSOT round-trip
        // that remember + classify_repo share).
        for s in ["personal", "company", "mirror", "community"] {
            assert_eq!(Origin::from_str(s).unwrap().as_str(), s);
        }
        // whitespace is trimmed at the boundary.
        assert_eq!(Origin::from_str("  company  ").unwrap(), Origin::Company);
        // present-but-invalid is REJECTED (parse-don't-validate) — never silently coerced to personal.
        assert!(Origin::from_str("evil").is_err());
        assert!(Origin::from_str("").is_err());
    }

    #[test]
    fn defaults_when_file_missing() {
        let cfg = BoringConfig::load(Some(std::path::Path::new("/nonexistent/boring.json")))
            .expect("missing file should return defaults");
        assert_eq!(cfg.schema_version, 1);
        assert_eq!(cfg.note_lang, NoteLang::Auto);
        assert!(cfg.repos.is_empty());
        assert!(cfg.agents.is_empty());
    }

    #[test]
    fn embed_defaults_and_override() {
        // missing → bge-m3 / 1024 (kernel default)
        let def = BoringConfig::from_str(r#"{"schema_version": 1}"#).unwrap();
        assert_eq!(def.embed_model, "bge-m3");
        assert_eq!(def.embed_dim, 1024);
        // explicit override
        let cfg = BoringConfig::from_str(
            r#"{"schema_version": 1, "embed_model": "nomic-embed-text", "embed_dim": 768}"#,
        )
        .unwrap();
        assert_eq!(cfg.embed_model, "nomic-embed-text");
        assert_eq!(cfg.embed_dim, 768);
    }

    #[test]
    fn parse_full_config() {
        let raw = r#"{
            "schema_version": 1,
            "note_lang": "ko",
            "repos": [
                {"match": "acme", "origin": "company", "name": "acme"}
            ],
            "agents": [
                {"id": "claude-code", "enabled": true, "format": "claude-json", "paths": ["~/.claude/projects"]}
            ]
        }"#;
        let cfg = BoringConfig::from_str(raw).unwrap();
        assert_eq!(cfg.note_lang, NoteLang::Ko);
        assert_eq!(cfg.repos.len(), 1);
        assert_eq!(cfg.repos[0].matcher, "acme");
        assert_eq!(cfg.repos[0].origin, Origin::Company);
        assert_eq!(cfg.repos[0].name, "acme");
        assert_eq!(cfg.agents.len(), 1);
        assert!(cfg.agents[0].enabled);
    }

    #[test]
    fn schema_version_warning_does_not_fail() {
        let raw = r#"{"schema_version": 99, "note_lang": "en"}"#;
        let cfg = BoringConfig::from_str(raw).unwrap();
        assert_eq!(cfg.schema_version, 99);
        assert_eq!(cfg.note_lang, NoteLang::En);
    }

    #[test]
    fn unknown_top_level_field_warns_but_parses() {
        let raw = r#"{"schema_version": 1, "future_field": true}"#;
        let cfg = BoringConfig::from_str(raw).unwrap();
        assert_eq!(cfg.schema_version, 1);
    }

    #[test]
    fn schema_metadata_key_is_ignored() {
        let raw = r#"{"$schema": "./boring.schema.json", "schema_version": 1}"#;
        let cfg = BoringConfig::from_str(raw).unwrap();
        assert_eq!(cfg.schema_version, 1);
    }

    #[test]
    fn source_dirs_expand_tilde_and_skip_disabled() {
        let cfg = BoringConfig {
            agents: vec![
                AgentSource {
                    id: "a".into(),
                    enabled: true,
                    format: "claude-json".into(),
                    paths: vec!["~/.claude/projects".into()],
                },
                AgentSource {
                    id: "b".into(),
                    enabled: false,
                    format: "claude-json".into(),
                    paths: vec!["~/other".into()],
                },
            ],
            ..Default::default()
        };
        let home = std::env::var("HOME").unwrap_or_default();
        let dirs = cfg.source_dirs();
        assert_eq!(dirs.len(), 1);
        assert!(dirs[0].starts_with(&home));
        assert!(dirs[0].ends_with(".claude/projects"));
    }

    #[test]
    fn classify_first_match_wins() {
        let cfg = BoringConfig {
            repos: vec![
                RepoRule {
                    matcher: "acme".into(),
                    origin: Origin::Company,
                    name: "acme".into(),
                },
                RepoRule {
                    matcher: "oh-my-boring".into(),
                    origin: Origin::Personal,
                    name: "oh-my-boring".into(),
                },
            ],
            ..Default::default()
        };
        let (origin, name) = cfg.classify("/Users/x/acme/oh-my-boring", None);
        assert_eq!(origin, Origin::Company);
        assert_eq!(name, Some("acme".to_owned()));
    }

    #[test]
    fn default_origin_is_personal() {
        let cfg = BoringConfig::default();
        let (origin, name) = cfg.classify("/Users/x/something", None);
        assert_eq!(origin, Origin::Personal);
        assert_eq!(name, None);
    }

    #[test]
    fn env_fallback_builds_config() {
        let mut vars = std::collections::HashMap::new();
        vars.insert("DRUDGE_NOTE_LANG".to_owned(), "en".to_owned());
        vars.insert("DRUDGE_COMPANY_SUBSTR".to_owned(), "acme:bigco".to_owned());
        vars.insert("DRUDGE_SOURCE_DIRS".to_owned(), "/x:/y".to_owned());
        let cfg = BoringConfig::from_env_map(&vars);
        assert_eq!(cfg.note_lang, NoteLang::En);
        assert_eq!(cfg.repos.len(), 2);
        assert!(cfg.repos.iter().all(|r| r.origin == Origin::Company));
        assert_eq!(cfg.agents.len(), 1);
        assert_eq!(cfg.agents[0].paths, vec!["/x", "/y"]);
    }
}
