use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Deserializer, Serialize, de};

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
    "code_index",
    "schedule",
    "vector",
    "collect",
    "hermes_cron_jobs",
    "checkout_roots",
    "card",
];
const SCHEDULE_FIELDS: &[&str] = &[
    "sync_hours",
    "compact_hours",
    "brief_hour",
    "ingest_tick_minutes",
    "queue_per_tick",
];
const VECTOR_FIELDS: &[&str] = &["backend", "surreal"];
const COLLECT_FIELDS: &[&str] = &[
    "window_hours",
    "min_bytes",
    "stable_age_seconds",
    "pending_ttl_seconds",
    "retry_ttl_seconds",
];
const DEFAULT_HERMES_CRON_JOBS: &str = r#"{
  "morning-card": {"enabled": true, "schedule": "0 8 * * *", "script": "run-morning-card.py", "deliver": "local"},
  "weekly-card": {"enabled": true, "schedule": "0 9 * * 1", "script": "run-weekly-card.py", "deliver": "local"},
  "weekly-briefing": {"enabled": false, "schedule": "0 9 * * 1", "script": "weekly-briefing.py"}
}"#;
const DEFAULT_EMBED_MODEL: &str = "bge-m3";
const DEFAULT_PG_DSN: &str = "postgresql://boring:boring@localhost:5432/boring";
const DEFAULT_EMBED_DIM: u32 = 1024;
const DEFAULT_LLM_BASE_URL: &str = "http://host.docker.internal:11434/v1";
const DEFAULT_CHAT_MODEL: &str = "gemma4:12b";
const DEFAULT_API_KEY_ENV: &str = "BORING_LLM_API_KEY";

#[must_use]
pub fn env_set(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

#[must_use]
pub fn pg_dsn() -> String {
    std::env::var("PG_DSN").unwrap_or_else(|_| DEFAULT_PG_DSN.to_owned())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct BoringConfig {
    pub schema_version: u32,
    pub note_lang: NoteLang,
    pub repos: Vec<RepoRule>,
    pub agents: Vec<AgentSource>,
    pub embed_model: String,
    pub embed_dim: u32,
    pub allow_company_origin: bool,
    pub llm: LlmConfig,
    pub code_index: CodeIndexConfig,
    pub schedule: ScheduleConfig,
    pub vector: VectorConfig,
    pub collect: CollectConfig,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hermes_cron_jobs: Option<serde_json::Value>,
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
            code_index: CodeIndexConfig::default(),
            schedule: ScheduleConfig::default(),
            vector: VectorConfig::default(),
            collect: CollectConfig::default(),
            hermes_cron_jobs: None,
        }
    }
}

/// An env variable that took over a boring.json value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvOverride {
    pub field: &'static str,
    pub env: &'static str,
}

type EnvLookup<'a> = &'a dyn Fn(&str) -> Option<String>;

/// boring.json `schedule`. These defaults are the only Rust definition; boring.schema.json and
/// src/ohmyboring/config.py carry the same numbers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ScheduleConfig {
    pub sync_hours: u64,
    pub compact_hours: u64,
    /// `None` (JSON null) = the daily brief is off.
    pub brief_hour: Option<u8>,
    pub ingest_tick_minutes: u64,
    pub queue_per_tick: u64,
}

impl Default for ScheduleConfig {
    fn default() -> Self {
        Self {
            sync_hours: 4,
            compact_hours: 24,
            brief_hour: Some(8),
            ingest_tick_minutes: 20,
            queue_per_tick: 3,
        }
    }
}

impl ScheduleConfig {
    fn validate(&self) -> Result<()> {
        for (name, value) in [
            ("sync_hours", self.sync_hours),
            ("compact_hours", self.compact_hours),
            ("ingest_tick_minutes", self.ingest_tick_minutes),
            ("queue_per_tick", self.queue_per_tick),
        ] {
            anyhow::ensure!(
                value >= 1,
                "boring.json schedule.{name} must be an integer >= 1"
            );
        }
        if let Some(hour) = self.brief_hour {
            anyhow::ensure!(
                hour <= 23,
                "boring.json schedule.brief_hour must be 0..=23 or null"
            );
        }
        Ok(())
    }

