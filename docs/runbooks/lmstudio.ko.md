# LM Studio 런북

## 목적

LM Studio를 ohmyboring의 로컬 OpenAI-compatible 백엔드로 사용합니다. 엔진은 그대로 `/v1/chat/completions`와 `/v1/embeddings`를 호출하며, provider 부트스트랩과 모델 id만 바뀝니다.

## 사전 조건

- LM Studio가 설치되어 있어야 합니다.
- LM Studio Developer 모드에서 로컬 서버가 켜져 있어야 합니다.
- chat 모델 1개와 embedding 모델 1개가 로드되어 있어야 합니다.
- `jq`, `curl`, Docker, `make`를 사용할 수 있어야 합니다.

## 설정

`boring.json`의 provider를 `lmstudio`로 두고, Docker 런타임에서는 `host.docker.internal`을 사용합니다:

```json
{
  "llm": {
    "provider": "lmstudio",
    "base_url": "http://host.docker.internal:1234/v1",
    "model": "<정확한 chat model id>",
    "embed_model": "<정확한 embedding model id>",
    "embed_dim": 768,
    "api_key_env": "BORING_LLM_API_KEY",
    "bootstrap": "manual"
  }
}
```

LM Studio가 반환하는 id를 그대로 사용합니다:

```bash
curl -s http://localhost:1234/v1/models | jq -r '.data[].id'
```

LM Studio CLI가 설치되어 있으면 호스트에서도 같은 상태를 확인할 수 있습니다:

```bash
~/.lmstudio/bin/lms ls
~/.lmstudio/bin/lms ps
```

`text-embedding-nomic-embed-text-v1.5` 같은 embedding 모델만 보이는 것은 충분하지 않습니다. ohmyboring은 `/v1/chat/completions`용 chat 모델 1개와 `/v1/embeddings`용 embedding 모델 1개가 모두 필요하며, 설정된 chat 모델이 없으면 `make verify-llm`은 실패해야 정상입니다.

## 검증

```bash
make verify-llm
make up
make doctor
```

기대 결과:

- `make verify-llm`이 provider 스크립트를 찾고, `/v1/models`에 접근하며, 설정된 두 모델 id를 모두 확인합니다.
- `make doctor`가 엔진 정상과 write door open을 보고합니다.
- Hermes/Codex 적재가 켜져 있으면 `make doctor`가 Codex 워커 상태도 함께 보여줍니다.

## Embedding 차원

Embedding 모델 차원은 저장소 계약입니다. 흔한 값:

| 모델 | `embed_dim` |
| --- | ---: |
| `bge-m3` | 1024 |
| `nomic-embed-text` / `text-embedding-nomic-embed-text-v1.5` | 768 |
| `text-embedding-3-small` | 1536 |

`llm.embed_model`을 바꿀 때는 `llm.embed_dim`도 맞게 바꾸고, vector 모드를 믿기 전에 `make reset`을 실행합니다. wiki-first recall은 마크다운을 직접 읽지만, vector search, claims, graph, status, brief는 vector store 형태에 의존합니다.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| `/v1/models`가 비어 있음 | LM Studio 로컬 서버를 켜고 앱에서 모델을 로드합니다. |
| `/v1/models`에 embedding 모델만 보임 | chat 모델을 다운로드하고 로드한 뒤 `llm.model`에 정확한 id를 설정합니다. |
| `make verify-llm`이 모델을 못 찾음 | `/v1/models`의 정확한 id를 복사합니다. 표시 이름만으로는 부족합니다. |
| Docker가 LM Studio에 접근 못 함 | `boring.json`에는 `localhost`가 아니라 `http://host.docker.internal:1234/v1`을 씁니다. |
| 호스트 벤치마크가 LM Studio에 접근 못 함 | `scripts/bench-llm.py --base-url`에는 `http://localhost:1234/v1`을 씁니다. |
| embedding upsert 실패 | `llm.embed_dim`이 embedding 모델과 맞지 않습니다. 차원을 수정하고 vector DB를 reset합니다. |
