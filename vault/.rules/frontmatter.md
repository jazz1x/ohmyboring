# Frontmatter + Wikilink 규약 — 사람용 SSOT

> 봇/도구 기계 파싱 SSOT 는 [`.rules/schema.yaml`](schema.yaml). 이 파일과 정합 유지 필수.
>
> 이 규약은 **컴파일된 `vault/wiki/wiki-NNNN.md`** 페이지에 적용된다. `vault/raw/` 의 증류 노트는
> frontmatter 없이 자유 markdown 이고, `drudge vault compile` 이 raw→wiki 로 큐레이션하며 frontmatter 를 생성한다.

---

## 필수 필드 (required_frontmatter)

| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | string | 페이지 ID. 파일명 stem 과 일치. 패턴 `wiki-NNNN[N]` |
| `title` | string | 페이지 제목. 한 줄, 명확하게 |
| `kind` | enum | `note` \| `memory` \| `session` \| `decision` |
| `origin` | enum | `personal` \| `company` |
| `date` | string | 작성일 `YYYY-MM-DD` |

## 선택 필드 (optional)

| 필드 | 타입 | 설명 |
|------|------|------|
| `sources` | list[string] | 출처 파일 경로 (prefix: `raw/`, `meta/`, `.rules/`) |
| `relates_to` | list[string] | 연관 페이지 ID 목록 (`wiki-NNNN`) |
| `tags` | list[string] | 분류 태그 (Obsidian-안전: 공백·특수문자 → `-`. `repo/<slug>` nested 태그 포함) |
| `superseded_by` | string | 이 페이지를 대체한 페이지 ID (`wiki-NNNN`) |
| `summary` | string | 한 줄 요약 (200자 이내 권장) |

---

## ID 규칙

- 패턴: `^wiki-\d{4,5}$` (4~5자리 숫자). 파일명 stem == frontmatter `id`.
- 단조 증가(monotonic). 한 번 배정된 ID는 재사용 금지.
- 삭제 시: 파일 삭제 대신 tombstone — 본문 비우고 `superseded_by` 남김.

## Wikilink 규약

- 본문 페이지 참조: `[[wiki-NNNN]]` (Obsidian 표준).
- 교차 레이어 링크(`[[raw/...]]`·`[[meta/...]]`) 금지 — `sources:` 필드로 참조.
- dangling `[[wiki-NNNN]]`(대상 부재)는 `vault lint` 가 error.
