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

// v2: added the `llm` block (provider/base_url/model/api_key_env/bootstrap + optional embed model).
// v1 configs still load (top-level embed_model/embed_dim + .env DRUDGE_LLM_* honored as fallback).
const CURRENT_SCHEMA_VERSION: u32 = 2;
const KNOWN_TOP_LEVEL: &[&str] = &[
    "schema_version",
    "note_lang",
    "repos",
    "agents",
    "embed_model",
    "embed_dim",
    "allow_company_origin",
    "llm",
];
/// Default embedder (bge-m3 = 1024-dim) — the kernel's sole model dependency.
const DEFAULT_EMBED_MODEL: &str = "bge-m3";
const DEFAULT_EMBED_DIM: u32 = 1024;
/// Default OpenAI-compatible endpoint = host Ollama `/v1` as seen from inside the container
/// (`host.docker.internal` resolves to the host). boring.json (bind-mounted) is the SSOT; compose no
/// longer injects a base_url default that would shadow it. Overridable at runtime via env (see
/// `Llm::from_config`). NOTE: running the `drudge` binary directly on the host (e.g. `selftest`) needs
/// `BORING_LLM_BASE_URL=http://localhost:11434/v1`, since `host.docker.internal` resolves only in-container.
const DEFAULT_LLM_BASE_URL: &str = "http://host.docker.internal:11434/v1";
/// Default synthesis (chat) model — used only by the `ask`/`brief` generation path.
const DEFAULT_CHAT_MODEL: &str = "gemma4:12b";
/// Default env var name holding the LLM API key (providers that need auth — OpenAI etc.).
/// Named (not the key itself) so the secret never lands in boring.json.
const DEFAULT_API_KEY_ENV: &str = "BORING_LLM_API_KEY";

/// Read env `canonical`, falling back to a deprecated alias (warns once on the alias). Empty = unset,
/// so an `BORING_X=` placeholder doesn't mask the alias/default. SSOT for the BORING_* (canonical) /
/// DRUDGE_* (deprecated) env-prefix migration — shared by config/llm/main/serve.
#[must_use]
pub fn env_alias(canonical: &str, deprecated: &str) -> Option<String> {
    if let Ok(v) = std::env::var(canonical)
        && !v.is_empty()
    {
        return Some(v);
    }
    match std::env::var(deprecated) {
        Ok(v) if !v.is_empty() => {
            eprintln!("[config] deprecated: {deprecated} is set; rename it to {canonical}");
            Some(v)
        }
        _ => None,
    }
}

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
    /// When true, `origin: company` notes are an accepted part of the corpus (session experience from
    /// company work) rather than contamination — the audit `clean` flag stops penalizing them. Default
    /// false keeps the strict boundary (company notes flagged for review). See `allow_company_origin`
    /// in boring.json. Note: this governs *session experience*, not company KB originals (still off-policy).
    pub allow_company_origin: bool,
    /// LLM connection + bootstrap policy (v2). The OpenAI-compatible engine is backend-agnostic; this
    /// block tells the *bootstrap scripts* which provider to prepare (Ollama pull vs LM Studio health
    /// vs nothing) and supplies declarative connection defaults. Runtime env still overrides (see
    /// `Llm::from_config`). embed_model/embed_dim here are authoritative when set (v2 SSOT); they are
    /// resolved into the top-level fields at parse time so the rest of the kernel reads one place.
    pub llm: LlmConfig,
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
            allow_company_origin: false,
            llm: LlmConfig::default(),
        }
    }
}

/// OpenAI-compatible LLM provider. The engine talks `/v1` to all of them identically; the value only
/// steers the host-side bootstrap (model pull / daemon ensure / health probe shape).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Provider {
    /// Local Ollama — bootstrap ensures `ollama serve` and `ollama pull`s the models (`/api/tags`).
    #[default]
    Ollama,
    /// LM Studio — OpenAI-compatible `/v1`; models are loaded via its UI/`lms` CLI (no pull), health = `/v1/models`.
    Lmstudio,
    /// Any other OpenAI-compatible server (vLLM, llama.cpp, remote OpenAI) — health = `/v1/models`, no pull.
    OpenaiCompatible,
}

impl Provider {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ollama => "ollama",
            Self::Lmstudio => "lmstudio",
            Self::OpenaiCompatible => "openai-compatible",
        }
    }
}

/// Whether the bootstrap scripts may start a daemon / pull models (`auto`) or must leave the server to
/// the user (`manual` — only health-checks, never `ollama serve`/`pull`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Bootstrap {
    #[default]
    Auto,
    Manual,
}

