#!/usr/bin/env python3
"""BoringStore — LangGraph's BaseStore over our engine and door.

A LangGraph agent plugs this in and its memory becomes our memory: a put is one
engine /remember (the value is the note body, the key is hashed into the claim
subject, the old note for the same key is named in supersedes), so what the agent
writes is a note we search, judge, and seal like any other. Putting the same key
again replaces: get answers with the new value while the old note and its claims
stay on record — records are never deleted. Delete is refused, not emulated.

Coverage on purpose, each a visible raise rather than a silent fallback:
  - get    → door GET /claim-source (404 → None; the claim's value rides the 200)
  - put    → door GET /claim-source, then engine POST /remember. A same-value re-put
             stops after the GET — a quiet no-op, no write leaves. A changed value names
             the current note in supersedes, and since r4.1 a corrected note always lands:
             the engine skips the duplicate gate for anything that names what it
             supersedes, so the engine answering `duplicate` is a real failure and
             raises instead of passing for success. The first put stamps `created_at`
             (UTC ISO) into the claim value; a re-put inherits it from the current
             value, so get's created_at is when the record was born, not last written.
  - search → ("boring", ...) only, over the engine's /search via BoringRetriever
             (nothing re-implemented here); any other prefix is next wheel
  - delete / list_namespaces / ttl / index → refused or next wheel, never faked

Run against the parity copy only: scripts/parity-harness.sh up, then
BoringStore(engine_url="http://127.0.0.1:7701", door_url="http://127.0.0.1:7711").
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from retriever import BoringRetriever, _post_json  # noqa: E402

_TIMEOUT = 30.0
_PREDICATE = "langgraph-store-value"


def _subject_for(namespace: tuple[str, ...], key: str) -> str:
    """The claim subject an (namespace, key) lives under: a hash, never the raw key.

    Subjects and predicates are folded through the engine's canon() at ingest, so a
    raw key like 'user_prefs' would collide with 'User Prefs'. A lowercase hex digest
    is canon-inert — different keys always land in different claim slots.
    """
    raw = json.dumps([list(namespace), key], ensure_ascii=False)
    return "langgraph-store-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _claim_value(namespace: tuple[str, ...], key: str, value: dict[str, Any], created_at: datetime) -> str:
    """What the claim carries: the coordinates, the value, and when the record was born,
    one JSON string. `created_at` never changes after the first put — a re-put inherits
    it from the current value, so get answers "when was this record created", not "when
    was it last written"."""
    return json.dumps(
        {
            "namespace": list(namespace),
            "key": key,
            "value": value,
            "created_at": created_at.isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _created_at_of(payload: dict[str, Any]) -> datetime:
    """A claim value's created_at. Values written before r4.1 lack the field; the record's
    first candidate valid_from is the closest honest answer for those."""
    raw = json.loads(payload["value"]).get("created_at")
    if raw is not None:
        return datetime.fromisoformat(raw)
    return min(datetime.fromisoformat(c["valid_from"]) for c in payload["candidates"])


def _get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _claim_source(door_url: str, subject: str) -> dict[str, Any] | None:
    """The door's answer for a subject, None on its 404 — anything else raises."""
    query = urllib.parse.urlencode({"subject": subject})
    try:
        return _get_json(f"{door_url}/claim-source?{query}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


class BoringStore(BaseStore):
    """engine_url and door_url, no defaults — the caller names both ends explicitly."""

    def __init__(self, engine_url: str, door_url: str):
        self.engine_url = engine_url.rstrip("/")
        self.door_url = door_url.rstrip("/")

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        return [self._run(op) for op in ops]

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, ops)

    def _run(self, op: Op) -> Result:
        if isinstance(op, GetOp):
            return self._get(op)
        if isinstance(op, PutOp):
            return self._put(op)
        if isinstance(op, SearchOp):
            return self._search(op)
        if isinstance(op, ListNamespacesOp):
            raise NotImplementedError("list_namespaces is next wheel (agent namespaces)")
        raise TypeError(f"unknown op {type(op).__name__}")

    def _get(self, op: GetOp) -> Item | None:
        payload = _claim_source(self.door_url, _subject_for(op.namespace, op.key))
        if payload is None:
            return None
        # The door must carry the claim's value; a door from before that field
        # raises KeyError here rather than answering a stale shape.
        envelope = json.loads(payload["value"])
        return Item(
            value=envelope["value"],
            key=envelope["key"],
            namespace=tuple(envelope["namespace"]),
            created_at=_created_at_of(payload),
            updated_at=datetime.fromisoformat(payload["valid_from"]),
        )

    def _put(self, op: PutOp) -> None:
        if op.value is None:
            raise ValueError("records are not deleted — put a new value to supersede")
        if op.index is not None:
            raise ValueError("index fields are not supported — next wheel")
        if op.ttl is not None:
            raise ValueError("ttl is not supported — records do not expire")
        subject = _subject_for(op.namespace, op.key)
        current = _claim_source(self.door_url, subject)
        current_note = current["note"] if current is not None else None
        if current is not None and json.loads(current["value"])["value"] == op.value:
            # Same value already current — nothing to supersede. Quiet no-op, and no
            # write leaves: the engine's duplicate gate would skip it anyway.
            return None
        created_at = _created_at_of(current) if current is not None else datetime.now(UTC)
        body: dict[str, Any] = {
            "title": f"langgraph store {'/'.join(op.namespace)}/{op.key}",
            "body": "```json\n" + json.dumps(op.value, ensure_ascii=False, sort_keys=True) + "\n```",
            "claims": [
                {
                    "subject": subject,
                    "predicate": _PREDICATE,
                    "value": _claim_value(op.namespace, op.key, op.value, created_at),
                }
            ],
            "tags": ["langgraph-store"],
            "origin": "personal",
        }
        if current_note is not None:
            body["supersedes"] = [current_note]
        resp = _post_json(f"{self.engine_url}/remember", body)
        # Since r4.1 a corrected note always lands: the engine skips the duplicate gate
        # for anything that names what it supersedes. A duplicate answer here means the
        # gate fired anyway and the write was swallowed — that is a failure, not a quiet
        # None: the caller's new value is NOT what get will answer.
        if resp.get("duplicate"):
            raise RuntimeError(f"/remember swallowed the put as a duplicate of {resp['duplicate']}")
        return None

    def _search(self, op: SearchOp) -> list[SearchItem]:
        if not op.namespace_prefix or op.namespace_prefix[0] != "boring":
            raise NotImplementedError(
                f"search outside ('boring',) is next wheel (agent namespaces): {op.namespace_prefix!r}"
            )
        if op.query is None:
            raise ValueError("search needs a query — semantic search over our memory")
        if op.filter is not None:
            raise ValueError("search filters are not supported — next wheel")
        if op.offset:
            raise ValueError("search offset is not supported — next wheel")
        project = op.namespace_prefix[1] if len(op.namespace_prefix) > 1 else None
        retriever = BoringRetriever(base_url=self.engine_url, max_results=op.limit, project=project)
        docs = retriever.invoke(op.query)
        now = datetime.now(UTC)
        return [
            SearchItem(
                namespace=("boring", doc.metadata["project"]),
                key=doc.id,
                value={"content": doc.page_content, **doc.metadata},
                created_at=now,
                updated_at=now,
                score=None,
            )
            for doc in docs
        ]