    /// The values the process actually runs on: env on top of the file. The `.max(1)` is the
    /// tokio interval guard — a zero period panics. An env brief hour `off` turns the brief
    /// off; any other value outside 0..=23 turns it off too but is reported in
    /// `brief_hour_error`, never swallowed.
    #[must_use]
    pub fn effective(self, env: EnvLookup<'_>) -> ScheduleEffective {
        let mut out = self;
        let mut overrides = Vec::new();
        let number = |name: &str| env(name).and_then(|v| v.parse::<u64>().ok());
        if let Some(v) = number("BORING_SYNC_HOURS") {
            out.sync_hours = v.max(1);
            overrides.push(EnvOverride {
                field: "sync_hours",
                env: "BORING_SYNC_HOURS",
            });
        }
        if let Some(v) = number("BORING_COMPACT_HOURS") {
            out.compact_hours = v.max(1);
            overrides.push(EnvOverride {
                field: "compact_hours",
                env: "BORING_COMPACT_HOURS",
            });
        }
        if let Some(v) = number("BORING_QUEUE_PER_TICK") {
            out.queue_per_tick = v;
            overrides.push(EnvOverride {
                field: "queue_per_tick",
                env: "BORING_QUEUE_PER_TICK",
            });
        }
        let mut brief_hour_error = None;
        if let Some(raw) = env("BORING_BRIEF_HOUR") {
            overrides.push(EnvOverride {
                field: "brief_hour",
                env: "BORING_BRIEF_HOUR",
            });
            let raw = raw.trim();
            out.brief_hour = None;
            if raw != "off" {
                match raw.parse::<u8>().ok().filter(|h| *h <= 23) {
                    Some(hour) => out.brief_hour = Some(hour),
                    None => brief_hour_error = Some(format!("brief_hour 잘못된 값: {raw}")),
                }
            }
        }
        ScheduleEffective {
            value: out,
            overrides,
            brief_hour_error,
        }
    }
}

/// `ScheduleConfig::effective`: the running values, the env names that took over, and the
/// reason the brief is off when its env value is neither `off` nor an hour 0..=23.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduleEffective {
    pub value: ScheduleConfig,
    pub overrides: Vec<EnvOverride>,
    pub brief_hour_error: Option<String>,
}

/// boring.json `vector.backend`, parsed once at the boundary. `pgvector` and `surreal` are
/// implemented; any other value is kept as `Unsupported` so config_get and the startup log can
/// name it, and the engine runs on pgvector regardless (an invalid value must not take the
/// engine down).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum VectorBackend {
    #[default]
    Pgvector,
    Surreal,
    Unsupported(String),
}

impl VectorBackend {
    fn parse(raw: &str) -> Self {
        match raw {
            "pgvector" => Self::Pgvector,
            "surreal" => Self::Surreal,
            other => Self::Unsupported(other.to_owned()),
        }
    }

    fn as_str(&self) -> &str {
        match self {
            Self::Pgvector => "pgvector",
            Self::Surreal => "surreal",
            Self::Unsupported(raw) => raw,
        }
    }
}

impl Serialize for VectorBackend {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for VectorBackend {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        String::deserialize(deserializer).map(|raw| Self::parse(&raw))
    }
}

/// A SurrealQL identifier that is spliced into `DEFINE NAMESPACE/DATABASE` (names cannot be bound
/// as variables), so it is checked once here and trusted below.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SurrealName(String);

