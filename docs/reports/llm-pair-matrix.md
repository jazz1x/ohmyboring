# ohmyboring LLM pair matrix: gemma4 vs qwen3

> Goal: pick a **fair, same-scale** pair from the Google `gemma4` family and the Alibaba `qwen3` family for each common MacBook RAM tier.
> If a tier has no viable model in one family, that cell is left empty.

## How to read the matrix

- **Default tags** use Ollama's default quantization (usually `q4_K_M` or equivalent). Disk sizes are taken from the Ollama library page.
- **Loaded memory** is roughly the same as disk size plus KV-cache. macOS itself plus typical apps need ~4 GB headroom, so we only recommend a model when its disk size leaves comfortable room.
- **Same-scale pairing** is based on parameter count and architecture, not raw disk size. MoE models are paired with MoE models.
- These are **recommendations for local Ollama**. They also apply to LM Studio / any OpenAI-compatible `/v1` endpoint that hosts the same quantized checkpoints.

## Pair matrix

| MacBook RAM | gemma4 (Google)              | disk   | qwen3 (Alibaba)      | disk   | Pairing note |
|------------:|------------------------------|-------:|----------------------|-------:|:-------------|
| 8 GB        | *(empty)*                    | —      | `qwen3:4b`           | 2.5 GB | Gemma4 has no practical 8 GB option: even `gemma4:e2b-it-q4_K_M` is ~7.2 GB disk. |
| 16 GB       | `gemma4:12b`                 | 7.6 GB | `qwen3:14b`          | 9.3 GB | Closest same-scale **dense** pair (12B vs 14B). `qwen3:8b` also fits here but is a tier smaller. |
| 24 GB       | `gemma4:26b-a4b`             | 18 GB  | `qwen3:30b-a3b`      | 19 GB  | Same-scale **MoE** pair: gemma4 26B total / 4B active vs qwen3 30B total / 3B active. |
| 32 GB       | `gemma4:31b`                 | 20 GB  | `qwen3:32b`          | 20 GB  | Dense flagship pair (~31B vs ~32B). |
| 48 GB       | `gemma4:31b`                 | 20 GB  | `qwen3:32b`          | 20 GB  | Same models as 32 GB tier, with headroom for long context and concurrent apps. |
| 64 GB+      | *(empty)*                    | —      | *(empty)*            | —      | No new recommended pair; `qwen3:235b-a22b` needs ~142 GB disk and is not practical locally. |

## Running the benchmark for a tier

```bash
# list tiers
python3 scripts/bench-llm.py --list-tiers

# 16 GB tier: gemma4:12b vs qwen3:14b
make bench-llm-tier TIER=16gb
# or directly
python3 scripts/bench-llm.py --tier 16gb

# pull missing models automatically
python3 scripts/bench-llm.py --tier 16gb --pull
```

## Measured results

Run on a **MacBook Pro (Apple M5 Pro, 48 GB RAM, macOS 26.5.1)** with local Ollama and default quantizations. Samples = 3 synthetic 3-turn transcripts; metrics come from `scripts/bench-llm.py`.

### 16 GB tier pair: `gemma4:12b` vs `qwen3:14b`

| model        | valid JSON | title Korean | 2+ sections | clean body | avg latency |
|--------------|-----------:|-------------:|------------:|-----------:|------------:|
| `gemma4:12b` | 100%       | 100%         | 100%        | 100%       | 15.76 s     |
| `qwen3:14b`  | 100%       | 100%         | 100%        | 100%       | 18.42 s     |

Both models pass the mechanical quality gate. `gemma4:12b` is slightly faster; `qwen3:14b` consistently emits an extra `## 남은 일` section, which is useful if you want the model to surface follow-ups automatically.

### Same-tier comparison: `qwen3:8b`

`qwen3:8b` also fits in 16 GB and runs faster, but it is a smaller model than `gemma4:12b`:

| model        | valid JSON | title Korean | 2+ sections | clean body | avg latency |
|--------------|-----------:|-------------:|------------:|-----------:|------------:|
| `qwen3:8b`   | 100%       | 100%         | 100%        | 100%       | 8.11 s      |

For pure note-taking quality the measured samples are equivalent, so pick `qwen3:8b` if you prioritize speed and `qwen3:14b`/`gemma4:12b` if you want larger capacity.

### Local embedding: `bge-m3`

`scripts/bench-embed.py` against the same Ollama host:

- dimension: 1024 (matches `embed_dim`)
- average single-text latency: **0.105 s**
- related pair cosine (`docker-cache` ↔ `rust-mutex`): **0.45**
- unrelated pair cosine (`docker-cache` ↔ `lunch`): **0.35**

The related pair is closer, so the embedding sanity check passes.

## Benchmark methodology

`scripts/bench-llm.py` reuses the production distillation prompt (`agents/shared/distill_core.py::_build_prompt`) and JSON extractor (`_extract_json`). It evaluates:

- valid JSON rate
- title language compliance (`note_lang`)
- body section coverage (`배경`, `시도`, `결과`, optionally `남은 일`)
- metadata leakage into the body (trailing `tags`/`tools`/`concepts`/`claims`)
- end-to-end latency

The current samples are synthetic 3-turn transcripts covering Docker layer caching, Rust async mutex deadlocks, and JWT clock skew. They are good enough for gatekeeping mechanical quality; hallucination and factual accuracy still need human review or a larger golden dataset.

## Notes on specific families

- **gemma4**: the public Ollama tags are `e2b`, `e4b`, `12b`, `26b-a4b` (MoE), and `31b`. The `e2b`/`e4b` experimental small tags are surprisingly heavy (~7–10 GB) because Gemma 4 is multimodal, so they are not competitive against `qwen3:4b` on memory-constrained machines.
- **qwen3**: the public Ollama tags include `0.6b`, `1.7b`, `4b`, `8b`, `14b`, `30b-a3b` (MoE), `32b`, and `235b-a22b` (MoE). The default `qwen3:30b` is the `a3b` MoE variant.

## LM Studio / remote endpoint

The benchmark talks to an OpenAI-compatible `/v1/chat/completions` endpoint, so setting `--base-url` works with LM Studio, vLLM, or cloud providers:

```bash
python3 scripts/bench-llm.py --tier 16gb --base-url http://localhost:1234/v1
```

`--pull` only makes sense for local Ollama and is ignored/skipped for remote endpoints because the script cannot inspect their model registry.
