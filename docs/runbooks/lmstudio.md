# LM Studio Runbook

## Purpose

Use LM Studio as the local OpenAI-compatible backend for ohmyboring without changing the engine path. The engine still calls `/v1/chat/completions` and `/v1/embeddings`; only the provider bootstrap and model ids change.

## Preconditions

- LM Studio is installed.
- The local server is enabled in LM Studio Developer mode.
- One chat model and one embedding model are loaded.
- `jq`, `curl`, Docker, and `make` are available.

## Configuration

Set `boring.json` to `lmstudio` and use `host.docker.internal` for the Docker runtime:

```json
{
  "llm": {
    "provider": "lmstudio",
    "base_url": "http://host.docker.internal:1234/v1",
    "model": "<exact chat model id>",
    "embed_model": "<exact embedding model id>",
    "embed_dim": 768,
    "api_key_env": "BORING_LLM_API_KEY",
    "bootstrap": "manual"
  }
}
```

Use the exact ids reported by LM Studio:

```bash
curl -s http://localhost:1234/v1/models | jq -r '.data[].id'
```

If the LM Studio CLI is installed, the same check is visible from the host:

```bash
~/.lmstudio/bin/lms ls
~/.lmstudio/bin/lms ps
```

Seeing only an embedding model, for example `text-embedding-nomic-embed-text-v1.5`, is not enough. ohmyboring needs one chat model for `/v1/chat/completions` and one embedding model for `/v1/embeddings`; `make verify-llm` must fail when the configured chat model is missing.

## Verification

```bash
make verify-llm
make up
make doctor
make readiness
```

Expected result:

- `make verify-llm` finds the provider script, reaches `/v1/models`, and sees both configured model ids.
- `make verify-llm` posts to `/v1/embeddings` and confirms the actual vector length equals `llm.embed_dim`.
- `make doctor` reports the engine healthy, the write door open, and current worker/marker state.
- `make readiness` is green before you rely on a scheduled morning briefing; it fails on provider/embed mismatch, worker failure, stale markers, or stale newest notes.
- If Hermes/Codex ingestion is enabled, `make doctor` also reports the Codex worker state.

## Embedding Dimension

The embedding model dimension is part of the storage contract. Common values:

| Model | `embed_dim` |
| --- | ---: |
| `bge-m3` | 1024 |
| `nomic-embed-text` / `text-embedding-nomic-embed-text-v1.5` | 768 |
| `text-embedding-3-small` | 1536 |

When changing `llm.embed_model`, update `llm.embed_dim` and run `make reset` before relying on vector mode. Wiki-first recall still reads markdown directly, but vector search, claims, graph, status, and brief depend on the vector store shape.

For the current 1024d release path, do not call LM Studio vector-ready unless `/v1/models` lists `bge-m3` and `make verify-llm` reports an actual embedding dimension of 1024. `text-embedding-nomic-embed-text-v1.5` is 768d; using it is a deliberate reset/re-index decision, not a silent substitute for `bge-m3`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `/v1/models` returns nothing | Start the LM Studio local server and load models in the app. |
| `/v1/models` shows only an embedding model | Download and load a chat model, then set `llm.model` to that exact id. |
| `make verify-llm` cannot find the model | Copy the exact id from `/v1/models`; display names are not enough. |
| `make verify-llm` reports an actual dimension mismatch | The server is returning a different embedding shape than `llm.embed_dim`; either load the intended model or change `embed_dim` and run `make reset`. |
| Docker cannot reach LM Studio | Use `http://host.docker.internal:1234/v1` in `boring.json`, not `localhost`. |
| Host benchmark cannot reach LM Studio | Use `http://localhost:1234/v1` with `scripts/bench-llm.py --base-url`. |
| Embedding upsert fails | `llm.embed_dim` does not match the embedding model; update it and reset the vector DB. |
| `make readiness` reports stale markers or stale newest note | Treat the scheduled briefing as not ready. Inspect `~/.cache/boring-distill`, verify the Codex/Hermes workers, and reconcile the stale marker or ingestion gap before relying on the brief. |