impl SurrealName {
    fn parse(raw: &str) -> Result<Self, String> {
        let ok = !raw.is_empty()
            && raw
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
        if ok {
            Ok(Self(raw.to_owned()))
        } else {
            Err(format!("surreal 이름은 영문·숫자·_·- 만 허용: {raw:?}"))
        }
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Serialize for SurrealName {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for SurrealName {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = String::deserialize(deserializer)?;
        Self::parse(&raw).map_err(de::Error::custom)
    }
}

const DEFAULT_SURREAL_URL: &str = "http://localhost:8000";
const DEFAULT_SURREAL_NAME: &str = "boring";
const DEFAULT_SURREAL_USER: &str = "root";
const DEFAULT_SURREAL_PASSWORD_ENV: &str = "BORING_SURREAL_PASSWORD";
const SURREAL_URL_ENV: &str = "BORING_SURREAL_URL";

/// boring.json `vector.surreal`: where the SurrealDB server is. The password is never in the file:
/// `password_env` names the env variable that holds it (same rule as `llm.api_key_env`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct SurrealConfig {
    pub url: String,
    pub namespace: SurrealName,
    pub database: SurrealName,
    pub user: String,
    pub password_env: String,
}

impl Default for SurrealConfig {
    fn default() -> Self {
        Self {
            url: DEFAULT_SURREAL_URL.to_owned(),
            namespace: SurrealName(DEFAULT_SURREAL_NAME.to_owned()),
            database: SurrealName(DEFAULT_SURREAL_NAME.to_owned()),
            user: DEFAULT_SURREAL_USER.to_owned(),
            password_env: DEFAULT_SURREAL_PASSWORD_ENV.to_owned(),
        }
    }
}

/// `SurrealConfig` with env applied and the password read: what the process connects with.
#[derive(Clone, PartialEq, Eq)]
pub struct SurrealSettings {
    pub url: String,
    pub namespace: SurrealName,
    pub database: SurrealName,
    pub user: String,
    pub password: String,
}

/// Hand-written: the password must never reach a log.
impl std::fmt::Debug for SurrealSettings {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SurrealSettings")
            .field("url", &self.url)
            .field("namespace", &self.namespace)
            .field("database", &self.database)
            .finish_non_exhaustive()
    }
}

impl SurrealConfig {
    /// Env `BORING_SURREAL_URL` takes over `url`; the password comes from the variable named by
    /// `password_env`. A missing password is an error, not a default.
    pub fn settings(&self, env: EnvLookup<'_>) -> Result<SurrealSettings> {
        let password = env(&self.password_env).with_context(|| {
            format!(
                "vector.surreal: 비밀번호 env {} 가 비어 있음",
                self.password_env
            )
        })?;
        Ok(SurrealSettings {
            url: env(SURREAL_URL_ENV).unwrap_or_else(|| self.url.clone()),
            namespace: self.namespace.clone(),
            database: self.database.clone(),
            user: self.user.clone(),
            password,
        })
    }
}

/// boring.json `vector`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct VectorConfig {
    pub backend: VectorBackend,
    pub surreal: SurrealConfig,
}

/// Shown at startup and in `config_get` next to the effective backend when it is surreal: the one
/// path that stays on pgvector, and the frozen-copy caveat.
pub const SURREAL_PINNED_NOTES: [&str; 2] =
    ["recurrences 는 pgvector", "surreal: 한 번 채우기 시점 고정"];

/// `VectorConfig::effective`: the backend the process runs on, and the notice shown when the
/// file asked for one that is not implemented.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VectorEffective {
    pub backend: VectorBackend,
    pub backend_error: Option<String>,
}

impl VectorConfig {
    #[must_use]
    pub fn effective(&self) -> VectorEffective {
        match &self.backend {
            VectorBackend::Pgvector => VectorEffective {
                backend: VectorBackend::Pgvector,
                backend_error: None,
            },
            VectorBackend::Surreal => VectorEffective {
                backend: VectorBackend::Surreal,
                backend_error: None,
            },
            VectorBackend::Unsupported(raw) => VectorEffective {
                backend: VectorBackend::Pgvector,
                backend_error: Some(format!("vector.backend={raw} 미지원, pgvector 로 돎")),
            },
        }
    }
}

/// boring.json `collect` (session collectors; read by the Python side, shown here by config_get).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CollectConfig {
    pub window_hours: u64,
    pub min_bytes: u64,
    pub stable_age_seconds: u64,
    pub pending_ttl_seconds: u64,
    /// Absent = follows `pending_ttl_seconds`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_ttl_seconds: Option<u64>,
}

impl Default for CollectConfig {
    fn default() -> Self {
        Self {
            window_hours: 720,
            min_bytes: 20480,
            stable_age_seconds: 1800,
            pending_ttl_seconds: 1800,
            retry_ttl_seconds: None,
        }
    }
}

