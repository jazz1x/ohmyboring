"""한국어 노트인데 제목이 영어로 돌아왔을 때 붙이는 교정 문구."""

LANGUAGE_CORRECTION = (
    "\n\n=== CORRECTION ===\nYour previous output was in English — that is WRONG. Re-emit the "
    "SAME JSON object but with title, body, tags, and concepts ALL in Korean (한국어). Keep code, "
    "IDs, and proper nouns verbatim."
)
