#!/usr/bin/env python3
"""Benchmark the local embedding model used by ohmyboring.

Checks endpoint health, actual embedding dimension vs boring.json, single/batch latency,
and a sanity check that related sentences score higher than unrelated ones.

Run:
  make bench-embed
  python3 scripts/bench-embed.py --model bge-m3 --embed-dim 1024
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
import boring_config  # noqa: E402
import omb_env  # noqa: E402

SAMPLES: dict[str, str] = {
    "docker": "Dockerfile에서 package.json을 먼저 COPY하고 npm install해야 layer cache가 살아난다.",
    "mutex": "async context에서는 std::sync::Mutex 대신 tokio::sync::Mutex를 써야 deadlock을 피한다.",
    "jwt": "JWT의 nbf나 exp claim에 clock skew leeway를 30~60초 정도 주는 것이 일반적이다.",
    "unrelated": "오늘 점심 메뉴는 김치찌개였다.",
}


def _embed(
    texts: list[str], base_url: str, api_key: str, model: str
) -> tuple[list[list[float]], float]:
    headers: dict[str, str] = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": model,
        "input": texts,
        "encoding_format": "float",
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    latency = time.time() - start
    vectors = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
    return [v["embedding"] for v in vectors], latency


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def main() -> None:
    ap = argparse.ArgumentParser(description="Benchmark local embedding model")
    ap.add_argument("--base-url", default=omb_env.llm_base_url(), help="OpenAI-compatible base URL")
    ap.add_argument("--api-key", default=omb_env.llm_api_key(), help="API key (optional)")
    ap.add_argument("--model", default=omb_env.embed_model(), help="Embedding model name")
    ap.add_argument(
        "--embed-dim",
        type=int,
        default=boring_config.load().get("llm", {}).get("embed_dim", 1024),
        help="Expected embedding dimension",
    )
    args = ap.parse_args()

    print(f"endpoint={args.base_url}  model={args.model}  expected_dim={args.embed_dim}")
    print()

    texts = list(SAMPLES.values())
    try:
        vectors, latency = _embed(texts, args.base_url, args.api_key, args.model)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"embed request failed: HTTP {e.code} {body}")
        sys.exit(1)
    except Exception as e:
        print(f"embed request failed: {e}")
        sys.exit(1)

    actual_dim = len(vectors[0])
    print(f"single-batch latency ({len(texts)} texts): {latency:.3f}s")
    print(f"actual dim: {actual_dim}  expected dim: {args.embed_dim}")
    if actual_dim != args.embed_dim:
        print(f"DIMENSION MISMATCH: update llm.embed_dim to {actual_dim} and run 'make reset'")
        sys.exit(1)
    print("✓ dimension matches")
    print()

    # one-at-a-time latency
    one_latencies: list[float] = []
    for name, text in SAMPLES.items():
        _, lat = _embed([text], args.base_url, args.api_key, args.model)
        one_latencies.append(lat)
        print(f"  {name:12} single latency: {lat:.3f}s")
    avg_one = sum(one_latencies) / len(one_latencies)
    print(f"average single latency: {avg_one:.3f}s")
    print()

    # sanity: related pair should be closer than unrelated pair
    by_name = {name: vec for name, vec in zip(SAMPLES.keys(), vectors)}
    related = _cosine(by_name["docker"], by_name["mutex"])
    unrelated = _cosine(by_name["docker"], by_name["unrelated"])
    print("cosine sanity:")
    print(f"  docker ↔ mutex (both dev topics):   {related:.4f}")
    print(f"  docker ↔ unrelated (lunch):         {unrelated:.4f}")
    if related <= unrelated:
        print("  ⚠ related pair is not closer than unrelated pair — model may be misconfigured")
    else:
        print("  ✓ related pair is closer")


if __name__ == "__main__":
    main()
