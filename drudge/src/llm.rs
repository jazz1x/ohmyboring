//! LLM 클라이언트 — OpenAI-호환 `/v1` (embeddings + chat/completions).
//! 어떤 OpenAI-호환 서버와도 동작: Ollama(`/v1`)·LM Studio·vLLM·llama.cpp server 등.
//! 런타임·모델은 env 로 교체(`DRUDGE_LLM_BASE_URL`/`DRUDGE_LLM_MODEL`/`DRUDGE_EMBED_MODEL`).
//! `think:false` = Ollama gemma 의 추론모드를 끄는 확장 필드(지연 회피). OpenAI 호환 서버는
//! 미지의 body 필드를 무시(스펙) → 타 런타임 호환성 깨지 않음.
use anyhow::{Context, Result};
use serde::Deserialize;

pub struct Llm {
    base_url: String, // OpenAI-호환 base — 예: http://localhost:11434/v1
    api_key: Option<String>,
    embed_model: String,
    chat_model: String,
    client: reqwest::Client,
}

impl Llm {
    pub fn from_env() -> Self {
        // DRUDGE_LLM_BASE_URL 미설정 시 Ollama 로컬 /v1 로 폴백(기본 런타임).
        let base_url = std::env::var("DRUDGE_LLM_BASE_URL")
            .unwrap_or_else(|_| "http://localhost:11434/v1".into());
        Self {
            base_url: base_url.trim_end_matches('/').to_owned(),
            // 인증 필요한 provider(OpenAI 등)용. Ollama/LM Studio 는 불필요 → 미설정이면 헤더 생략.
            api_key: std::env::var("DRUDGE_LLM_API_KEY")
                .ok()
                .filter(|s| !s.is_empty()),
            embed_model: std::env::var("DRUDGE_EMBED_MODEL").unwrap_or_else(|_| "bge-m3".into()),
            chat_model: std::env::var("DRUDGE_LLM_MODEL").unwrap_or_else(|_| "gemma4:12b".into()),
            client: reqwest::Client::new(),
        }
    }

    /// `{base}{path}` 로 POST 요청 빌더. api_key 있으면 Bearer 인증 부착.
    fn post(&self, path: &str) -> reqwest::RequestBuilder {
        let r = self.client.post(format!("{}{path}", self.base_url));
        match &self.api_key {
            Some(k) => r.bearer_auth(k),
            None => r,
        }
    }

    /// 텍스트 1개 임베딩 → 벡터(bge-m3 = 1024차원). OpenAI `/v1/embeddings` 형식.
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

    /// 비-스트리밍 생성. OpenAI `/v1/chat/completions`(system+user 메시지).
    /// `think:false` 로 thinking 모드 차단(Ollama 대상, 비대상 서버는 무시).
    pub async fn generate(&self, system: &str, prompt: &str) -> Result<String> {
        #[derive(Deserialize)]
        struct Message {
            content: String,
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
            .map(|c| c.message.content)
            .context("chat 응답에 choices[0] 없음")
    }
}
