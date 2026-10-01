"""PII 게이트 — drudge 의 pii.rs 를 파이썬으로 미러한다 (순수).

정본: drudge/src/pii.rs. 볼트 rules/pii.yaml + pii.local.yaml 을 읽어 모양 기반 규칙을
만들고, remember 쓰기 본단(mcp.rs:1597 apply_pii_gate)에서 쓰는 대로 칸마다 돌린다 —
block 규칙이 걸리면 노트를 버리게 하고(-32603 사유), redact 규칙은 자리에서 가리고,
flag 규칙은 살리되 pii-flag 태그를 붙인다. allow 규칙은 block/redact/flag 에서 빠지는
면책 구간이고, exemption_marker 가 있는 줄의 flag 는 면제다.

그림자가 쓴다 — 쓰기 0, 읽기·계산만. 실패(정규식 깨짐·베이스 없는 로컬 오버레이)는
예외 대신 사유 문자열 값으로 돌아온다(pii.rs 가 폐쇄적으로 서버를 띄우지 않는 것과 같은
취지 — 규칙을 못 읽으면 게이트를 「없음」으로 치지 않고 어긋남을 못 가린다고 말한다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ohmyboring.remember.parse import Claim, FrontMatter, RememberNote, sanitize_tag
from ohmyboring.result import Either, Err, Ok

#: 규칙 이름 → 동작 어휘 (pii.rs PiiAction, 소문자).
_ACTIONS = ("block", "redact", "flag", "allow")

#: flag 규칙이 걸리면 노트에 붙는 태그 — mcp.rs:1639.
_PII_FLAG = "pii-flag"

#: 규칙 파일이 하나도 없을 때 — 게이트 비활성(pii.rs 가 Ok(None) 을 돌리는 것과 같다).
_GATE_DISABLED: Any = None

#: allow 스팬을 가릴 때 쓰는 비공개 구간 자리표 — pii.rs 의 \u{E000}ALLOW{i}\u{E000} 그대로.
_PLACEHOLDER = "\ue000ALLOW{idx}\ue000"


@dataclass(frozen=True)
class PiiMatch:
    """규칙 하나의 적중 — pii.rs PiiMatch."""

    rule: str
    action: str
    severity: str
    reason: str


@dataclass(frozen=True)
class PiiScan:
    """한 텍스트의 스캔 결과 — pii.rs PiiScan (matched 원문은 담지 않는다: 비밀 경계)."""

    redacted: str
    redacted_count: int
    block: PiiMatch | None
    flags: tuple[PiiMatch, ...]


@dataclass(frozen=True)
class _CompiledRule:
    name: str
    regex: re.Pattern[str]
    action: str
    severity: str
    replacement: str | None
    reason: str


@dataclass(frozen=True)
class PiiScanner:
    """불러온 게이트 한 벌 — pii.rs PiiScanner."""

    default_action: str
    exemption_marker: str | None
    rules: tuple[_CompiledRule, ...]

    def scan(self, text: str) -> PiiScan:
        """block/redact/flag 를 한 번에 — pii.rs scan 의 순서 그대로."""
        allowed = self._allowed_spans(text)
        masked, tokens = _mask_spans(text, allowed)

        redacted = masked
        redacted_count = 0
        for rule in self.rules:
            if rule.action != "redact":
                continue
            replacement = _replacement(rule.replacement)
            found = rule.regex.findall(redacted)
            redacted_count += len(found)
            redacted = rule.regex.sub(replacement, redacted)
        redacted = _restore_spans(redacted, tokens)

        block: PiiMatch | None = None
        flags: list[PiiMatch] = []
        for rule in self.rules:
            if block is not None:
                break
            if rule.action not in ("block", "flag"):
                continue
            for match in rule.regex.finditer(text):
                if _overlaps_any(match.start(), match.end(), allowed):
                    continue
                if rule.action == "block":
                    # block 은 면제 마커를 무시한다 — pii.rs 그대로.
                    block = PiiMatch(rule.name, rule.action, rule.severity, rule.reason)
                    break
                if self.exemption_marker is not None and _line_contains(
                    text, match.start(), self.exemption_marker
                ):
                    continue
                flags.append(PiiMatch(rule.name, rule.action, rule.severity, rule.reason))
        return PiiScan(redacted, redacted_count, block, tuple(flags))

    def _allowed_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for rule in self.rules:
            if rule.action != "allow":
                continue
            spans.extend((m.start(), m.end()) for m in rule.regex.finditer(text))
        spans.sort(key=lambda s: s[0])
        # 겹치는 구간은 빠른 것부터 탐욕으로 합친다 — pii.rs allowed_spans 그대로.
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                continue
            merged.append((start, end))
        return merged


def _replacement(raw: str | None) -> str:
    """Rust regex 의 `$1` 참조를 파이썬 re 의 `\\\\g<1>` 로 옮긴다 — 규칙 파일 문법은 그대로."""
    if raw is None:
        return "[REDACTED]"
    return re.sub(r"\$(\d+)", r"\\g<\1>", raw)


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> tuple[str, list[tuple[str, str]]]:
    if not spans:
        return text, []
    out: list[str] = []
    tokens: list[tuple[str, str]] = []
    cursor = 0
    for idx, (start, end) in enumerate(spans):
        out.append(text[cursor:start])
        placeholder = _PLACEHOLDER.format(idx=idx)
        out.append(placeholder)
        tokens.append((placeholder, text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), tokens


def _restore_spans(text: str, tokens: list[tuple[str, str]]) -> str:
    out = text
    for placeholder, original in tokens:
        out = out.replace(placeholder, original)
    return out


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _line_contains(text: str, offset: int, needle: str) -> bool:
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    return needle in text[line_start:line_end]


def _read_rules(path: Path) -> Either[dict[str, Any], str]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        return Err(f"cannot read/parse PII rules {path}: {e}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return Err(f"PII rules {path} is not a mapping")
    policy = raw.get("policy") or {}
    if not isinstance(policy, dict):
        return Err(f"PII rules {path}: policy is not a mapping")
    unknown_policy = set(policy) - {"default_action", "exemption_marker"}
    if unknown_policy:
        return Err(f"PII rules {path}: unknown policy keys {sorted(unknown_policy)}")
    rules = raw.get("rules") or []
    if not isinstance(rules, list):
        return Err(f"PII rules {path}: rules is not a list")
    return Ok({"policy": policy, "rules": rules})


def _compile_rule(entry: Any, default_action: str) -> Either[_CompiledRule, str]:
    """규칙 맵 하나를 컴파일 — pii.rs load 의 규칙 검증(깨진 정규식은 이름과 함께 Err)."""
    if not isinstance(entry, dict):
        return Err(f"PII rule is not a mapping: {entry!r}")
    name = entry.get("name")
    pattern = entry.get("regex")
    if not isinstance(name, str) or not name or not isinstance(pattern, str):
        return Err(f"PII rule needs a name and a regex string: {entry!r}")
    if not isinstance(entry.get("replacement"), str | None):
        return Err(f"PII rule {name!r} replacement must be a string")
    action = entry.get("action") or default_action
    if action not in _ACTIONS:
        return Err(f"PII rule {name!r} action must be one of {_ACTIONS}, got {action!r}")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return Err(f"PII rule {name!r} has invalid regex: {e}")
    replacement = entry.get("replacement")
    severity = entry.get("severity")
    reason = entry.get("reason")
    return Ok(
        _CompiledRule(
            name=name,
            regex=regex,
            action=action,
            severity=severity if isinstance(severity, str) and severity else "warning",
            replacement=replacement,
            reason=reason if isinstance(reason, str) else "",
        )
    )


def _compile_rules(entries: list[Any], default_action: str) -> Either[tuple[_CompiledRule, ...], str]:
    """규칙 맵 목록을 컴파일 — 첫 깨진 규칙에서 Err."""
    compiled: list[_CompiledRule] = []
    for entry in entries:
        match _compile_rule(entry, default_action):
            case Err(reason):
                return Err(reason)
            case Ok(rule):
                compiled.append(rule)
    return Ok(tuple(compiled))


def _merge_overlay(policy: dict[str, Any], rules: list[Any], local: Path) -> Either[None, str]:
    """로컬 오버레이 합치기 — pii.rs merge_policy + rules.extend."""
    match _read_rules(local):
        case Err(reason):
            return Err(reason)
        case Ok(overlay):
            pass
    for key in ("default_action", "exemption_marker"):
        if overlay["policy"].get(key) is not None:
            policy[key] = overlay["policy"][key]
    rules.extend(overlay["rules"])
    return Ok(None)


def _finish_scanner(policy: dict[str, Any], rules: list[Any]) -> Either[PiiScanner, str]:
    """정책 확정 + 규칙 컴파일 — pii.rs load 의 마지막 반."""
    default_action = policy.get("default_action") or "flag"
    if default_action not in _ACTIONS:
        return Err(f"PII policy default_action must be one of {_ACTIONS}, got {default_action!r}")
    match _compile_rules(rules, default_action):
        case Err(reason):
            return Err(reason)
        case Ok(compiled):
            pass
    exemption_marker = policy.get("exemption_marker")
    return Ok(
        PiiScanner(
            default_action=default_action,
            exemption_marker=exemption_marker if isinstance(exemption_marker, str) else None,
            rules=compiled,
        )
    )


def _load_rules(base: Path, local_overlay: Path | None) -> Either[PiiScanner, str]:
    """베이스를 읽고 오버레이를 얹은 뒤 스캐너를 만든다."""
    match _read_rules(base):
        case Err(reason):
            return Err(reason)
        case Ok(raw):
            pass
    policy = dict(raw["policy"])
    rules = list(raw["rules"])
    if local_overlay is not None:
        match _merge_overlay(policy, rules, local_overlay):
            case Err(reason):
                return Err(reason)
            case Ok(_):
                pass
    match _finish_scanner(policy, rules):
        case Err(reason):
            return Err(reason)
        case Ok(scanner):
            pass
    return Ok(scanner)


def load(base: Path | None, local: Path | None) -> Either[PiiScanner | None, str]:
    """베이스 + 로컬 오버레이 불러오기 — pii.rs PiiScanner::load.

    둘 다 없으면 Ok(None)(게이트 비활성). 로컬만 있으면 Err(오버레이는 커밋된 베이스를
    덧쓰는 것이지 유일한 규칙이 아니다). 정규식이 깨지면 그 규칙 이름을 담아 Err.
    """
    base_present = base is not None and base.exists()
    local_present = local is not None and local.exists()
    if not base_present and not local_present:
        return Ok(_GATE_DISABLED)
    if not base_present:
        return Err(
            f"PII local overlay found but base rules missing: {local} — "
            "local overlays must extend a committed base"
        )
    return _load_rules(base, local if local_present else None)


def load_from_vault(vault_dir: str) -> Either[PiiScanner | None, str]:
    """볼트 규칙 경로에서 — pii.rs load_from_vault (rules/pii.yaml + pii.local.yaml)."""
    rules_dir = Path(vault_dir) / "rules"
    return load(rules_dir / "pii.yaml", rules_dir / "pii.local.yaml")


def _gate_batch(scanner: PiiScanner, flagged: list[bool], values: list[str]) -> Either[list[str], str]:
    """칸 값들을 순서대로 게이트 — 첫 block 에서 그 규칙을 사유로 Err, flag 는 누적."""
    out: list[str] = []
    for value in values:
        match _apply_to_field(scanner, value):
            case Err(block):
                return Err(_blocked_message(block))
            case Ok((redacted, hit)):
                flagged.append(hit)
                out.append(redacted)
    return Ok(out)


def _blocked_message(block: PiiMatch) -> str:
    """엔진 메시지 그대로의 차단 사유 — mcp.rs:1656."""
    return (
        f"PII gate blocked by rule '{block.rule}' ({block.severity}): "
        f"{block.reason} — matched sensitive text omitted"
    )


def _chunks(values: list[str], size: int) -> list[list[str]]:
    """claim 다섯 칸씩 도로 자르기 — 배치를 claim 목록으로 되돌리는 데 쓴다."""
    return [values[i : i + size] for i in range(0, len(values), size)]


def _apply_to_field(scanner: PiiScanner, field: str) -> Either[tuple[str, bool], PiiMatch]:
    """칸 하나에 게이트를 건다 — mcp.rs apply_pii_to_field. block 이면 Err(적중)."""
    out = scanner.scan(field)
    if out.block is not None:
        return Err(out.block)
    return Ok((out.redacted, bool(out.flags)))


def apply_pii_gate(scanner: PiiScanner, note: RememberNote) -> Either[RememberNote, str]:
    """노트 전체에 게이트를 건다 — mcp.rs:1597 apply_pii_gate 의 순서 그대로.

    title·본문·태그·tools·concepts·sources·claims(다섯 칸)을 가리고, flag 가 하나라도
    걸리면 pii-flag 태그를 붙인다. block 이 처음 걸리는 칸에서 그 규칙을 사유로 Err.
    엔진 순서(제목→본문→태그→tools→concepts→sources→claims)를 한 줄로 이어 한 번에
    친다 — 순서는 배치 안 위치로 그대로 재현한다."""
    claim_field_count = 5
    claim_values = [
        getattr(claim, key)
        for claim in note.front.claims
        for key in ("subject", "predicate", "value", "kind", "confidence")
    ]
    batch = [
        note.front.title,
        note.body,
        *note.front.tags,
        *note.front.tools,
        *note.front.concepts,
        *note.front.sources,
        *claim_values,
    ]
    flagged: list[bool] = []
    match _gate_batch(scanner, flagged, batch):
        case Err(reason):
            return Err(reason)
        case Ok(values):
            pass

    # 배치를 순서대로 다시 자른다 — 엔진 칸 순서가 바뀌지 않도록 인덱스로 고정한다.
    idx = 2
    tags_raw = values[idx : idx + len(note.front.tags)]
    idx += len(note.front.tags)
    tools_raw = values[idx : idx + len(note.front.tools)]
    idx += len(note.front.tools)
    concepts_raw = values[idx : idx + len(note.front.concepts)]
    idx += len(note.front.concepts)
    sources_raw = values[idx : idx + len(note.front.sources)]
    idx += len(note.front.sources)
    claims_raw = values[idx:]

    # 태그는 가린 뒤 다시 소독하고 중복을 뺀다 — mcp.rs:1610-1619.
    tags: list[str] = []
    for tag in tags_raw:
        if (clean := sanitize_tag(tag)) is not None and clean not in tags:
            tags.append(clean)

    claims: list[Claim] = []
    for claim, values_for_claim in zip(
        note.front.claims,
        _chunks(claims_raw, claim_field_count),
        strict=True,
    ):
        subject, predicate, value, kind, confidence = values_for_claim
        claims.append(Claim(subject, predicate, value, kind, confidence, said_by=claim.said_by))

    if any(flagged) and _PII_FLAG not in tags:
        tags.append(_PII_FLAG)

    front = FrontMatter(
        title=values[0],
        kind=note.front.kind,
        origin=note.front.origin,
        project=note.front.project,
        date=note.front.date,
        tags=tuple(tags),
        tools=tuple(tools_raw),
        concepts=tuple(concepts_raw),
        claims=tuple(claims),
        sources=tuple(sources_raw),
        omb_session_id=note.front.omb_session_id,
        author=note.front.author,
    )
    return Ok(RememberNote(front=front, body=values[1]))
