#!/usr/bin/env python3
"""The morning card's register reads and candidate queue — no Slack, no LLM, no engine.

collect_registers shapes one project's four engine register answers through an injected
fetch callable; candidate_order and merge_project_candidates turn those registers into the
advise loop's (project, subject, register) queue, 아직 subjects first."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from card_types import ANSWER_REGISTERS, BOTTLENECK_REGISTERS, RegisterName, Registers


def _note_path(node: Any) -> str | None:
    path = (node or {}).get("source_path")
    return str(path) if path else None


def _recurrence_paths(rows: list[dict]) -> list[str]:
    out: set[str] = set()
    for row in rows:
        newer = _note_path(row.get("newer"))
        if newer:
            out.add(newer)
        for older in row.get("older") or []:
            path = _note_path(older)
            if path:
                out.add(path)
    return sorted(out)


def _recurrence_text(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        newer = row.get("newer") or {}
        head = " — ".join(str(newer[k]) for k in ("subject", "predicate", "value") if newer.get(k))
        if head:
            lines.append(f"* {head}")
    return "\n".join(lines)


def collect_registers(fetch: Callable[[str, str], dict[str, Any]], project: str = "") -> Registers:
    """Shape one project's four register answers through the injected `fetch` (path, project
    in — engine JSON out). `project=""` is not "no filter": the engine's own register filter
    treats an explicit empty string as "unassigned documents only" (measured 2026-09-22), so
    every call — active project or the unassigned bucket alike — always names one. A
    malformed payload is a ValueError, not a skip — proposals are grounded on these, and half
    a register set means grounding on nothing."""

    texts: dict[str, str] = {}
    sources: dict[str, list[str]] = {}
    for name in ANSWER_REGISTERS:
        data = fetch(f"/{name}", project)
        answer, srcs = data.get("answer"), data.get("sources")
        if not isinstance(answer, str) or not isinstance(srcs, list):
            raise ValueError(f"register {name}: expected {{answer, sources}}, got keys {sorted(data)!r}")
        texts[name] = answer
        sources[name] = [str(s) for s in srcs]
    data = fetch("/recurrences", project)
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"register recurrences: expected rows list, got keys {sorted(data)!r}")
    texts["recurrences"] = _recurrence_text(rows)
    sources["recurrences"] = _recurrence_paths(rows)
    return Registers(texts=texts, sources=sources)


def candidate_order(registers: Registers, priority: Iterable[str] = ()) -> list[tuple[str, RegisterName]]:
    """The advise loop's queue: 「아직」 subjects first (a past 「해」 still claimed today —
    wiki-1765's step 3 asks for these first), then the bottleneck registers in the owner's
    order (재발 → 막힘/위험 → 정체). next_actions never appears — it is already actionable,
    nothing to advise about. Each subject appears once, in the register it actually came from."""
    seen: set[str] = set()
    out: list[tuple[str, RegisterName]] = []
    for subject in priority:
        if subject in seen:
            continue
        seen.add(subject)
        register = next(
            (name for name in BOTTLENECK_REGISTERS if subject in registers.sources.get(name, [])),
            BOTTLENECK_REGISTERS[0],
        )
        out.append((subject, register))
    for name in BOTTLENECK_REGISTERS:
        for subject in registers.sources.get(name, []):
            if subject in seen:
                continue
            seen.add(subject)
            out.append((subject, name))
    return out


def merge_project_candidates(
    project_order: Iterable[str],
    project_registers: dict[str, Registers],
    priority: Iterable[tuple[str, str]] = (),
) -> list[tuple[str, str, RegisterName]]:
    """One (project, subject, register) queue across every active project plus the
    unassigned bucket — reusing candidate_order per project so the advise loop's own
    per-project ordering (재발 → 위험 → 정체) is unchanged. `priority` is (project, subject)
    pairs from cross_check's 아직 set and always leads, whichever project they came from.
    A subject is deduplicated globally, not per project: the same string appearing in two
    projects' registers would otherwise spend two of the loop's eight calls on one idea."""
    seen: set[str] = set()
    out: list[tuple[str, str, RegisterName]] = []
    for project, subject in priority:
        if subject in seen:
            continue
        registers = project_registers.get(project)
        if registers is None:
            continue
        matched = candidate_order(registers, [subject])
        if not matched:
            continue
        seen.add(subject)
        out.append((project, subject, matched[0][1]))
    for project in project_order:
        registers = project_registers.get(project)
        if registers is None:
            continue
        for subject, register in candidate_order(registers):
            if subject in seen:
                continue
            seen.add(subject)
            out.append((project, subject, register))
    return out