/// LLM connection + bootstrap config (the `llm` block of boring.json).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct LlmConfig {
    pub provider: Provider,
    /// OpenAI-compatible base URL (e.g. `http://localhost:11434/v1`, LM Studio `http://localhost:1234/v1`).
    pub base_url: String,
    /// Chat/synthesis model (used only by the `ask`/`brief` generation path).
    pub model: String,
    /// Embedding model — authoritative when set (resolves into the top-level `embed_model`). None = use
    /// the (legacy) top-level field / default.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embed_model: Option<String>,
    /// Embedding dimension — authoritative when set (resolves into top-level `embed_dim`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embed_dim: Option<u32>,
    /// Name of the env var holding the API key (the key itself never lives in boring.json).
    pub api_key_env: String,
    pub bootstrap: Bootstrap,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            provider: Provider::default(),
            base_url: DEFAULT_LLM_BASE_URL.to_owned(),
            model: DEFAULT_CHAT_MODEL.to_owned(),
            embed_model: None,
            embed_dim: None,
            api_key_env: DEFAULT_API_KEY_ENV.to_owned(),
            bootstrap: Bootstrap::default(),
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

/// How an agent adapter is wired into the host/IDE. Mirrors the `adapter` field in boring.json.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Adapter {
    /// Claude Code style — session-end (and optionally prompt-submit) hooks.
    #[default]
    SessionEnd,
    /// Prompt-submit hook only.
    PromptSubmit,
    /// MCP-only agent (Cursor, Codex, Windsurf, Claude Desktop, …).
    McpOnly,
    /// Background cron ingestion (hermes-agent).
    Cron,
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
    pub adapter: Adapter,
    #[serde(default)]
    pub format: String,
    #[serde(default)]
    pub paths: Vec<String>,
    /// Optional override for the agent's settings file path (e.g. MCP config or Claude Code
    /// settings.json). When absent, the wiring script uses per-agent defaults.
    #[serde(default)]
    pub settings_path: Option<String>,
}

impl Default for AgentSource {
    fn default() -> Self {
        Self {
            id: String::new(),
            enabled: true,
            adapter: Adapter::default(),
            format: String::new(),
            paths: Vec::new(),
            settings_path: None,
        }
    }
}

const fn default_true() -> bool {
    true
}

impl BoringConfig {
    /// Load config from `path`, or discover it via `BORING_CONFIG` / `BORING_HOME` / cwd.
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
                    adapter: Adapter::SessionEnd,
                    format: "claude-json".to_owned(),
                    paths,
                    settings_path: None,
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

