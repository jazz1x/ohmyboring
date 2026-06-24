//! LLM client — OpenAI-compatible `/v1` (embeddings + chat/completions).
//! Works with any OpenAI-compatible server: Ollama (`/v1`) · LM Studio · vLLM · llama.cpp server, etc.
//! Runtime/model are swappable via env (`DRUDGE_LLM_BASE_URL`/`DRUDGE_LLM_MODEL`/`DRUDGE_EMBED_MODEL`).
//! `reasoning_effort:"none"` = OpenAI-standard knob that turns off reasoning/thinking mode (avoids latency).
//! Verified on Ollama `/v1`: `think:false` is dropped there (only works on native `/api/chat`), but
//! `reasoning_effort:"none"` is honored (0.6s vs 8s on gemma4). Servers that don't reason ignore it gracefully.
use std::time::Duration;

use anyhow::{Context, Result};
use serde::Deserialize;
use tokio::time::sleep;

use crate::config::env_alias;

pub struct Llm {
    base_url: String, // OpenAI-compatible base — e.g. http://localhost:11434/v1
    api_key: Option<String>,
    embed_model: String,
    chat_model: String,
    client: reqwest::Client,
}

impl Llm {
    /// Build from config + env. `boring.json`'s `llm` block is the declarative SSOT for the connection
    /// (base_url/model) and the embed model (policy SSOT, kernel A's sole model). Runtime env still
    /// overrides — `BORING_LLM_*` is the canonical prefix, `DRUDGE_LLM_*` a deprecated alias (one cycle).
    /// The chat model is used only by `ask`/`brief` synthesis (the one allowed generation path).
    pub fn from_config(cfg: &crate::config::BoringConfig) -> Self {
        let base_url = env_alias("BORING_LLM_BASE_URL", "DRUDGE_LLM_BASE_URL")
            .unwrap_or_else(|| cfg.llm.base_url.clone());
        let chat_model = env_alias("BORING_LLM_MODEL", "DRUDGE_LLM_MODEL")
            .unwrap_or_else(|| cfg.llm.model.clone());
        // API key: read the env var NAMED by boring.json (api_key_env) so the secret never lands in
        // the config file. Legacy DRUDGE_LLM_API_KEY stays as a fallback. Ollama/LM Studio omit it.
        let api_key = std::env::var(&cfg.llm.api_key_env)
            .ok()
            .or_else(|| std::env::var("DRUDGE_LLM_API_KEY").ok())
            .filter(|s| !s.is_empty());
        Self {
            base_url: base_url.trim_end_matches('/').to_owned(),
            api_key,
            embed_model: cfg.embed_model.clone(),
            chat_model,
            client: reqwest::Client::new(),
        }
    }

    /// POST request builder to `{base}{path}`. Attaches Bearer auth if api_key is present.
    /// IO-boundary timeout: caps a hung/slow LLM (network boundary) so callers fail fast
    /// instead of blocking forever — e.g. `/brief` synthesis on a reasoning model.
    fn post(&self, path: &str) -> reqwest::RequestBuilder {
        // 3-min cap. clippy wants a larger unit, but std Duration has no from_mins (unstable),
        // so seconds is the readable stable form here.
        #[allow(clippy::unreadable_literal)]
        const LLM_TIMEOUT_SECS: u64 = 180;
        let r = self
            .client
            .post(format!("{}{path}", self.base_url))
            .timeout(Duration::from_secs(LLM_TIMEOUT_SECS));
        match &self.api_key {
            Some(k) => r.bearer_auth(k),
            None => r,
        }
    }

    /// Embed a single text → vector (bge-m3 = 1024 dimensions). OpenAI `/v1/embeddings` format.
    pub async fn embed(&self, text: &str) -> Result<Vec<f32>> {
        #[derive(Deserialize)]
        struct Embedding {
            embedding: Vec<f32>,
        }
        #[derive(Deserialize)]
        struct R {
            data: Vec<Embedding>,
        }
        let r: R = self
            .post("/embeddings")
            .json(&serde_json::json!({"model": self.embed_model, "input": text}))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        r.data
            .into_iter()
            .next()
            .map(|e| e.embedding)
            .context("embeddings response has no data[0]")
    }

    /// Non-streaming generation. OpenAI `/v1/chat/completions` (system+user messages).
    /// Disables reasoning via `reasoning_effort:"none"` (OpenAI-standard; honored by Ollama /v1).
    ///
    /// Local models (Ollama) occasionally return an empty `content` on cold-start/unload. Retry
    /// once after a short backoff so user-facing endpoints don't fail transiently.
    pub async fn generate(&self, system: &str, prompt: &str) -> Result<String> {
        const MAX_RETRIES: usize = 1;
        let mut last_err: Option<anyhow::Error> = None;
        for attempt in 0..=MAX_RETRIES {
            match self.generate_once(system, prompt).await {
                Ok(text) if !text.is_empty() => return Ok(text),
                Ok(_) => {
                    eprintln!(
                        "[llm] generate returned empty content (attempt {}), retrying…",
                        attempt + 1
                    );
                    if attempt < MAX_RETRIES {
                        sleep(Duration::from_millis(500)).await;
                        continue;
                    }
                    // ROP: an empty generation (model cold/unloaded) is a failure, not a success.
                    // Returning Ok("") would launder it into a 200 answer:"" citing sources — callers
                    // (ask/brief, smoke, MCP agents) could not tell "model down" from "answered nothing".
                    return Err(anyhow::anyhow!(
                        "llm returned empty content after {} attempt(s)",
                        MAX_RETRIES + 1
                    ));
                }
                Err(e) => {
                    last_err = Some(e);
                    if attempt < MAX_RETRIES {
                        sleep(Duration::from_millis(500)).await;
                    }
                }
            }
        }
        Err(last_err
            .unwrap_or_else(|| anyhow::anyhow!("llm generate failed without producing an error")))
    }

    async fn generate_once(&self, system: &str, prompt: &str) -> Result<String> {
        #[derive(Deserialize)]
        struct Message {
            // Optional: OpenAI-compatible servers may return `content: null`
            // (tool-call / reasoning-only finishes) — tolerate it as empty.
            content: Option<String>,
        }
        #[derive(Deserialize)]
        struct Choice {
            message: Message,
        }
        #[derive(Deserialize)]
        struct R {
            choices: Vec<Choice>,
        }
        let r: R = self
            .post("/chat/completions")
            .json(&serde_json::json!({
                "model": self.chat_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "stream": false,
                "reasoning_effort": "none"
            }))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        r.choices
            .into_iter()
            .next()
            .map(|c| c.message.content.unwrap_or_default())
            .context("chat response has no choices[0]")
    }
}
