//! LLM client — OpenAI-compatible `/v1` (embeddings + chat/completions).
//! Works with any OpenAI-compatible server: Ollama (`/v1`) · LM Studio · vLLM · llama.cpp server, etc.
//! Runtime/model are swappable via env (`DRUDGE_LLM_BASE_URL`/`DRUDGE_LLM_MODEL`/`DRUDGE_EMBED_MODEL`).
//! `think:false` = an extension field that turns off Ollama gemma's reasoning mode (avoids latency). OpenAI-compatible servers
//! ignore unknown body fields (per spec) → doesn't break compatibility with other runtimes.
use anyhow::{Context, Result};
use serde::Deserialize;

pub struct Llm {
    base_url: String, // OpenAI-compatible base — e.g. http://localhost:11434/v1
    api_key: Option<String>,
    embed_model: String,
    chat_model: String,
    client: reqwest::Client,
}

impl Llm {
    pub fn from_env() -> Self {
        // If DRUDGE_LLM_BASE_URL is unset, fall back to Ollama local /v1 (the default runtime).
        let base_url = std::env::var("DRUDGE_LLM_BASE_URL")
            .unwrap_or_else(|_| "http://localhost:11434/v1".into());
        Self {
            base_url: base_url.trim_end_matches('/').to_owned(),
            // For providers that require auth (OpenAI, etc.). Ollama/LM Studio don't need it → omit the header if unset.
            api_key: std::env::var("DRUDGE_LLM_API_KEY")
                .ok()
                .filter(|s| !s.is_empty()),
            embed_model: std::env::var("DRUDGE_EMBED_MODEL").unwrap_or_else(|_| "bge-m3".into()),
            chat_model: std::env::var("DRUDGE_LLM_MODEL").unwrap_or_else(|_| "gemma4:12b".into()),
            client: reqwest::Client::new(),
        }
    }

    /// POST request builder to `{base}{path}`. Attaches Bearer auth if api_key is present.
    fn post(&self, path: &str) -> reqwest::RequestBuilder {
        let r = self.client.post(format!("{}{path}", self.base_url));
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
            .context("embeddings 응답에 data[0] 없음")
    }

    /// Non-streaming generation. OpenAI `/v1/chat/completions` (system+user messages).
    /// Blocks thinking mode via `think:false` (targets Ollama; non-target servers ignore it).
    pub async fn generate(&self, system: &str, prompt: &str) -> Result<String> {
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
                "think": false
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
            .context("chat 응답에 choices[0] 없음")
    }
}
