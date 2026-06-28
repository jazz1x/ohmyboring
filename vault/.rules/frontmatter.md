# Frontmatter + Wikilink conventions — human-facing SSOT

> The machine-parsing SSOT for bots/tools is [`.rules/schema.yaml`](schema.yaml). This file must stay consistent with it.
>
> These conventions apply to the **compiled `vault/wiki/wiki-NNNN.md`** pages. The distill notes in `vault/raw/`
> are free-form markdown without frontmatter; `drudge vault compile` curates raw→wiki and generates the frontmatter.

---

## Required fields (required_frontmatter)

| Field | Type | Description |
|------|------|------|
| `id` | string | Page ID. Matches the filename stem. Pattern `wiki-NNNN[N]` |
| `title` | string | Page title. One line, clear |
| `kind` | enum | `note` \| `memory` \| `session` \| `decision` |
| `origin` | enum | `personal` \| `company` |
| `date` | string | Creation date `YYYY-MM-DD` |

## Optional fields (optional)

| Field | Type | Description |
|------|------|------|
| `sources` | list[string] | Source file paths (prefix: `raw/`, `meta/`, `.rules/`) |
| `relates_to` | list[string] | List of related page IDs (`wiki-NNNN`) |
| `tags` | list[string] | Classification tags (Obsidian-safe: spaces/special chars → `-`. Includes `repo/<slug>` nested tags) |
| `superseded_by` | string | ID of the page that superseded this one (`wiki-NNNN`) |
| `summary` | string | One-line summary (recommended under 200 chars) |

## Semantic fields (for recall & graph)

| Field | Type | Description |
|------|------|------|
| `tools` | list[string] | Concrete tools/commands used in this note |
| `concepts` | list[string] | Recurring ideas/axes |
| `claims` | list[{subject, predicate, value}] | Durable facts/decisions. Curated by the distillation agent; drudge stores them as temporal authority. |

### Claims

Claims are the most important field for later recall. Each claim is a `(subject, predicate, value)` triple.

- `subject`: project or component name (e.g., `kb-rag-bot`)
- `predicate`: property/decision axis (e.g., `model-interface`, `status`, `release-version`)
- `value`: concrete fact (e.g., `bedrock-converse`, `removed`, `0.1.3`)

Aim for 3–5 claims per session-distilled note. Avoid vague values like "검토" or "확인" — they sound like next-steps, not facts.

---

## ID rules

- Pattern: `^wiki-\d{4,5}$` (4–5 digits). Filename stem == frontmatter `id`.
- Monotonically increasing. Once assigned, an ID is never reused.
- On deletion: instead of deleting the file, tombstone it — empty the body and leave `superseded_by`.

## Wikilink conventions

- Body page references: `[[wiki-NNNN]]` (Obsidian standard).
- Cross-layer links (`[[raw/...]]`, `[[meta/...]]`) are forbidden — reference via the `sources:` field.
- A dangling `[[wiki-NNNN]]` (missing target) is an error in `vault lint`.