/// An env number scaled by `factor`: a whole number stays an integer (`720`, not `720.0`).
fn env_number(raw: &str, factor: u64) -> Option<serde_json::Value> {
    let raw = raw.trim();
    if let Ok(whole) = raw.parse::<u64>() {
        return whole.checked_mul(factor).map(serde_json::Value::from);
    }
    let fractional = raw.parse::<f64>().ok().filter(|v| v.is_finite())?;
    let scaled = fractional * f64::from(u32::try_from(factor).ok()?);
    serde_json::Number::from_f64(scaled).map(serde_json::Value::Number)
}

impl CollectConfig {
    fn validate(&self) -> Result<()> {
        for (name, value) in [
            ("window_hours", self.window_hours),
            ("pending_ttl_seconds", self.pending_ttl_seconds),
            ("retry_ttl_seconds", self.retry_ttl_seconds.unwrap_or(1)),
        ] {
            anyhow::ensure!(
                value >= 1,
                "boring.json collect.{name} must be an integer >= 1"
            );
        }
        Ok(())
    }

    /// File values with env on top, as JSON numbers. The env names are the collectors' own:
    /// `COLLECT_MIN_KB` is in KB, the pending/retry TTL also read `INGEST_*`.
    #[must_use]
    pub fn effective(&self, env: EnvLookup<'_>) -> (serde_json::Value, Vec<EnvOverride>) {
        let mut overrides = Vec::new();
        let mut pick = |field: &'static str,
                        names: &[&'static str],
                        factor: u64,
                        file: serde_json::Value|
         -> serde_json::Value {
            for name in names {
                if let Some(v) = env(name).and_then(|raw| env_number(&raw, factor)) {
                    overrides.push(EnvOverride { field, env: name });
                    return v;
                }
            }
            file
        };
        let window = pick(
            "window_hours",
            &["COLLECT_WINDOW_HOURS"],
            1,
            self.window_hours.into(),
        );
        let min_bytes = pick(
            "min_bytes",
            &["COLLECT_MIN_KB"],
            1024,
            self.min_bytes.into(),
        );
        let stable = pick(
            "stable_age_seconds",
            &["COLLECT_STABLE_AGE_SECONDS"],
            1,
            self.stable_age_seconds.into(),
        );
        let pending = pick(
            "pending_ttl_seconds",
            &["COLLECT_PENDING_TTL", "INGEST_PENDING_TTL"],
            1,
            self.pending_ttl_seconds.into(),
        );
        // An unset retry follows the pending value (after env), like the collectors do.
        let retry_file = self
            .retry_ttl_seconds
            .map_or_else(|| pending.clone(), serde_json::Value::from);
        let retry = pick(
            "retry_ttl_seconds",
            &["COLLECT_RETRY_TTL", "INGEST_RETRY_TTL"],
            1,
            retry_file,
        );
        (
            serde_json::json!({
                "window_hours": window,
                "min_bytes": min_bytes,
                "stable_age_seconds": stable,
                "pending_ttl_seconds": pending,
                "retry_ttl_seconds": retry,
            }),
            overrides,
        )
    }
}

fn overrides_json(overrides: &[EnvOverride]) -> serde_json::Value {
    overrides
        .iter()
        .map(|o| (o.field.to_owned(), serde_json::Value::from(o.env)))
        .collect::<serde_json::Map<_, _>>()
        .into()
}

/// Jobs that run on this machine but that boring.json does not manage. Listed by name; their
/// state is not read live (a static list — the live ones are known by name only).
fn unmanaged_jobs() -> serde_json::Value {
    serde_json::json!([
        {"name": "repair-judge", "kind": "hermes cron"},
        {"name": "advice-write", "kind": "hermes cron"},
        {"name": "backfill-sessions", "kind": "hermes cron"},
        {"name": "com.ohmyboring.night-drain", "kind": "launchd"},
        {
            "name": "com.olympus.collect",
            "kind": "launchd",
            "interval_seconds": 10800,
            "env": {"COLLECT_WINDOW_HOURS": "720", "COLLECT_MIN_KB": "20", "COLLECT_LIMIT": "1"},
            "note": "env 로 덮임: com.olympus.collect"
        }
    ])
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default)]
pub struct CodeIndexConfig {
    pub sources: Vec<CodeIndexSource>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CodeIndexSource {
    id: RepositoryId,
    name: RepositoryDisplayName,
    root: AbsoluteRepositoryRoot,
    language: CodeLanguage,
    #[serde(default)]
    enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize)]
