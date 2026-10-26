#!/usr/bin/env python3
"""Hand-off queue between the host SessionEnd hooks and the hermes ingest-worker.

Hooks only enqueue; the worker drains through `distill_run`. Standard library only, so a hook
can import it without the engine dependencies. The directory follows `markers.MARK_DIR` at call
time, which is what the hermes container rewrites to its `/host` mirror.
"""

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "shared"))
import markers  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "src"))
from ohmyboring.adapters import events  # noqa: E402  (standard library only, like this module)


@dataclass(frozen=True)
class QueueItem:
    session_id: str
    agent: str
    origin: str
    repo: str
    text: str
    enqueued_at: float = field(default_factory=time.time)
    # text holds only the turns after this transcript time; None means from the session start.
    since: float | None = None


def queue_dir() -> str:
    return os.path.join(markers.MARK_DIR, "queue")


def item_path(session_id: str) -> str:
    return os.path.join(queue_dir(), f"{markers.safe_id(session_id)}.json")


def is_queued(session_id: str) -> bool:
    return os.path.exists(item_path(session_id))


def _dead_event(item: QueueItem, status: str, reason: str) -> None:
    events.try_append_event(
        "distill-queue",
        "distill_queue_dead",
        status,
        reason=reason,
        agent=item.agent,
        session_id=item.session_id,
    )


def enqueue(item: QueueItem) -> None:
    """Write `item` atomically, replacing any earlier item of the same session, and mark it pending."""
    if markers.is_dead(item.session_id):
        # A dead session is re-offered only with a tail that starts after its death (`since`, from
        # resume_since); mark_pending below clears the marker and the retry count starts again.
        # Anything else is the content that already failed, so it is not queued.
        if item.since is None:
            _dead_event(item, "skipped", "dead_no_new_turns")
            print(f"[distill-queue] dead session not queued: {item.session_id}", file=sys.stderr)
            return
        _dead_event(item, "requeued", "dead_requeued_tail")
        print(f"[distill-queue] dead session re-queued with its tail: {item.session_id}", file=sys.stderr)
    os.makedirs(queue_dir(), exist_ok=True)
    path = item_path(item.session_id)
    fd, tmp = tempfile.mkstemp(dir=queue_dir(), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(item), f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    markers.mark_pending(item.session_id)


def resume_since(session_id: str) -> float | None:
    """Transcript time after which this session is not yet in memory.

    A queued item (still waiting, or kept after a failed try) has not reached memory, so its
    own start carries over; otherwise the done marker says how far memory goes. Reading only the
    marker loses the tail after a failure, because enqueue clears the marker.
    A dead session resumes after its death: what failed before is not offered again.
    """
    dead = markers.dead_time(session_id)
    if dead is not None:
        return dead
    if is_queued(session_id):
        queued = _parse(item_path(session_id))
        if queued is not None:
            return queued.since
    return markers.done_time(session_id)


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
