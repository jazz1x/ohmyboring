"""The morning card's display strings, one language per file (en.py · ko.py · ja.py).

Every string the card puts in front of the owner (title, buttons, verdict marks, the past-approval
head line, the lane headers, the register tag's localized name, the block-limit overflow line)
lives in those files, keyed by the language `card_advice.resolve_lang(config.note_lang())`
picks. card_view.py and card.py never spell out a display string themselves. Register icons are
not translatable text and stay in card_view.REGISTER_ICONS; the model's advice prompt is not a
display string and stays with card_advice.
"""

from __future__ import annotations

from typing import Literal

from ohmyboring.i18n import en, ja, ko

Lang = Literal["en", "ko", "ja"]

STRINGS: dict[Lang, dict[str, str]] = {"en": en.STRINGS, "ko": ko.STRINGS, "ja": ja.STRINGS}

REGISTER_LABELS: dict[Lang, dict[str, str]] = {
    "en": en.REGISTER_LABELS,
    "ko": ko.REGISTER_LABELS,
    "ja": ja.REGISTER_LABELS,
}
