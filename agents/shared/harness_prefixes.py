"""Openings of user turns the harness wrote, not the owner — one list for every reader.

The recall/rules hooks skip these prompts; the distiller drops these rows from the owner's words.
A peer session's message reaches the hook as "<cross-session-message from=…>" (query_log
2026-10-29 01:05:41Z), while the transcript row starts with "Another Claude session sent a
message" (1,474 of 1,474 rows, 10-28~29) — both forms are listed.
"""

HARNESS_PREFIXES = (
    "<task-notification>",
    "Another Claude session sent a message",
    "<agent-message",
    "<cross-session-message",
)