#[serde(transparent)]
struct RepositoryId(String);

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
struct RepositoryDisplayName(String);

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
struct AbsoluteRepositoryRoot(PathBuf);

impl<'de> Deserialize<'de> for RepositoryId {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        if value.trim().is_empty() {
            return Err(de::Error::custom("code_index source id must not be empty"));
        }
        Ok(Self(value))
    }
}

impl<'de> Deserialize<'de> for RepositoryDisplayName {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        if value.trim().is_empty() {
            return Err(de::Error::custom(
                "code_index source display name must not be empty",
            ));
        }
        Ok(Self(value))
    }
}

impl<'de> Deserialize<'de> for AbsoluteRepositoryRoot {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = PathBuf::deserialize(deserializer)?;
        if !value.is_absolute() {
            return Err(de::Error::custom(
                "code_index source root must be an absolute path",
            ));
        }
        Ok(Self(value))
    }
}

impl<'de> Deserialize<'de> for CodeIndexConfig {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize, Default)]
        #[serde(default, deny_unknown_fields)]
        struct RawCodeIndexConfig {
            sources: Vec<CodeIndexSource>,
        }

        let raw = RawCodeIndexConfig::deserialize(deserializer)?;
        let mut ids = HashSet::new();
        for source in &raw.sources {
            if !ids.insert(source.id.as_str()) {
                return Err(de::Error::custom(format!(
                    "duplicate code_index source id: {}",
                    source.id.as_str()
                )));
            }
        }
        Ok(Self {
            sources: raw.sources,
        })
    }
}

impl CodeIndexSource {
    pub fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        root: PathBuf,
        language: CodeLanguage,
        enabled: bool,
    ) -> Result<Self> {
        let id = id.into();
        anyhow::ensure!(
            !id.trim().is_empty(),
            "code_index source id must not be empty"
        );
        let name = name.into();
        anyhow::ensure!(
            !name.trim().is_empty(),
            "code_index source display name must not be empty"
        );
        anyhow::ensure!(
            root.is_absolute(),
            "code_index source root must be an absolute path"
        );
        Ok(Self {
            id: RepositoryId(id),
            name: RepositoryDisplayName(name),
            root: AbsoluteRepositoryRoot(root),
            language,
            enabled,
        })
    }

    #[must_use]
    pub fn id(&self) -> &str {
        self.id.as_str()
    }

    #[must_use]
    pub fn name(&self) -> &str {
        self.name.as_str()
    }

    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root.0
    }

    #[must_use]
    pub const fn language(&self) -> CodeLanguage {
        self.language
    }

    #[must_use]
    pub const fn enabled(&self) -> bool {
        self.enabled
    }
}

impl RepositoryId {
    fn as_str(&self) -> &str {
        &self.0
    }
}

