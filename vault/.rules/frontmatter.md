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

---

## ID rules

- Pattern: `^wiki-\d{4,5}$` (4–5 digits). Filename stem == frontmatter `id`.
- Monotonically increasing. Once assigned, an ID is never reused.
- On deletion: instead of deleting the file, tombstone it — empty the body and leave `superseded_by`.

## Wikilink conventions

- Body page references: `[[wiki-NNNN]]` (Obsidian standard).
- Cross-layer links (`[[raw/...]]`, `[[meta/...]]`) are forbidden — reference via the `sources:` field.
- A dangling `[[wiki-NNNN]]` (missing target) is an error in `vault lint`.
