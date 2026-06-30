---
name: memory-ingest
description: "Distill ONE Claude Code session transcript into a memory note and store it via the ohmyboring/remember MCP tool. Used for autonomous backfill + on-session-end ingestion."
version: 1.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [memory, ingest, distill, remember, ohmyboring, rag, backfill]
    related_skills: []
---

# Memory Ingest — distill a session and STORE it

You turn ONE Claude Code session transcript into ONE durable memory note and **store it via the `remember` tool from the `ohmyboring` MCP server**. This is the write door of a personal RAG: you are the curator, `ohmyboring` (the `remember` tool) is the deterministic store.

## CRITICAL: You MUST call the `remember` tool

This skill is NOT done until you have actually **invoked the `remember` tool**.
Reasoning about the note, describing it, or printing JSON is NOT enough — the note only exists once `remember` returns success (a message containing "remembered → wiki/..." or a wiki id; a richer duplicate may update an existing wiki id).
**Call `remember` exactly once, then report the wiki id it returns.**

## Inputs

You are given (in the prompt):
- A transcript path under `/host/.claude/projects/.../<id>.jsonl`, OR the already-extracted session text inline.
- An `origin` (`personal` or `company`) and optionally a `repo` slug. Use them verbatim if provided.
- An `omb_session_id` line that must be copied into the note's frontmatter.

## Steps

1. **Get the text.** If inline text is given, use it. If only a path is given, read it — but transcripts can be huge: read with `offset`/`limit` (e.g. first ~400 lines + last ~400 lines) to stay under the read limit. Never try to read a multi-MB file in one shot.
2. **Judge (KEEP/SKIP).** Most real coding sessions ARE worth keeping. SKIP only if it is pure greeting, an empty/aborted session, or only tool-output with no human-meaningful work. To SKIP: do nothing, reply `SKIP`, and stop.
3. **Distill** a problem-solving narrative in the SESSION'S OWN LANGUAGE (Korean session → Korean note):
   - `title` — ≤60 chars, specific. **Required.**
   - `body` — concise markdown: problem → what was tried → what worked → what's next. Keep durable facts/decisions.
   - `tags` — array of ≤6 lowercase topical tags.
   - `tools` — array of ≤6 tool/library names.
   - `concepts` — array of ≤6 patterns/concepts.
   - `claims` — optional array of `{subject, predicate, value, kind, confidence}` facts/decisions/risks/next-steps.
     - `kind`: `fact`, `decision`, `assumption`, `risk`, `blocked`, `goal`, or `next`.
     - Use `next` for concrete follow-up actions still pending at the end of the session.
     - Use `blocked` only for active obstacles that prevent progress.
     - Example: `{"subject":"omb","predicate":"next-step","value":"add /next_actions endpoint","kind":"next","confidence":"certain"}`
4. **STORE.** Call the `remember` tool ONCE with `{title, body, tags, tools, concepts, origin, repo, omb_session_id, claims}`.
   - `tags`, `tools`, `concepts` must be JSON arrays of strings, not a single comma-separated string.
   - `repo` is optional; include it only when a repo slug is provided.
   - `omb_session_id` is required when the ingestion worker provides it.
5. **Report** the wiki id from the tool result (e.g. "stored → wiki-0042"). If `remember` errors, say so plainly — do not pretend success.

## Rules

- ONE session, ONE `remember` call per run. Do not loop over many sessions.
- Do NOT add a "Related" section or `[[wikilinks]]` in the body — cross-links are managed by the engine.
- Output only: the wiki id on success, or `SKIP`, or the plain error. No essays.
- If the tool call fails due to a missing or malformed argument, fix the argument and retry once.
