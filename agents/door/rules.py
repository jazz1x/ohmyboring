#!/usr/bin/env python3
"""Rules — the owner's standing corrections, grouped for the trigger hook.

A rule is a pair of current claims with kind='rule' on one subject: the claim
with predicate 'rule' carries the sentence, the claim with predicate 'trigger'
carries the trigger words. A subject holding only one of them is incomplete —
it is counted, not served half.

Everything here is pure: rows in, dict out. The door route owns the SQL and
the wire format.
"""

from __future__ import annotations

SQL = (
    "select subject, predicate, value, source_path from claim "
    "where kind = %s and superseded_at is null "
    "order by subject, valid_from desc"
)


def group_rules(rows: list[tuple[str, str, str, str]]) -> dict:
    """rows: (subject, predicate, value, source_path), newest first per predicate.

    {"rules": [{"subject", "rule", "trigger", "source_path"}] sorted by subject,
    "incomplete": n} — a subject missing its 'rule' or 'trigger' claim is skipped
    and counted, never silently dropped and never served half."""
    by_subject: dict[str, dict[str, tuple[str, str]]] = {}
    for subject, predicate, value, source_path in rows:
        entry = by_subject.setdefault(subject, {})
        if predicate in ("rule", "trigger") and predicate not in entry:
            entry[predicate] = (value, source_path)
    rules = []
    incomplete = 0
    for subject in sorted(by_subject):
        entry = by_subject[subject]
        if "rule" not in entry or "trigger" not in entry:
            incomplete += 1
            continue
        (rule, source_path), (trigger, _) = entry["rule"], entry["trigger"]
        rules.append({"subject": subject, "rule": rule, "trigger": trigger, "source_path": source_path})
    return {"rules": rules, "incomplete": incomplete}
