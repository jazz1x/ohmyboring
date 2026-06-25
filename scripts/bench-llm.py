#!/usr/bin/env python3
"""Benchmark LLM distillation quality: gemma4 vs qwen3 by MacBook RAM tier.

Reuses the production distillation prompt from agents/shared/distill_core.py so the numbers
reflect real ohmyboring behavior. Evaluates:
  - valid JSON rate
  - title language compliance (note_lang)
  - body section coverage (배경/시도/결과)
  - metadata leakage into body (trailing tags/tools/concepts)
  - latency

Run by model:
  BORING_LLM_MODEL=gemma4:12b python3 scripts/bench-llm.py --model gemma4:12b
  BORING_LLM_MODEL=qwen3:8b python3 scripts/bench-llm.py --model qwen3:8b

Run by MacBook RAM tier (recommended pairs):
  python3 scripts/bench-llm.py --tier 16gb   # gemma4:12b vs qwen3:14b
  python3 scripts/bench-llm.py --tier 24gb   # gemma4:26b-a4b vs qwen3:30b-a3b
  python3 scripts/bench-llm.py --tier 32gb   # gemma4:31b vs qwen3:32b

See docs/reports/llm-pair-matrix.md for the full matrix.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from typing import Any, Optional

# Reuse the production distillation prompt / JSON extractor.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
import boring_config  # noqa: E402
import omb_env  # noqa: E402
from distill_core import _build_prompt, _extract_json  # noqa: E402

# Ollama tag sizes are rounded disk sizes from https://ollama.com/library/<model>/tags.
# Loaded memory is roughly the same plus KV-cache; leave ~4 GB for macOS + apps.
TIER_PAIRS: dict[str, dict[str, Any]] = {
    "8gb": {
        "ram_gb": 8,
        "gemma4": None,
        "qwen3": "qwen3:4b",
        "gemma4_size": None,
        "qwen3_size": "2.5 GB",
        "note": "Gemma4 has no practical 8 GB option: even e2b-it-q4_K_M is ~7.2 GB disk.",
    },
    "16gb": {
        "ram_gb": 16,
        "gemma4": "gemma4:12b",
        "qwen3": "qwen3:14b",
        "gemma4_size": "7.6 GB",
        "qwen3_size": "9.3 GB",
        "note": "Closest same-scale dense pair (12B vs 14B). qwen3:8b also fits but is a tier smaller.",
    },
    "24gb": {
        "ram_gb": 24,
        "gemma4": "gemma4:26b-a4b",
        "qwen3": "qwen3:30b-a3b",
        "gemma4_size": "18 GB",
        "qwen3_size": "19 GB",
        "note": "MoE pair: gemma4 26B total / 4B active vs qwen3 30B total / 3B active.",
    },
    "32gb": {
        "ram_gb": 32,
        "gemma4": "gemma4:31b",
        "qwen3": "qwen3:32b",
        "gemma4_size": "20 GB",
        "qwen3_size": "20 GB",
        "note": "Dense flagship pair (~31B vs ~32B).",
    },
    "48gb": {
        "ram_gb": 48,
        "gemma4": "gemma4:31b",
        "qwen3": "qwen3:32b",
        "gemma4_size": "20 GB",
        "qwen3_size": "20 GB",
        "note": "Same models as 32 GB tier, with headroom for long context and concurrent apps.",
    },
    "64gb": {
        "ram_gb": 64,
        "gemma4": None,
        "qwen3": None,
        "gemma4_size": None,
        "qwen3_size": None,
        "note": "No new recommended pair; qwen3:235b-a22b needs ~142 GB disk and is impractical.",
    },
}

# Synthetic session transcripts covering different problem shapes.
SAMPLES: list[dict[str, str]] = [
    {
        "name": "docker-cache",
        "text": (
            "User: docker build가 매번 npm install부터 다시 돌아. 캐시가 안 먹는 것 같아.\n"
            "Assistant: layer cache invalidation 한번 봐야겠네. Dockerfile COPY 순서가 문제일 수 있어. "
            "package*.json을 먼저 COPY하고 npm install 한 뒤에 소스를 COPY해야 dependency 변경이 없을 때 "
            "캐시가 살아. 지금은 전체 소스를 먼저 COPY해서 하나라도 바뀌면 npm install layer까지 날아가는 거야.\n"
            "User: 고쳤더니 2분 걸리던 빌드가 20초로 줄었어.\n"
            "Assistant: 멀티스테이지로 node_modules만 복사하면 이미지 크기도 줄일 수 있어."
        ),
    },
    {
        "name": "rust-mutex",
        "text": (
            "User: Rust tokio 프로그램이 가끔 deadlock 걸려.\n"
            "Assistant: async mutex 사용처를 확인필요. std::sync::Mutex 안에서 .await를 호출하면 "
            "스레드가 뮤텍스를 들고 await 지점에서 yield 될 수 있어서 다른 태스크가 같은 뮤텍스를 얻지 못해. "
            "tokio::sync::Mutex로 바꾸거나 .await를 락 스코프 밖으로 빼내야 해.\n"
            "User: tokio::sync::Mutex로 바꾸니 해결됐어. 왜 std mutex가 안 되는 거야?\n"
            "Assistant: std mutex는 스레드 단위 블로킹이라 async runtime과 찰떡궁합이 아니야. "
            "async context에서는 tokio::sync::Mutex가 권장돼."
        ),
    },
    {
        "name": "jwt-clock-skew",
        "text": (
            "User: JWT 검증이 간헐적으로 실패해. 서버 시간이 맞는데도.\n"
            "Assistant: nbf나 exp claim에 clock skew 허용이 있는지 확인필요. 대부분 라이브러리가 기본 0초라 "
            "NTP가 살짝 어긋나면 토큰이 아직 유효하지 않다고 거부해. 30~60초 정도 leeway를 주는 게 일반적이야.\n"
            "User: leeway 60초 주니까 안정적이네.\n"
            "Assistant: 너무 크게 주면 replay window가 늘어나니 5분 이내로 유지해."
        ),
    },
]


def _local_models() -> set[str]:
    """Return the set of models currently available in the local Ollama library."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode != 0:
            return set()
        models: set[str] = set()
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if parts:
                models.add(parts[0])
        return models
    except Exception:
        return set()


