"""노트 색인 — 문 프로세스 안의 경로 → (바뀜 표지, 중복 문이 쓰는 파싱 결과) 표 (E3b-2).

그림자의 중복 문이 매 쓰기마다 볼트 3,081장을 통째로 읽고 순수 파이썬 YAML 로더로
파싱하던 것(그림자 8.9s 중 ~6s, 호스트 실측 파싱 3.77s·읽기 2.6s)을 줄인다. 색인은
쓰기마다 디렉터리 목록·stat 만 하고, 바뀌었거나 새로 생긴 노트만 다시 읽고 파싱한다.
사라진 노트는 색인에서 빠진다.

판정은 바꾸지 않는다 — 파싱은 중복 문이 쓰는 parse_existing_note 그 자리를 그대로 쓰고,
색인은 읽기를 줄일 뿐 갈래 판정에 개입하지 않는다. 엔진이 쓰기 순간 디스크를 훑는
것(mcp.rs:1718-1740)과 색인의 동기화 시점 볼트 사이에는 이미 그림자가 응답 뒤에 도는
이상 언제나 벌어지는 시차가 있다 — 목록·stat 표지가 같은 노트는 디스크 훑기가 지금
읽어도 같은 결과를 내는 노트다.

미리 채우기 — 문은 자주 재기동하므로, 빈 색인으로 받은 첫 쓰기가 9초를 그대로 낼 수
없다. 문이 뜰 때 백그라운드 스레드가 색인을 통째로 채우고(응답을 막지 않게), 채우는
동안 들어온 쓰기는 sync() 가 None 을 돌려 주어 옛날 디스크 훑기 길로 떨어진다 — 그
결정이 곧 엔진과 같은 결정이다. 채우기에 걸린 시간은 호출자(문)가 로그에 남긴다.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ohmyboring.remember.dedup import ExistingNote, parse_existing_note

#: 한 노트의 바뀜 표지 — 수정 시각(나노초)·크기. 둘 다 같은 노트는 다시 읽지 않는다.
_Marker = tuple[int, int]


@dataclass(frozen=True)
class DiskSeams:
    """색인이 볼트를 읽는 네 면 — dedup.VaultView 의 디스크 면과 같은 모양·같은 계약.

    쓰기 면은 없다: 색인은 읽기만 하는 표다."""

    vault_dir: str
    list_notes: Callable[[], list[str]]
    read_note: Callable[[str, str], str | None]
    split_frontmatter: Callable[[str], tuple[str, str] | None]


@dataclass(frozen=True)
class SyncResult:
    """동기화 한 번의 결과 — 후보 (경로, 파싱 결과) 목록과 시간을 칸별로 나눈 것.

    stat_s 는 디렉터리 목록·stat 시간, read_s·parse_s 는 바뀐 노트를 다시 읽고
    파싱한 시간이다. 그림자 사건의 칸(elapsed_vault_s·elapsed_parse_s)에 그대로
    싣는다 — 전체가 칸들의 합으로 설명되게 하기 위함이다."""

    entries: tuple[tuple[str, ExistingNote], ...]
    stat_s: float
    read_s: float
    parse_s: float


class NoteIndex:
    """볼트 wiki 노트의 (표지, ExistingNote) 표 — 문 프로세스 안에 두고 쓴다.

    읽기 면(목록·stat·읽기·파싱)만 주입받고 쓰기 면은 없다 — 볼트·DB·그래프에 손을
    대는 일은 여전히 없다. prefill() 로 통째로 채운 뒤, 쓰기마다 sync() 로 목록·stat
    만 맞춘다. prefill 이 끝나지 않았거나 실패했으면 sync() 는 None — 호출자는 디스크
    훑기로 돌아간다(결정이 색인으로 바뀌는 일은 없다)."""

    def __init__(
        self,
        seams: DiskSeams,
        parse_note: Callable[
            [str, str, Callable[[str], tuple[str, str] | None]], ExistingNote
        ] = parse_existing_note,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seams = seams
        self._parse_note = parse_note
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[_Marker, ExistingNote]] = {}
        self._ready = threading.Event()
        self._usable = False
        self.prefill_s: float | None = None
        self.prefill_count: int = 0

    @property
    def ready(self) -> bool:
        """prefill 이 끝났는지 — 끝났어도 usable 이 False 면 디스크 훑기로 돌아간다."""
        return self._ready.is_set()

    @property
    def usable(self) -> bool:
        """색인을 써도 되는지 — prefill 실패(볼트 불응 등)면 늘 False."""
        return self._usable

    def prefill(self) -> None:
        """처음부터 통째로 채운다 — 문 기동 뒤 백그라운드 스레드가 돌린다(응답을 막지 않게).

        실패핏값 없다: 무슨 일이 나도 ready 는 세팅되고 usable 만 False — sync() 가
        None 을 돌려 그림자를 디스크 훑기로 본다. 걸린 시간·채운 수는 prefill_s·
        prefill_count 에 남아 문 로그에 인용된다."""
        started = self._clock()
        usable = False
        try:
            entries, _, _, _ = self._collect({})
            with self._lock:
                self._entries = entries
            usable = True
        except Exception:  # noqa: BLE001 — 색인 실패는 그림자를 죽이는 일이 아니라 훑기로 귀결
            usable = False
        finally:
            self._usable = usable
            self.prefill_s = self._clock() - started
            self.prefill_count = len(self._entries)
            self._ready.set()

    def sync(self) -> SyncResult | None:
        """쓰기 한 번의 재고 — 목록·stat 만 보고 바뀐 것만 다시 읽고 파싱한다.

        prefill 이 끝나지 않았거나 실패했으면 None — 호출자(그림자)는 디스크 훑기로
        돌아가 그 결정(엔진과 같은 결정)을 내린다. 사라진 노트는 여기서 빠진다."""
        if not (self._ready.is_set() and self._usable):
            return None
        with self._lock:
            entries, stat_s, read_s, parse_s = self._collect(self._entries)
            self._entries = entries
        return SyncResult(
            entries=tuple(
                (f"/vault/wiki/{note_id}.md", existing) for note_id, (_, existing) in sorted(entries.items())
            ),
            stat_s=stat_s,
            read_s=read_s,
            parse_s=parse_s,
        )

    def _collect(
        self, base: dict[str, tuple[_Marker, ExistingNote]]
    ) -> tuple[dict[str, tuple[_Marker, ExistingNote]], float, float, float]:
        """목록·stat 을 맞추고 바뀐 노트만 다시 읽고 파싱해 새 표를 짠다.

        목록 직후 사라진 노트는 stat 에서, stat 직후 사라진 노트는 읽기에서 빠진다 —
        디스크 훑기가 그 순간 못 읽는 것과 같은 후보 제외다. 시간은 목록·stat 은 stat_s,
        다시 읽은 것은 read_s·parse_s 로 나눠 잰다."""
        clock = self._clock
        read_s = 0.0
        parse_s = 0.0
        live: dict[str, tuple[_Marker, ExistingNote]] = {}
        loop_started = clock()
        for note_id in self._seams.list_notes():
            try:
                st = os.stat(os.path.join(self._seams.vault_dir, "wiki", f"{note_id}.md"))
            except OSError:
                continue
            marker: _Marker = (st.st_mtime_ns, st.st_size)
            entry = base.get(note_id)
            if entry is not None and entry[0] == marker:
                live[note_id] = entry
                continue
            started = clock()
            text = self._seams.read_note(self._seams.vault_dir, note_id)
            read_s += clock() - started
            if text is None:
                continue
            started = clock()
            existing = self._parse_note(f"/vault/wiki/{note_id}.md", text, self._seams.split_frontmatter)
            parse_s += clock() - started
            live[note_id] = (marker, existing)
        stat_s = clock() - loop_started - read_s - parse_s
        return live, stat_s, read_s, parse_s
