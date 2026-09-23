"""LangChain retriever over the engine's /search — our ranking leaves the engine verbatim.

A query becomes one POST /search and comes back as list[Document], in the engine's own
order (RRF + feedback, applied in drudge/src/retrieve.rs apply_feedback). Nothing is
re-ranked, re-embedded, or re-ranked here. Give the retriever a session_id and /search
records every hit shown as handed to that session; record_verdict then attaches one
verdict to the whole handover, and the next same query sees it in used_count /
contested_count — and, where two scores sit within one rank gap, in the order itself.

Engine failures (4xx/5xx, unreachable) raise. A dead gauge reads zero; it does not
silently return an empty list.

Run against the parity copy only: scripts/parity-harness.sh up, then
BoringRetriever(base_url="http://127.0.0.1:7701").
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

_TIMEOUT = 30.0


class BoringRetriever(BaseRetriever):
    """Our search, wearing LangChain's jacket — an agent plugs this in and gets our memory.

    base_url has no default: the caller names the engine (the door idiom reads
    BORING_DOOR_URL; here the caller passes the URL in). session_id is opt-in — set it
    and /search leaves a handed edge per hit for that session, so a later record_verdict
    has something to attach to.
    """

    base_url: str
    max_results: int = 5
    project: str | None = None
    session_id: str | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        body: dict[str, Any] = {"query": query, "max_results": self.max_results}
        if self.project is not None:
            body["project"] = self.project
        if self.session_id is not None:
            body["session_id"] = self.session_id
        payload = _post_json(f"{self.base_url}/search", body)
        return [_hit_to_document(hit) for hit in payload["hits"]]


def record_verdict(base_url: str, session_id: str, verdict: Literal["used", "contested"]) -> dict:
    """One verdict over everything handed to the session — POST /consumption, no path lists.

    The engine rejects a verdict sent alongside used/contested lists (400), so the body
    here is exactly session_id + observed_at (UTC, RFC 3339, call time) + verdict. The
    engine's JSON response is returned as-is.
    """
    body = {
        "session_id": session_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "verdict": verdict,
    }
    return _post_json(f"{base_url}/consumption", body)


def _post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hit_to_document(hit: dict[str, Any]) -> Document:
    metadata: dict[str, Any] = {
        "source_path": hit["source_path"],
        "project": hit["project"],
        "origin": hit["origin"],
        "used_count": hit["used_count"],
        "contested_count": hit["contested_count"],
        "superseded_by": hit.get("superseded_by", []),
    }
    for key in ("dist", "dist_kind"):
        if key in hit:
            metadata[key] = hit[key]
    return Document(id=hit["id"], page_content=hit["snippet"], metadata=metadata)
