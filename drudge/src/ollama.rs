//! Ollama HTTP 클라이언트 — 임베딩(bge-m3) + 생성(gemma4:12b).
//! 생성은 `think:false` 고정 — thinking 모델(gemma4 등)의 추론모드를 끈다(지연 6~8배 회피).
//! 비-thinking 모델(qwen2.5 등)은 이 파라미터를 무시하므로 모델 분기 불필요(SSOT).
use anyhow::Result;
use serde::Deserialize;

pub struct Ollama {
    host: String,
    embed_model: String,
    llm_model: String,
    client: reqwest::Client,
}

impl Ollama {
    pub fn from_env() -> Self {
        Self {
            host: std::env::var("OLLAMA_HOST").unwrap_or_else(|_| "http://localhost:11434".into()),
            embed_model: std::env::var("DRUDGE_EMBED_MODEL").unwrap_or_else(|_| "bge-m3".into()),
            llm_model: std::env::var("DRUDGE_LLM_MODEL").unwrap_or_else(|_| "gemma4:12b".into()),
            client: reqwest::Client::new(),
        }
    }

    /// 텍스트 1개 임베딩 → 벡터(bge-m3 = 1024차원).
    pub async fn embed(&self, text: &str) -> Result<Vec<f32>> {
        #[derive(Deserialize)]
        struct R {
            embedding: Vec<f32>,
        }
        let r: R = self
            .client
            .post(format!("{}/api/embeddings", self.host))
            .json(
                &serde_json::json!({"model": self.embed_model, "prompt": text, "keep_alive": "5m"}),
            )
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(r.embedding)
    }

    /// 비-스트리밍 생성. `think:false` 로 thinking 모드 차단(지연 회피, 비대상 모델은 무시).
    pub async fn generate(&self, system: &str, prompt: &str) -> Result<String> {
        #[derive(Deserialize)]
        struct R {
            response: String,
        }
        let r: R = self
            .client
            .post(format!("{}/api/generate", self.host))
            .json(&serde_json::json!({
                "model": self.llm_model, "system": system, "prompt": prompt,
                "stream": false, "think": false, "keep_alive": "5m"
            }))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        Ok(r.response)
    }
}
