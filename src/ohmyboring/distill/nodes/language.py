"""제목이 영어로 돌아왔을 때 한 번만 교정을 붙여 다시 묻는다."""

from __future__ import annotations

import re
import sys

from ohmyboring.adapters import llm
from ohmyboring.distill.prompts.correction import LANGUAGE_CORRECTION


def retry_language(state):
    retry = llm.call_llm(state["prompt"] + LANGUAGE_CORRECTION)
    if retry and re.search(r"[가-힣]", retry.get("title", "")):
        print("[distill-session] language retry → Korean OK", file=sys.stderr)
        return {"parsed": retry}
    print("[distill-session] language retry failed — keeping original", file=sys.stderr)
    return {}