impl RepositoryDisplayName {
    fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CodeLanguage {
    Rust,
    Python,
    Shell,
}

impl CodeLanguage {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Rust => "rust",
            Self::Python => "python",
            Self::Shell => "shell",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Provider {
    #[default]
    Ollama,
    Lmstudio,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Bootstrap {
    #[default]
    Auto,
    Manual,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct LlmConfig {
    pub provider: Provider,
    pub base_url: String,
    pub model: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embed_model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embed_dim: Option<u32>,
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
    #[serde(rename = "match")]
    pub matcher: String,
    pub origin: Origin,
    #[serde(default)]
    pub name: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "kebab-case")]
pub enum Adapter {
    #[default]
    SessionEnd,
    PromptSubmit,
    McpOnly,
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

/// `Origin::from_str` rejected a value that names no origin; carries the trimmed input.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid origin: {0}")]
pub struct ParseOriginError(String);

impl std::str::FromStr for Origin {
    type Err = ParseOriginError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.trim() {
            "personal" => Ok(Self::Personal),
            "company" => Ok(Self::Company),
            "mirror" => Ok(Self::Mirror),
            "community" => Ok(Self::Community),
            other => Err(ParseOriginError(other.to_owned())),
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
    #[must_use]
    pub fn origins_excluded_by_policy(&self) -> Vec<String> {
        if self.allow_company_origin {
            Vec::new()
        } else {
            vec![Origin::Company.as_str().to_owned()]
        }
    }

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

    pub fn from_env() -> Self {
        Self::from_env_map(&std::env::vars().collect())
    }

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
                if k.starts_with('$') {
                    continue;
                }
                if !known.contains(k) {
                    eprintln!("[config] warning: unknown top-level field '{key}' in boring.json");
                }
            }
        }

        for (block, fields) in [
            ("schedule", SCHEDULE_FIELDS),
            ("vector", VECTOR_FIELDS),
            ("collect", COLLECT_FIELDS),
        ] {
            let Some(inner) = value.get(block).and_then(serde_json::Value::as_object) else {
                continue;
            };
            for key in inner.keys().filter(|k| !fields.contains(&k.as_str())) {
                eprintln!("[config] warning: unknown field '{key}' in boring.json {block}");
            }
        }

        let mut config: BoringConfig =
            serde_json::from_value(value).context("deserialize boring.json")?;
        config.schedule.validate()?;
        config.collect.validate()?;
        anyhow::ensure!(
            config
                .hermes_cron_jobs
                .as_ref()
                .is_none_or(serde_json::Value::is_object),
            "boring.json hermes_cron_jobs must be an object"
        );
        config.resolve_embed();
        Ok(config)
    }

    /// The `schedule` this process runs on: env over boring.json.
    #[must_use]
    pub fn schedule_effective(&self) -> ScheduleEffective {
        self.schedule.effective(&env_set)
    }

    /// What `config_get` returns: the config as parsed plus, for the schedule, collect and
    /// hermes_cron_jobs blocks, `file` (boring.json) next to `effective` (what runs), and the
    /// env names that took over a value. Jobs outside boring.json are listed as `unmanaged`.
    pub fn settings_view(&self, env: EnvLookup<'_>) -> serde_json::Result<serde_json::Value> {
        let mut view = serde_json::to_value(self)?;
        let schedule = self.schedule.effective(env);
        let mut schedule_effective = serde_json::to_value(schedule.value)?;
        if let Some(reason) = &schedule.brief_hour_error {
            schedule_effective["brief_hour_error"] = serde_json::Value::from(reason.as_str());
        }
        let (collect, collect_env) = self.collect.effective(env);
        let defaults: serde_json::Value = serde_json::from_str(DEFAULT_HERMES_CRON_JOBS)?;
        let hermes_file = self
            .hermes_cron_jobs
            .clone()
            .unwrap_or(serde_json::Value::Null);
        let hermes_effective = self.hermes_cron_jobs.clone().unwrap_or(defaults);
        view["schedule"] = serde_json::json!({
            "file": serde_json::to_value(self.schedule)?,
            "effective": schedule_effective,
            "overridden_by_env": overrides_json(&schedule.overrides),
        });
        let vector = self.vector.effective();
        let mut vector_effective = serde_json::json!({"backend": vector.backend});
        if let Some(reason) = &vector.backend_error {
            vector_effective["backend_error"] = serde_json::Value::from(reason.as_str());
        }
        if vector.backend == VectorBackend::Surreal {
            vector_effective["notes"] = serde_json::json!(SURREAL_PINNED_NOTES);
            vector_effective["surreal_url"] = serde_json::Value::from(
                env(SURREAL_URL_ENV).unwrap_or_else(|| self.vector.surreal.url.clone()),
            );
        }
        view["vector"] = serde_json::json!({
            "file": serde_json::to_value(&self.vector)?,
            "effective": vector_effective,
        });
        view["collect"] = serde_json::json!({
            "file": serde_json::to_value(self.collect)?,
            "effective": collect,
            "overridden_by_env": overrides_json(&collect_env),
            "overridden_by_unmanaged": {
                "window_hours": "com.olympus.collect",
                "min_bytes": "com.olympus.collect",
            },
        });
        view["hermes_cron_jobs"] = serde_json::json!({
            "file": hermes_file,
            "effective": hermes_effective,
        });
        view["unmanaged"] = unmanaged_jobs();
        Ok(view)
    }

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

    pub fn classify(&self, cwd: &str, remote_url: Option<&str>) -> (Origin, Option<String>) {
        let matchers: Vec<String> = self
            .repos
            .iter()
            .map(|r| r.matcher.to_lowercase())
            .collect();

        if let Some(url) = remote_url {
            let lowered = url.to_lowercase();
            for (i, rule) in self.repos.iter().enumerate() {
                if lowered.contains(&matchers[i]) {
                    let name = if rule.name.is_empty() {
                        derive_name_from_match(&rule.matcher, cwd, remote_url)
                    } else {
                        Some(rule.name.clone())
                    };
                    return (rule.origin, name);
                }
            }
        }

        let lowered = cwd.to_lowercase();
        for (i, rule) in self.repos.iter().enumerate() {
            if lowered.contains(&matchers[i]) {
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

pub fn discover_path() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("BORING_CONFIG") {
        return Some(PathBuf::from(p));
    }
    if let Some(home) = env_set("BORING_HOME") {
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
    if matcher.contains('/') {
        let parts: Vec<&str> = matcher.split('/').filter(|s| !s.is_empty()).collect();
        return parts.last().map(|s| (*s).to_owned());
    }
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

    use std::path::Path;

    use super::{
        Adapter, AgentSource, Bootstrap, BoringConfig, CodeIndexConfig, CodeLanguage, NoteLang,
        Origin, Provider, RepoRule,
    };

    #[test]
    fn origin_parse_roundtrips_and_rejects_unknown() {
        use std::str::FromStr;
        for s in ["personal", "company", "mirror", "community"] {
            assert_eq!(Origin::from_str(s).unwrap().as_str(), s);
        }
        assert_eq!(Origin::from_str("  company  ").unwrap(), Origin::Company);
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
                    {"match": "jazz1x/ohmyboring", "name": "ohmyboring", "origin": "personal"},
                    {"match": "jazz1x/oh-my-boring", "name": "ohmyboring", "origin": "personal"},
                    {"match": "oh-my-boring", "name": "ohmyboring", "origin": "personal"}
                ]
            }"#,
        )
        .unwrap();
        assert_eq!(
            cfg.canonical_repo("marketboro/foodspring-front"),
            "foodspring-front"
        );
        assert_eq!(cfg.canonical_repo("foodspring-front"), "foodspring-front");
        assert_eq!(cfg.canonical_repo("jazz1x/ohmyboring"), "ohmyboring");
        assert_eq!(cfg.canonical_repo("jazz1x/oh-my-boring"), "ohmyboring");
        assert_eq!(cfg.canonical_repo("oh-my-boring"), "ohmyboring");
        assert_eq!(
            cfg.canonical_repo("git@github.com:acme/widget.git"),
            "widget"
        );
        assert_eq!(cfg.canonical_repo(""), "");
    }

    #[test]
    fn defaults_when_file_missing() {
        let cfg = BoringConfig::load(Some(std::path::Path::new("/nonexistent/boring.json")))
            .expect("missing file should return defaults");
        assert_eq!(cfg.schema_version, 2);
        assert_eq!(cfg.note_lang, NoteLang::Auto);
        assert_eq!(cfg.repos, Vec::<RepoRule>::new());
        assert_eq!(cfg.agents, Vec::<AgentSource>::new());
    }

    #[test]
    fn llm_block_defaults_when_absent() {
        let cfg = BoringConfig::from_str(
            r#"{"schema_version": 1, "embed_model": "nomic-embed-text", "embed_dim": 768}"#,
        )
        .unwrap();
        assert_eq!(cfg.llm.provider, Provider::Ollama);
        assert_eq!(cfg.llm.base_url, "http://host.docker.internal:11434/v1");
        assert_eq!(cfg.llm.model, "gemma4:12b");
        assert_eq!(cfg.llm.api_key_env, "BORING_LLM_API_KEY");
        assert_eq!(cfg.llm.bootstrap, Bootstrap::Auto);
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
        let def = BoringConfig::from_str(r#"{"schema_version": 1}"#).unwrap();
        assert_eq!(def.embed_model, "bge-m3");
        assert_eq!(def.embed_dim, 1024);
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
    fn code_index_sources_are_explicit_and_separate_from_repo_rules() {
        let cfg = BoringConfig::from_str(
            r#"{
                "schema_version": 2,
                "repos": [{"match": "acme", "origin": "company", "name": "work"}],
                "code_index": {"sources": [{
                    "id": "widget",
                    "name": "Widget",
                    "root": "/src/widget",
                    "language": "rust",
                    "enabled": true
                }]}
            }"#,
        )
        .unwrap();
        assert_eq!(cfg.repos.len(), 1);
        assert_eq!(cfg.code_index.sources.len(), 1);
        assert_eq!(cfg.code_index.sources[0].id(), "widget");
        assert_eq!(cfg.code_index.sources[0].name(), "Widget");
        assert_eq!(cfg.code_index.sources[0].root(), Path::new("/src/widget"));
        assert_eq!(cfg.code_index.sources[0].language(), CodeLanguage::Rust);
        assert!(cfg.code_index.sources[0].enabled());
    }

    #[test]
    fn code_index_rejects_unsupported_language_and_duplicate_identity() {
        let unsupported = BoringConfig::from_str(
            r#"{"schema_version":2,"code_index":{"sources":[{"id":"x","name":"X","root":"/x","language":"cpp","enabled":true}]}}"#,
        );
        assert!(unsupported.is_err());

        let duplicate = BoringConfig::from_str(
            r#"{"schema_version":2,"code_index":{"sources":[
                {"id":"x","name":"X","root":"/x","language":"rust"},
                {"id":"x","name":"Other","root":"/other","language":"rust"}
            ]}}"#,
        );
        assert!(duplicate.is_err());

        let empty_id = BoringConfig::from_str(
            r#"{"schema_version":2,"code_index":{"sources":[{"id":" ","name":"X","root":"/x","language":"rust"}]}}"#,
        );
        assert!(empty_id.is_err());

        let relative_root = BoringConfig::from_str(
            r#"{"schema_version":2,"code_index":{"sources":[{"id":"x","name":"X","root":"relative/path","language":"rust"}]}}"#,
        );
        assert!(relative_root.is_err());
    }

