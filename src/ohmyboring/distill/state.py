from __future__ import annotations

from typing import Any, TypedDict

from ohmyboring.distill.polish import PolishResult


class DistillState(TypedDict, total=False):
    text: str
    origin: str
    repo: str
    session_id: str
    resolution: str
    prompt: str
    parsed: dict[str, Any] | None
    wants_language_retry: bool
    note: dict[str, Any] | None
    report: Any
    verified: bool
    verifier_status: str
    polish_outcome: PolishResult | None
    repaired: dict[str, Any] | None
    repaired_note: dict[str, Any] | None
    repaired_report: Any
    repaired_verified: bool
    ok: bool
