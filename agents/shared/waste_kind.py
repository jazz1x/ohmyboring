"""C-WASTEVIEW1 S2: the waste kind a line belongs to, by fixed word rules.

The kinds and their words are plan wiki-00000038878 (3) as written, not tuned to any sample.
A line may carry several kinds or none. "Quality" and "asked again / went against a decision"
are only marked here by words; judging them is S3's model step.
"""

import re

WASTE_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("되풀이", re.compile(r"또\s|다시|몇\s?번|계속|자꾸|번째")),
    ("품질", re.compile(r"제대로|장난|대충")),
    ("범위·순서", re.compile(r"멈춰|그만|말고|하지\s?마")),
    ("못 찾음", re.compile(r"못\s?찾|찾아|찾냐|모르")),
    ("창·프로세스", re.compile(r"창|띄우|띄워|키냐|키던|켜")),
    ("모델·환경", re.compile(r"모델|오푸스|페이블|opus|fable|플러그인", re.I)),
)


def waste_kinds(text: str) -> list[str]:
    """Every kind whose words appear in `text`, in the table's order."""
    return [name for name, words in WASTE_KINDS if words.search(text)]