        let mut config: BoringConfig =
            serde_json::from_value(value).context("deserialize boring.json")?;
        config.resolve_embed();
        Ok(config)
    }

    /// Resolve the embedding model/dim SSOT. The `llm` block (v2) is authoritative when it sets either
    /// field; otherwise the (legacy v1) top-level field stays. After this, the rest of the kernel reads
    /// `embed_model`/`embed_dim` as the single source — and the `llm` block is backfilled so `drudge
    /// config` output is internally consistent.
    fn resolve_embed(&mut self) {
        if let Some(m) = self.llm.embed_model.clone() {
            self.embed_model = m;
        } else {
            self.llm.embed_model = Some(self.embed_model.clone());
        }
        if let Some(d) = self.llm.embed_dim {
            self.embed_dim = d;
        } else {
            self.llm.embed_dim = Some(self.embed_dim);
        }
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

    /// Return a canonical project/repo slug.
    ///
    /// 1. If a repo rule has a non-empty `name` and its `match` is a substring of
    ///    `raw_repo` (case-insensitive), use that name.
    /// 2. Strip an org prefix (`org/repo` → `repo`).
    /// 3. Strip a trailing `.git`.
    /// 4. Otherwise return as-is.
    pub fn canonical_repo(&self, raw_repo: &str) -> String {
        let repo = raw_repo.trim();
        if repo.is_empty() {
            return String::new();
        }
        let repo = repo.strip_suffix(".git").unwrap_or(repo);
        let lowered = repo.to_lowercase();
        for rule in &self.repos {
            let matcher = rule.matcher.trim();
            let name = rule.name.trim();
            if !matcher.is_empty() && !name.is_empty() && lowered.contains(&matcher.to_lowercase())
            {
                return name.to_owned();
            }
        }
        if let Some(idx) = repo.rfind('/') {
            return repo[idx + 1..].trim().to_owned();
        }
        repo.to_owned()
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
    if let Some(home) = env_alias("BORING_HOME", "OMB_HOME") {
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

    use super::{
        Adapter, AgentSource, Bootstrap, BoringConfig, NoteLang, Origin, Provider, RepoRule,
    };

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
    fn canonical_repo_collapses_org_prefix_and_honors_rule_name() {
        let cfg = BoringConfig::from_str(
            r#"{
                "schema_version": 1,
                "repos": [
                    {"match": "marketboro", "origin": "company"},
                    {"match": "jazz1x/oh-my-boring", "name": "oh-my-boring", "origin": "personal"}
                ]
            }"#,
        )
        .unwrap();
        // org prefix stripped when no explicit name
        assert_eq!(
            cfg.canonical_repo("marketboro/foodspring-front"),
            "foodspring-front"
        );
        assert_eq!(cfg.canonical_repo("foodspring-front"), "foodspring-front");
        // explicit rule name wins
        assert_eq!(cfg.canonical_repo("jazz1x/oh-my-boring"), "oh-my-boring");
        // .git suffix removed
        assert_eq!(
            cfg.canonical_repo("git@github.com:acme/widget.git"),
            "widget"
        );
        // empty stays empty
        assert_eq!(cfg.canonical_repo(""), "");
    }

    #[test]
    fn defaults_when_file_missing() {
        let cfg = BoringConfig::load(Some(std::path::Path::new("/nonexistent/boring.json")))
            .expect("missing file should return defaults");
        assert_eq!(cfg.schema_version, 2);
        assert_eq!(cfg.note_lang, NoteLang::Auto);
        assert!(cfg.repos.is_empty());
        assert!(cfg.agents.is_empty());
    }

    #[test]
    fn llm_block_defaults_when_absent() {
        // v1-style config (no llm block) → default provider/connection, embed resolves from top-level.
        let cfg = BoringConfig::from_str(
            r#"{"schema_version": 1, "embed_model": "nomic-embed-text", "embed_dim": 768}"#,
        )
        .unwrap();
        assert_eq!(cfg.llm.provider, Provider::Ollama);
        assert_eq!(cfg.llm.base_url, "http://host.docker.internal:11434/v1");
        assert_eq!(cfg.llm.model, "gemma4:12b");
        assert_eq!(cfg.llm.api_key_env, "BORING_LLM_API_KEY");
        assert_eq!(cfg.llm.bootstrap, Bootstrap::Auto);
        // top-level embed is backfilled into the llm block so `drudge config` is consistent.
        assert_eq!(cfg.embed_model, "nomic-embed-text");
        assert_eq!(cfg.embed_dim, 768);
        assert_eq!(cfg.llm.embed_model.as_deref(), Some("nomic-embed-text"));
        assert_eq!(cfg.llm.embed_dim, Some(768));
    }

    #[test]
    fn llm_block_parses_and_is_authoritative_for_embed() {
        let cfg = BoringConfig::from_str(
            r#"{
                "schema_version": 2,
                "embed_model": "bge-m3",
                "embed_dim": 1024,
                "llm": {
                    "provider": "lmstudio",
                    "base_url": "http://localhost:1234/v1",
                    "model": "qwen2.5-coder",
                    "embed_model": "text-embedding-3-small",
                    "embed_dim": 1536,
                    "api_key_env": "MY_KEY",
                    "bootstrap": "manual"
                }
            }"#,
        )
        .unwrap();
        assert_eq!(cfg.llm.provider, Provider::Lmstudio);
        assert_eq!(cfg.llm.base_url, "http://localhost:1234/v1");
        assert_eq!(cfg.llm.model, "qwen2.5-coder");
        assert_eq!(cfg.llm.api_key_env, "MY_KEY");
        assert_eq!(cfg.llm.bootstrap, Bootstrap::Manual);
        // llm block wins over the top-level embed fields (v2 SSOT).
        assert_eq!(cfg.embed_model, "text-embedding-3-small");
        assert_eq!(cfg.embed_dim, 1536);
    }

    #[test]
    fn provider_roundtrips_kebab_case() {
        for (s, expected) in [
            ("ollama", Provider::Ollama),
            ("lmstudio", Provider::Lmstudio),
            ("openai-compatible", Provider::OpenaiCompatible),
        ] {
            let cfg = BoringConfig::from_str(&format!(
                r#"{{"schema_version": 2, "llm": {{"provider": "{s}"}}}}"#
            ))
            .unwrap();
            assert_eq!(cfg.llm.provider, expected);
            assert_eq!(cfg.llm.provider.as_str(), s);
        }
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
                {"id": "claude-code", "enabled": true, "adapter": "session-end", "format": "claude-json", "paths": ["~/.claude/projects"]}
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
        assert_eq!(cfg.agents[0].adapter, Adapter::SessionEnd);
    }

    #[test]
    fn adapter_roundtrips_and_defaults_to_session_end() {
        for (s, expected) in [
            ("session-end", Adapter::SessionEnd),
            ("prompt-submit", Adapter::PromptSubmit),
            ("mcp-only", Adapter::McpOnly),
            ("cron", Adapter::Cron),
        ] {
            let cfg = BoringConfig::from_str(&format!(
                r#"{{"schema_version": 1, "agents": [{{"id": "x", "adapter": "{s}"}}]}}"#
            ))
            .unwrap();
            assert_eq!(cfg.agents[0].adapter, expected);
        }
        // omitted → default
        let cfg =
            BoringConfig::from_str(r#"{"schema_version": 1, "agents": [{"id": "x"}]}"#).unwrap();
        assert_eq!(cfg.agents[0].adapter, Adapter::SessionEnd);
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
                    adapter: Adapter::SessionEnd,
                    format: "claude-json".into(),
                    paths: vec!["~/.claude/projects".into()],
                    settings_path: None,
                },
                AgentSource {
                    id: "b".into(),
                    enabled: false,
                    adapter: Adapter::SessionEnd,
                    format: "claude-json".into(),
                    paths: vec!["~/other".into()],
                    settings_path: None,
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
