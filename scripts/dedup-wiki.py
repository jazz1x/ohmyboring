#!/usr/bin/env python3
"""One-time duplicate-note cleanup for vault/wiki.

Clusters notes by embedding cosine similarity and reports which older duplicates
would be archived. Report only: --apply is refused while the migration runs
(deletion is closed, loop contract section 0 item 14).
"""

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "agents", "shared"))
import omb_env  # noqa: E402
from vault_note import split_frontmatter  # noqa: E402

DEFAULT_THRESHOLD = 0.93


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LlmEmbedder:
    """Minimal OpenAI-compatible embeddings client."""

    def __init__(self) -> None:
        self.base_url = omb_env.llm_base_url().rstrip("/")
        self.api_key = omb_env.llm_api_key()
        self.model = omb_env.embed_model()

    def embed(self, text: str) -> list[float]:
        payload = {"input": text, "model": self.model}
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["data"][0]["embedding"]


# Pull in urllib only where we need it to keep the top clean.
import urllib.request  # noqa: E402

import yaml  # noqa: E402


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def parse_note(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    if split is None:
        return {"path": path, "title": "", "body": text, "mtime": path.stat().st_mtime}
    yaml_text, body = split
    try:
        fm = json.loads(yaml_text) if yaml_text.strip().startswith("{") else yaml.safe_load(yaml_text)
    except Exception:
        fm = {}
    title = (fm.get("title") or "").strip()
    return {"path": path, "title": title, "body": body.strip(), "mtime": path.stat().st_mtime}


def cluster_notes(notes: list[dict[str, Any]], threshold: float) -> list[list[int]]:
    n = len(notes)
    emb = [n["embedding"] for n in notes]
    uf = UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            sim = cosine_similarity(emb[i], emb[j])
            if sim >= threshold:
                uf.union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Deduplicate vault/wiki notes")
    parser.add_argument("--wiki-dir", default="vault/wiki", help="wiki directory")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="cosine similarity threshold"
    )
    parser.add_argument("--apply", action="store_true", help="refused during the migration")
    args = parser.parse_args()

    if args.apply:
        print(
            "--apply is closed during the migration: archiving a note makes sync prune its record, "
            "and no record is deleted until the migration is over. Nothing was moved.",
            file=sys.stderr,
        )
        return 2

    wiki_dir = Path(args.wiki_dir)
    if not wiki_dir.is_dir():
        print(f"wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    paths = sorted(wiki_dir.glob("*.md"))
    if not paths:
        print("no wiki notes found")
        return 0

    print(f"[{_now()}] Loading {len(paths)} notes from {wiki_dir} ...")
    notes = [parse_note(p) for p in paths]

    embedder = LlmEmbedder()

    print(f"[{_now()}] Embedding {len(notes)} notes (model={embedder.model}) ...")
    for i, n in enumerate(notes):
        text = f"{n['title']}\n\n{n['body']}"[:4000]
        try:
            n["embedding"] = embedder.embed(text)
        except Exception as e:
            print(f"[{_now()}] ⚠️ embedding failed for {n['path'].name}: {e}", file=sys.stderr)
            n["embedding"] = [0.0] * 1024
        if (i + 1) % 10 == 0 or i + 1 == len(notes):
            print(f"  embedded {i + 1}/{len(notes)}")

    print(f"[{_now()}] Clustering with threshold {args.threshold} ...")
    clusters = cluster_notes(notes, args.threshold)
    total_dup = sum(len(g) - 1 for g in clusters)
    print(f"[{_now()}] Found {len(clusters)} duplicate clusters ({total_dup} duplicates to archive).\n")

    if not clusters:
        print("No duplicates found.")
        return 0

    actions = []
    for cid, group in enumerate(clusters, 1):
        group.sort(key=lambda i: notes[i]["mtime"], reverse=True)
        keeper = group[0]
        dupes = group[1:]
        print(
            f"Cluster {cid}: keep {notes[keeper]['path'].name} (mtime={datetime.fromtimestamp(notes[keeper]['mtime'], tz=UTC).isoformat()})"
        )
        for d in dupes:
            print(
                f"  → archive {notes[d]['path'].name} (mtime={datetime.fromtimestamp(notes[d]['mtime'], tz=UTC).isoformat()})"
            )
            actions.append((notes[d]["path"], notes[keeper]["path"]))

    print(f"\n[{_now()}] Dry run complete. {len(actions)} duplicates reported; nothing moved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
