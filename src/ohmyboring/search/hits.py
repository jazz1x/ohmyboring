"""Document → /search hit dict — 문의 응답 JSON 모양.

Rust SearchHit 의 skip_serializing_if 규칙을 그대로 옮긴다: 빈 Vec·None 필드는 생략.
vector_cosine 경로라 dist·dist_kind 는 항상 있다; claims_total 은 claims 를 요청한 경우에만
(행이 없어도 0 으로 — 자른 것은 밝히지 않고 숨기지도 않는다).
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document


def document_to_hit(document: Document, *, claims_requested: bool) -> dict[str, Any]:
    """PgRetriever 의 Document 를 /search 응답 hit 하나로 — 키 순서는 serve.rs SearchHit 필드 순."""
    metadata = document.metadata
    hit: dict[str, Any] = {
        "id": document.id,
        "origin": metadata["origin"],
        "project": metadata["project"],
        "source_path": metadata["source_path"],
        "snippet": document.page_content,
        "dist": metadata["dist"],
        "dist_kind": metadata["dist_kind"],
    }
    superseded_by = metadata.get("superseded_by") or []
    if superseded_by:
        hit["superseded_by"] = superseded_by
    hit["used_count"] = metadata["used_count"]
    hit["contested_count"] = metadata["contested_count"]
    hit["said_by_owner"] = metadata["said_by_owner"]
    if claims_requested:
        claims = metadata.get("claims") or []
        if claims:
            hit["claims"] = claims
        hit["claims_total"] = metadata.get("claims_total", 0)
    return hit
