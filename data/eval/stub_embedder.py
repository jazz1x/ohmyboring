#!/usr/bin/env python3
"""Offline OpenAI-compatible embedder for the CI eval gate — replays recorded bge-m3 vectors.

The eval gate needs real semantic embeddings but CI has no GPU. This serves `/v1/embeddings` from
data/eval/recorded_embeddings.json (real bge-m3 output captured by record_embeddings.py), keyed by
sha256 of the *stripped* input — the same normalization the recorder used. So the fixture chunk text
drudge embeds at ingest and each golden query both resolve to their genuine recorded vector, and CI
recall == real recall.

Any text NOT in the recording (e.g. graph node/concept labels drudge also embeds) gets a deterministic
pseudo-vector of the same dimension: harmless, because /search ranks document *chunks* only — the graph
nodes those vectors feed never enter the recall@k path. A miss must never error (that would abort the
whole ingest), so it always returns a valid same-dim vector.

Usage (CI): python3 data/eval/stub_embedder.py  # serves 0.0.0.0:11434, OpenAI /v1 surface
"""
import hashlib
import json
import os
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REC = json.loads((HERE / "recorded_embeddings.json").read_text(encoding="utf-8"))
VECTORS = REC["vectors"]
DIM = REC["dim"]
PORT = int(os.environ.get("STUB_PORT") or "11434")


def _key(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _pseudo_vector(text: str) -> list:
    """Deterministic unit-ish vector for un-recorded inputs (graph node labels). Same dim so the
    pgvector column accepts it; never used in chunk recall ranking."""
    out = []
    seed = text.encode("utf-8")
    i = 0
    while len(out) < DIM:
        h = hashlib.sha256(seed + struct.pack(">I", i)).digest()
        for j in range(0, len(h), 4):
            if len(out) >= DIM:
                break
            v = struct.unpack(">I", h[j : j + 4])[0]
            out.append((v / 0xFFFFFFFF) * 2.0 - 1.0)  # ∈ [-1, 1]
        i += 1
    return out


def _embed_one(text: str) -> list:
    return VECTORS.get(_key(text)) or _pseudo_vector(text)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # quiet

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # /v1/models — health/probe surface for the openai-compatible provider check.
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": REC.get("model", "bge-m3"), "object": "model"}]})
        else:
            self._json(200, {"status": "ok"})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        if not self.path.rstrip("/").endswith("/embeddings"):
            self._json(404, {"error": f"unhandled path {self.path}"})
            return
        inp = req.get("input")
        items = inp if isinstance(inp, list) else [inp if inp is not None else ""]
        data = [
            {"object": "embedding", "index": i, "embedding": _embed_one(str(t))}
            for i, t in enumerate(items)
        ]
        self._json(200, {"object": "list", "data": data, "model": req.get("model", "stub")})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"stub embedder on :{PORT} — {len(VECTORS)} recorded vectors, dim={DIM}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