    #[test]
    fn typed_code_index_config_rejects_duplicate_repository_ids() {
        let duplicate = serde_json::from_str::<CodeIndexConfig>(
            r#"{"sources":[
                {"id":"x","name":"X","root":"/x","language":"rust"},
                {"id":"x","name":"Other","root":"/other","language":"rust"}
            ]}"#,
        );
        assert!(duplicate.is_err());
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
    fn classify_prefers_remote_url_over_cwd() {
        let cfg = BoringConfig {
            repos: vec![
                RepoRule {
                    matcher: "acme-corp".into(),
                    origin: Origin::Company,
                    name: "acme-corp".into(),
                },
                RepoRule {
                    matcher: "personal".into(),
                    origin: Origin::Personal,
                    name: "personal".into(),
                },
            ],
            ..Default::default()
        };
        let (origin, name) = cfg.classify(
            "/Users/x/personal",
            Some("https://github.com/acme-corp/secret-project.git"),
        );
        assert_eq!(origin, Origin::Company);
        assert_eq!(name, Some("acme-corp".to_owned()));
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

    #[test]
    fn the_policy_decides_which_origins_a_reader_may_not_see() {
        let mut cfg = BoringConfig::default();
        assert!(!cfg.allow_company_origin);
        assert_eq!(
            cfg.origins_excluded_by_policy(),
            vec!["company".to_owned()],
            "with company origin disallowed, a caller passing the policy must exclude it"
        );

        cfg.allow_company_origin = true;
        assert!(
            cfg.origins_excluded_by_policy().is_empty(),
            "with company origin allowed, the policy excludes nothing"
        );
    }
}
