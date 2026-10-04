"""브리핑 본문을 프로젝트·항목으로 나눈다 — 라벨 별칭과 머리말 판정."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ohmyboring.briefing.text import _slack_inline

EMPTY_VALUES = {
    "",
    "-",
    "—",
    "~",
    "...",
    "…",
    "없음",
    "없습니다",
    "해당 없음",
    "해당없음",
    "none",
    "None",
    "N/A",
    "n/a",
    "na",
    "null",
    "nil",
    "tbd",
    "to be determined",
    "to be decided",
    "to be continued",
    "추후 진행 예정",
    "추후 예정",
    "추후 결정",
    "추후 협의",
    "추후",
    "later",
    "pending",
    "보류",
    "待定",
    "待ち",
}
# Phrases that add zero signal to a briefing and should be dropped entirely.
TEMPLATE_BLACKLIST = [
    "다음 지시 기다림",
    "다음 지시를 기다림",
    "다음 지시를 기다리는 중",
    "추후 지시 기다림",
    "추후 지시를 기다림",
    "지시 기다림",
    "지시를 기다림",
    "waiting for instructions",
    "awaiting instructions",
    "wait for next instruction",
    "waiting for next steps",
    "to be continued",
]
LABEL_ALIASES = {
    "Done": "Done",
    "완료": "Done",
    "Next": "Next",
    "다음": "Next",
    "Blocked": "Blocked",
    "막힘": "Blocked",
    "Decisions": "Decisions",
    # The engine writes these in the singular ("Decision: …", "Risk: …") and the table only had
    # the plurals, so eight labelled items a day fell into the unlabelled bucket and were
    # reported as "기타". The category was not missing; the alias was.
    "Decision": "Decisions",
    "결정": "Decisions",
    "Risks": "Risks",
    "Risk": "Risks",
    "리스크": "Risks",
    # `fact` is the engine's DEFAULT claim kind, not a rare one -- measured 2026-08-13 the ledger
    # held 4936 facts against 1250 decisions -- and the alias table never had it, so the single
    # largest category the distiller produces was arriving every morning as "기타".
    "Facts": "Facts",
    "Fact": "Facts",
    "사실": "Facts",
    "Stalled": "Stalled",
    "Stall": "Stalled",
    "정체": "Stalled",
    "Block": "Blocked",
    "Blocker": "Blocked",
}
LABELS = set(LABEL_ALIASES)


@dataclass
class BriefItem:
    label: str
    text: str


#: A `## heading` is a project name only if it looks like one. The parser used to take whatever
#: the model wrote, so prose became the label the reader sees beside every item — measured across
#: 62 briefings, 109 of 165 distinct headings (66%) were not projects at all: `# 1. **문제 개요**`,
#: `Risk`, section titles from some other document's outline. A blind reader given eight of these
#: found the label disagreed with the item's own text in 14 of 24 top picks.
#:
#: Shape, not a list of known projects: the renderer has no database and asking one would put a
#: network call in a pure function. A project name here is a short single line with no markdown
#: emphasis, no numbering, and no sentence punctuation — which is what every real one looks like
#: (`foodspring-front`, `kb-rag-bot`, `boro-janus`) and what none of the 109 do.
#: The caller has already stripped leading `#`, so a heading arrives as `1. **문제 개요**` rather
#: than `# 1. …` — the first version of this pattern anchored on the hash and let every one of
#: them through.
_NOT_A_PROJECT = re.compile(
    r"""(?:
          ^\s*\d+[.)]\s     # "1. " — an outline number, which is followed by a space.
                            # `1.95.3-stage-검증` is a real project here and must survive: a
                            # version number runs straight into the next character.
        | [*_]{2}          # **bold** anywhere: prose, not a name
        | ^\s*[*_]\w       # _italic_ opening
        | [.!?。]\s*$       # ends like a sentence
        | \(               # a parenthetical the model appended
      )
    """,
    re.VERBOSE,
)
#: Labels the briefing already understands (`Risk`, `Done`, `Stalled`, …). A heading that is one
#: of these is a section, not a project — the label branch above catches the canonical spellings,
#: this catches the rest before they become a project name.
_LABEL_WORDS = frozenset(w.lower() for w in LABEL_ALIASES)
#: Long enough to hold `omm-consumer-rollout-plan` (25) and `bi-slack-analytics-bot` (22); short
#: enough to reject `bi-slack-analytics-bot (S11 요구사항)` (32), which is a project name with a
#: parenthetical the model added and is not the name of anything.
_PROJECT_HEADING_MAX = 28

#: Group name for items whose heading was not a project the reader could look up.
UNATTRIBUTED = "Brief"


def _is_project_heading(heading: str, known_projects=None) -> bool:
    """Is this heading a project the reader could look up?

    Two gates, and the order matters. The shape test below rejects headings that cannot be a name
    -- outline numbers, bold prose, sentences. It is sound but not complete: of the 121 headings it
    passed across the 62 briefings in the vault, only 55 named a project the corpus knows. The rest
    were plausible strings ("다른 프로젝트") that no pattern can tell from a real name.

    So when the corpus can be asked, membership decides. `known_projects` is the set of names the
    engine actually has documents for; a heading outside it is not a project, however name-shaped.

    When it cannot be asked -- `known_projects` is None -- this falls back to the shape test plus a
    length cap. That is deliberate: an unreachable engine must not be read as "no projects exist",
    which would strip the label off every item and look like a corpus with nothing in it.

    An empty list is not that case. It is the corpus answering that it holds no projects, and then
    no heading names something the reader can look up, so none of them is a project.
    """
    text = (heading or "").strip()
    if not text:
        return False
    if text.lower() in _LABEL_WORDS:
        return False
    if _NOT_A_PROJECT.search(text):
        return False
    if known_projects is None:
        # The engine could not be asked. Length stands in for membership here and nowhere else:
        # a heading long enough to be a sentence is prose, and with no list to check against that
        # guess is the only thing left. It costs real projects -- three of the 126 names the corpus
        # holds are longer than this -- which is why it does not apply once the list is available.
        return len(text) <= _PROJECT_HEADING_MAX
    return text.lower() in {str(name).strip().lower() for name in known_projects}


@dataclass
class BriefProject:
    name: str
    items: list[BriefItem] = field(default_factory=list)


@dataclass
class BriefDocument:
    projects: list[BriefProject] = field(default_factory=list)


def parse_brief(answer: str, known_projects=None) -> BriefDocument:  # noqa: C901
    doc = BriefDocument()
    current: BriefProject | None = None
    previous_heading = ""
    pending_label = ""

    for raw in answer.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            plain_heading = _plain_label(heading)
            if plain_heading in LABELS:
                # Sub-heading like "### Done" sets the pending label.
                pending_label = canonical_label(plain_heading)
            elif _is_project_heading(heading, known_projects):
                if heading and heading != previous_heading:
                    current = BriefProject(heading)
                    doc.projects.append(current)
                    previous_heading = heading
                pending_label = ""
            else:
                # A heading that is neither a label nor a project name. Keeping the previous
                # project means the items below stay attributed to whatever came before, which is
                # wrong but silent; opening a new group under this text makes the model's prose
                # the project name, which is wrong and loud. Neither is acceptable, so the items
                # go into the unattributed group and the reader is not told a project they can
                # look up when there is none.
                current = None
                previous_heading = ""
                pending_label = ""
            continue

        plain = _plain_label(stripped)
        if plain in LABELS:
            pending_label = canonical_label(plain)
            continue

        bullet = _strip_bullet(stripped)
        item = parse_item(bullet if bullet is not None else stripped, pending_label)
        # A plain (non-bullet) line consumes the pending label; a bullet line keeps
        # it so multiple bullets under one label heading share the same label.
        if bullet is None:
            pending_label = ""
        if item is None:
            continue
        if current is None:
            # One unattributed group, not one per stretch of orphans. Headings the model writes
            # between real projects would otherwise open a second and third "Brief", and a reader
            # seeing the same group name three times reads it as three different things.
            current = next((p for p in doc.projects if p.name == UNATTRIBUTED), None)
            if current is None:
                current = BriefProject(UNATTRIBUTED)
                doc.projects.append(current)
        current.items.append(item)

    doc.projects = [project for project in doc.projects if project.items]
    return doc


def parse_item(text: str, pending_label: str = "") -> BriefItem | None:
    normalized = _slack_inline(text)
    for label in LABELS:
        for sep in (":", "：", " - ", " — "):
            prefix = f"{label}{sep}"
            if normalized.startswith(prefix):
                rest = normalized[len(prefix) :].strip()
                if rest in EMPTY_VALUES or _is_template_noise(rest):
                    return None
                return BriefItem(canonical_label(label), rest)
    if pending_label:
        if normalized in EMPTY_VALUES or _is_template_noise(normalized):
            return None
        return BriefItem(pending_label, normalized)
    if normalized in EMPTY_VALUES or _is_template_noise(normalized):
        return None
    return BriefItem("", normalized)


def canonical_label(label: str) -> str:
    return LABEL_ALIASES.get(label, label)


def _strip_bullet(line: str) -> str | None:
    if line.startswith(("- ", "* ", "• ")):
        return line[2:].strip()
    head, sep, tail = line.partition(". ")
    if sep and head.isdigit():
        return tail.strip()
    return None


def _plain_label(line: str) -> str:
    return line.strip().strip("*").strip().rstrip(":：")


def _is_template_noise(text: str) -> bool:
    """Return True for vacuous 'waiting for instructions' style bullets."""
    lowered = text.lower().strip(" .·")
    return any(noise.lower() in lowered for noise in TEMPLATE_BLACKLIST)
