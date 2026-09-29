#!/usr/bin/env python3
"""Hand-off queue between the host SessionEnd hooks and the hermes ingest-worker.

Hooks only enqueue; the worker drains through `distill_run`. Standard library only, so a hook
can import it without the engine dependencies. The directory follows `markers.MARK_DIR` at call
time, which is what the hermes container rewrites to its `/host` mirror.
"""

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import markers  # noqa: E402


@dataclass(frozen=True)
class QueueItem:
    session_id: str
    agent: str
    origin: str
    repo: str
    text: str
    enqueued_at: float = field(default_factory=time.time)


def queue_dir() -> str:
    return os.path.join(markers.MARK_DIR, "queue")


def item_path(session_id: str) -> str:
    return os.path.join(queue_dir(), f"{markers.safe_id(session_id)}.json")


def is_queued(session_id: str) -> bool:
    return os.path.exists(item_path(session_id))


def enqueue(item: QueueItem) -> None:
    """Write `item` atomically, replacing any earlier item of the same session, and mark it pending."""
    os.makedirs(queue_dir(), exist_ok=True)
    path = item_path(item.session_id)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(item), f, ensure_ascii=False)
    os.replace(tmp, path)
    markers.mark_pending(item.session_id)


def remove(session_id: str) -> None:
    try:
        os.unlink(item_path(session_id))
    except FileNotFoundError:
        pass


def _parse(path: str) -> QueueItem | None:
    try:
        with open(path, encoding="utf-8") as f:
            return QueueItem(**json.load(f))
    except (OSError, ValueError, TypeError):
        return None


def drain(limit: int | None = None) -> list[QueueItem]:
    """Oldest first. An unreadable file is set aside as `.bad` so it neither blocks nor vanishes."""
    if not os.path.isdir(queue_dir()):
        return []
    items = []
    for name in os.listdir(queue_dir()):
        if not name.endswith(".json"):
            continue
        path = os.path.join(queue_dir(), name)
        item = _parse(path)
        if item is None:
            os.replace(path, f"{path}.bad")
            print(f"[distill-queue] unreadable queue file set aside: {name}.bad", file=sys.stderr)
            continue
        items.append(item)
    items.sort(key=lambda i: i.enqueued_at)
    return items if limit is None else items[:limit]
