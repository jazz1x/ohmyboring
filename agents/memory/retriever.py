"""LangChain retriever over the engine's /search — our ranking leaves the engine verbatim.

A query becomes one POST /search and comes back as list[Document], in the engine's own
order (RRF + feedback, applied in drudge/src/retrieve.rs apply_feedback). Nothing is
re-ranked, re-embedded, or re-ranked here. Give the retriever a session_id and /search
records every hit shown as handed to that session; record_verdict then attaches one
verdict to the whole handover and record_notes judges note by note — every edge names
its judge, and the next same query sees the counts in used_count / contested_count —
and, where two scores sit within one rank gap, in the order itself.

Engine failures (4xx/5xx, unreachable) raise. A dead gauge reads zero; it does not
silently return an empty list.

Run against the parity copy only: scripts/parity-harness.sh up, then
BoringRetriever(base_url="http://127.0.0.1:7701").
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
from drudge_client import owner_headers  # noqa: E402

_TIMEOUT = 30.0


class BoringRetriever(BaseRetriever):
    """Our search, wearing LangChain's jacket — an agent plugs this in and gets our memory.

    base_url has no default: the caller names the door or the engine (the door idiom reads
    BORING_DOOR_URL; here the caller passes the URL in). session_id is opt-in — set it
    and /search leaves a handed edge per hit for that session, so a later record_verdict
    has something to attach to. claims > 0 asks each hit to hand over that many of the
    claims its note declares; they ride along in the Document's metadata.
    """

    base_url: str
    max_results: int = 5
    project: str | None = None
    session_id: str | None = None
    claims: int = 0

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        body: dict[str, Any] = {"query": query, "max_results": self.max_results}
        if self.project is not None:
            body["project"] = self.project
        if self.session_id is not None:
            body["session_id"] = self.session_id
        if self.claims > 0:
            body["claims"] = self.claims
        payload = _post_json(f"{self.base_url}/search", body)
        return [_hit_to_document(hit) for hit in payload["hits"]]


def record_verdict(base_url: str, session_id: str, verdict: Literal["used", "contested"], judge: str) -> dict:
    """One verdict over everything handed to the session — POST /consumption, no path lists.

    The engine rejects a verdict sent alongside used/contested lists (400), so the body
    here is exactly session_id + observed_at (UTC, RFC 3339, call time) + verdict +
    judge. `judge` is the engine's author vocabulary — owner | inferred | unknown |
    agent:<name>; anything else is a 400. 'owner' travels with BORING_OWNER_TOKEN in the
    owner-token header, and without that token the engine refuses it (400). The engine's
    JSON response is returned as-is.
    """
    body = {
        "session_id": session_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "verdict": verdict,
        "judge": judge,
    }
    return _post_json(f"{base_url}/consumption", body)


def record_notes(
    base_url: str,
    session_id: str,
    judge: str,
    used: list[str] | None = None,
    contested: list[str] | None = None,
) -> dict:
    """Note-by-note judgement for an autonomous agent — POST /consumption with path lists.

    The agent decides per note which of what it was handed actually landed (`used`) and
    which misled it (`contested`); both lists name their paths and every written edge
    carries `judge`, in the same vocabulary and with the same owner token rule as
    record_verdict. At least one list must be non-empty — a judgement that
    judged nothing is a caller bug, so both empty raises ValueError before any request.
    """
    used = used or []
    contested = contested or []
    if not used and not contested:
        raise ValueError("record_notes: used and contested are both empty — nothing to judge")
    body = {
        "session_id": session_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "judge": judge,
        "used": used,
        "contested": contested,
    }
    return _post_json(f"{base_url}/consumption", body)


def _post_json(url: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **owner_headers(body)},
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
        "said_by_owner": hit["said_by_owner"],
        "superseded_by": hit.get("superseded_by", []),
    }
    for key in ("dist", "dist_kind", "claims", "claims_total"):
        if key in hit:
            metadata[key] = hit[key]
    return Document(id=hit["id"], page_content=hit["snippet"], metadata=metadata)