def _pull_model(model: str) -> bool:
    """Pull a model with Ollama. Returns True on success."""
    print(f"  pulling {model} with ollama ...")
    result = subprocess.run(
        ["ollama", "pull", model], capture_output=True, text=True, timeout=3600, check=False
    )
    if result.returncode != 0:
        print(f"  pull failed: {result.stderr.strip()}")
        return False
    return True


def _call_model(base_url: str, api_key: str, model: str, prompt: str) -> tuple[str, float]:
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You emit only compact, valid JSON. No prose outside JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "stream": False,
        "response_format": {"type": "json_object"},
        "reasoning_effort": "none",
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode("utf-8"))
    latency = time.time() - start
    content = data["choices"][0]["message"]["content"]
    return content, latency


def _evaluate(parsed: Optional[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "valid_json": parsed is not None,
        "has_title": False,
        "has_body": False,
        "title_has_korean": False,
        "body_has_sections": False,
        "body_sections": [],
        "trailing_metadata": False,
        "skip": False,
    }
    if not parsed:
        return metrics
    if parsed.get("skip"):
        metrics["skip"] = True
        return metrics
    title = str(parsed.get("title", "")).strip()
    body = str(parsed.get("body", "")).strip()
    metrics["has_title"] = bool(title)
    metrics["has_body"] = bool(body)
    metrics["title_has_korean"] = bool(re.search(r"[가-힣]", title))
    sections = []
    for pat in (r"##\s*배경", r"##\s*시도", r"##\s*결과", r"##\s*남은 일"):
        if re.search(pat, body):
            sections.append(pat.replace(r"##\s*", ""))
    metrics["body_sections"] = sections
    metrics["body_has_sections"] = len(sections) >= 2
    # trailing metadata leak detection
    tail = "\n".join(body.splitlines()[-6:])
    metrics["trailing_metadata"] = bool(
        re.search(r"^(tags|tools|concepts|claims):\s*", tail, re.MULTILINE)
    )
    return metrics


def _run_one(model: str, base_url: str, api_key: str, sample: dict[str, str]) -> dict[str, Any]:
    note_lang = boring_config.note_lang()
    origin, _rule = boring_config.classify(cwd="", remote_url=None)
    prompt = _build_prompt(sample["text"], origin, "bench")
    raw, latency = _call_model(base_url, api_key, model, prompt)
    parsed = _extract_json(raw)
    metrics = _evaluate(parsed)
    metrics["latency_sec"] = round(latency, 2)
    metrics["raw_chars"] = len(raw)
    return metrics


def _print_tiers() -> None:
    print("MacBook RAM tier pairing matrix")
    print()
    print(f"{'tier':<8} {'RAM':<6} {'gemma4':<20} {'qwen3':<20} {'note'}")
    for key, tier in TIER_PAIRS.items():
        g = tier["gemma4"] or "(empty)"
        q = tier["qwen3"] or "(empty)"
        print(f"{key:<8} {tier['ram_gb']} GB   {g:<20} {q:<20} {tier['note']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Benchmark LLM distillation quality")
    ap.add_argument("--model", action="append", help="model name (repeatable)")
    ap.add_argument("--tier", help="RAM tier: 8gb, 16gb, 24gb, 32gb, 48gb, 64gb")
    ap.add_argument("--list-tiers", action="store_true", help="print the RAM tier matrix and exit")
    ap.add_argument("--pull", action="store_true", help="ollama pull missing models before benchmarking")
    ap.add_argument("--base-url", default=omb_env.llm_base_url(), help="OpenAI-compatible base URL")
    ap.add_argument("--api-key", default=omb_env.llm_api_key(), help="API key (optional)")
    args = ap.parse_args()

    if args.list_tiers:
        _print_tiers()
        return

    if args.tier and args.model:
        ap.error("--tier and --model are mutually exclusive")

    models: list[str]
    if args.tier:
        tier = TIER_PAIRS.get(args.tier.lower())
        if tier is None:
            ap.error(f"unknown tier {args.tier!r}; use --list-tiers")
        models = [m for m in (tier["gemma4"], tier["qwen3"]) if m]
        if not models:
            print(f"tier {args.tier!r} has no recommended models; nothing to benchmark.")
            return
    else:
        models = args.model or [omb_env.llm_model()]

    local_models = _local_models()
    missing = [m for m in models if m not in local_models]
    if missing:
        if args.pull:
            for m in missing:
                if not _pull_model(m):
                    sys.exit(f"failed to pull {m}")
        else:
            print(f"models not in local Ollama library: {', '.join(missing)}")
            print("run with --pull to fetch them, or pass --base-url for a remote/LM Studio endpoint")
            sys.exit(1)

    note_lang = boring_config.note_lang()
    print(f"note_lang={note_lang}  samples={len(SAMPLES)}  base_url={args.base_url}")
    print()

    results: dict[str, list[dict[str, Any]]] = {}
    for model in models:
        print(f"=== {model} ===")
        run_results: list[dict[str, Any]] = []
        for sample in SAMPLES:
            metrics = _run_one(model, args.base_url, args.api_key, sample)
            run_results.append(metrics)
            status = "ok" if metrics["valid_json"] and not metrics["trailing_metadata"] else "fail"
            print(
                f"  {sample['name']:12} {status:4} "
                f"latency={metrics['latency_sec']:6.2f}s "
                f"sections={metrics['body_sections']} "
                f"title_ko={metrics['title_has_korean']} "
                f"trailing_meta={metrics['trailing_metadata']}"
            )
        results[model] = run_results
        print()

    # summary table
    print("=== summary ===")
    print(f"{'model':<20} {'valid':>6} {'title_ko':>9} {'2+sections':>11} {'clean_body':>11} {'avg_lat':>10}")
    for model, runs in results.items():
        valid = sum(1 for r in runs if r["valid_json"]) / len(runs)
        title_ko = sum(1 for r in runs if r["title_has_korean"]) / len(runs)
        sections = sum(1 for r in runs if r["body_has_sections"]) / len(runs)
        clean = sum(1 for r in runs if not r["trailing_metadata"]) / len(runs)
        avg_lat = sum(r["latency_sec"] for r in runs) / len(runs)
        print(
            f"{model:<20} {valid:>6.0%} {title_ko:>9.0%} {sections:>11.0%} {clean:>11.0%} {avg_lat:>9.2f}s"
        )


if __name__ == "__main__":
    main()
