"""The polish prompt: rewrite a note body into readable markdown, losing nothing."""

from __future__ import annotations

_LANGUAGE = {
    "ko": "Write in Korean, as the note already is.",
    "en": "Write in English, as the note already is.",
    "ja": "Write in Japanese, as the note already is.",
}

# The owner approved this shape on 2026-09-30 (wiki-2296 rewritten by hand, polish-preview sample).
_EXAMPLE = """엔진을 부르는 코드(drudge_client)를 `agents/shared` 에서 새 패키지 `ohmyboring/adapters/engine.py` 로
옮겼다. 부르는 곳 18곳을 새 경로로 바꿨고, 동작은 옛 코드와 똑같음을 확인했다.

## 왜 옮겼나
- 구조 개편에서 「엔진과 이야기하는 코드는 adapters/ 에 둔다」로 정했다. 이 조각이 그 첫 번째다.

## 바뀐 것
- `agents/shared/drudge_client.py` → `ohmyboring/adapters/engine.py`
- 부르는 곳 14곳 + 부르는 방식이 바뀐 4곳

## 어떻게 확인했나
- 옛 코드와 새 코드의 엔진 요청 10가지가 바이트까지 같다
- 일부러 결함을 넣은 4가지를 시험이 모두 잡았다

## 남은 것
1. 새 engine.py 가 아직 옛 설정 모듈(omb_env)에 기댄다(:18)
2. 다음 조각 3b: 엔진이 실패하면 예외 대신 값으로 돌려준다
"""


def build_polish_prompt(body: str, note_lang: str, retry_reason: str | None = None) -> str:
    language = _LANGUAGE.get(note_lang, "Keep the note's own language.")
    retry = ""
    if retry_reason is not None:
        retry = f"""
Your previous rewrite was rejected by the checker: {retry_reason}
Return the full body once more with exactly that problem fixed. Change nothing else.
"""
    return f"""You tidy one note from a personal knowledge base so its owner can read it at a glance.

Rewrite ONLY the body below as markdown. {language}

Shape (see the example):
- First, 2–3 plain lines, no heading: what happened, why, and whether it worked.
- Then sections under `## ` headings that fit this note (e.g. why · what changed · how it was
  checked · delivery · what is left). Each section is a `- ` list or a numbered list; when the
  body sets several items side by side (before/after, per-case results, counts), use a markdown
  table instead.
- Keep every line under 120 characters. Put paths, commands, ids and code in `backticks`.
- Explain insider shorthand in plain words (e.g. "mutant killed" → "a deliberately planted defect
  was caught by a test"). Linter rule codes such as PLR0913 or B008 may be left out.
- Keep code blocks as they are.

Must not:
- Drop or change any fact. Every number, date, time, id (wiki-NNNN, PR #NNN, commit hashes), path,
  `code` span and URL must appear in your rewrite exactly as written.
- Add anything that is not in the body.

Example of the shape (a different note):
{_EXAMPLE}
{retry}Return JSON: {{"body": "<the rewritten body>"}}

=== BODY ===
{body}
"""
