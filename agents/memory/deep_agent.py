#!/usr/bin/env python3
"""A Deep Agent whose /memories/ files are engine notes (BoringStore) and whose `recall`
tool is BoringRetriever under the session name, so owner verdicts on its hits show next time."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain_core.tools import tool

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from retriever import BoringRetriever  # noqa: E402
from store import BoringStore  # noqa: E402


def boring_deep_agent(
    model: Any,
    *,
    engine_url: str,
    door_url: str,
    agent_name: str,
    session_id: str,
    tools: Sequence[Any] = (),
    max_results: int = 5,
):
    store = BoringStore(engine_url=engine_url, door_url=door_url)
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(store=store, namespace=lambda _rt: ("agent", agent_name, "memories")),
        },
    )
    retriever = BoringRetriever(base_url=engine_url, max_results=max_results, session_id=session_id)

    @tool
    def recall(query: str) -> str:
        """Search the owner's memory. One line per note, in the engine's order: id · used · contested · snippet."""
        return "\n".join(
            f"{doc.id} · used={doc.metadata['used_count']} contested={doc.metadata['contested_count']} · "
            f"{doc.page_content}"
            for doc in retriever.invoke(query)
        )

    return create_deep_agent(model=model, tools=[recall, *tools], backend=backend, store=store)
