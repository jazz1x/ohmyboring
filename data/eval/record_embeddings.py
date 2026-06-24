#!/usr/bin/env python3
"""Record real bge-m3 embeddings for the eval fixtures + golden queries → recorded_embeddings.json.

WHY: the eval gate (run_eval.py) needs real *semantic* embeddings — the golden queries are
paraphrases, so a hash/stub vector can't rank the right fixture. To run the gate in CI without a GPU,
we capture the real embedder's output once (here, against a live bge-m3) and replay it offline via
stub_embedder.py. The captured vectors are the genuine bge-m3 outputs, so CI recall == real recall.

Keys are sha256 of the *stripped* text — exactly the normalization stub_embedder.py applies — so the
chunk text drudge sends at ingest (`chunk(body.trim())`, single chunk since fixtures < CHUNK_SIZE) and
each query string both resolve to their recorded vector.

Run locally against your embedder, then commit recorded_embeddings.json:
    OMB_LLM_BASE_URL=http://localhost:11434/v1 python3 data/eval/record_embeddings.py
"""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden.json"
OUT = HERE / "recorded_embeddings.json"
BASE = (os.environ.get("OMB_LLM_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
MODEL = os.environ.get("OMB_EMBED_MODEL") or "bge-m3"


def key(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def body_of(raw: str) -> str:
    """Replicate frontmatter::parse + chunk(body.trim()): the exact text drudge embeds for a
    single-chunk note. Strip BOM, drop the `---\\n...\\n---\\n` frontmatter, trim."""
    raw = raw.removeprefix("﻿")
    if raw.startswith("---\n"):
        rest = raw[4:]
        end = rest.find("\n---\n")
        if end != -1:
            return rest[end + 5 :].strip()
    return raw.strip()


def embed(text: str) -> list:
    body = json.dumps({"model": MODEL, "input": text}).encode()
    req = urllib.request.Request(
        f"{BASE}/embeddings", data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["data"][0]["embedding"]


def main():
    texts = []
    for f in sorted(FIXTURES.glob("eval-*.md")):
        texts.append(body_of(f.read_text(encoding="utf-8")))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for q in golden.get("queries", []):
        texts.append(q["query"])

    recorded = {}
    dim = None
    for t in texts:
        vec = embed(t)
        dim = dim or len(vec)
        recorded[key(t)] = vec
        print(f"recorded ({len(vec)}d) {t[:60]!r}…", file=sys.stderr)

    OUT.write_text(
        json.dumps({"model": MODEL, "dim": dim, "vectors": recorded}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"\nwrote {OUT} — {len(recorded)} vectors, dim={dim}")


if __name__ == "__main__":
    main()
