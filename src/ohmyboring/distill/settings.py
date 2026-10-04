"""증류 설정 — 노트 언어와 해상도. 표준 라이브러리만 쓰므로 호스트 훅도 부른다."""

from __future__ import annotations

import os
import sys

from ohmyboring import config as boring_config
from ohmyboring.distill.resolution import ALLOWED_RESOLUTIONS, normalize_resolution

NOTE_LANG = boring_config.note_lang()


def distill_resolution() -> str:
    raw = os.environ.get("BORING_DISTILL_RESOLUTION")
    level = normalize_resolution(raw or "evidence", default="evidence")
    if raw and raw.strip().lower() not in ALLOWED_RESOLUTIONS:
        print(
            f"[distill-session] invalid BORING_DISTILL_RESOLUTION={raw!r}; using 'evidence'",
            file=sys.stderr,
        )
    return level
