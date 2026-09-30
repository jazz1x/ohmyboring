"""The polish prompt: rewrite a note body so a person can read it, losing nothing."""

from __future__ import annotations

_LANGUAGE = {
    "ko": "Write in Korean, as the note already is.",
    "en": "Write in English, as the note already is.",
    "ja": "Write in Japanese, as the note already is.",
}


def build_polish_prompt(body: str, note_lang: str) -> str:
    language = _LANGUAGE.get(note_lang, "Keep the note's own language.")
    return f"""You tidy one note from a personal knowledge base so a person can read it at a glance.

Rewrite ONLY the body below. {language}

Shape:
- Start with a 2–3 line summary: what happened and what was decided.
- Then short sections, each under a `## ` heading.
- Bullets or short sentences; keep every line under 120 characters.
- Keep code blocks as they are.

Must not:
- Drop or change any fact. Every number, date, id (wiki-NNNN, PR #NNN, commit hashes), `code` span,
  file path and URL in the body must appear in your rewrite exactly as written.
- Add anything that is not in the body.
- Touch frontmatter — you only get the body.

Return JSON: {{"body": "<the rewritten body>"}}

=== BODY ===
{body}
"""
