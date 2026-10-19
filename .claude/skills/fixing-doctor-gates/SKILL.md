---
name: fixing-doctor-gates
description: oh-my-boring 저장소에서 doctor 점검(scripts/doctor.sh)이나 게이트(scripts/guard.sh)를 새로 세우거나 고칠 때의 규칙. 못 잼은 빨강, 창은 요청에 싣기, 닫힌 창은 정보 줄, 통제군과 변이 시험, 새 시험 파일 이름. Triggers - "doctor", "게이트", "gate", "✗", "못 잼", "cannot measure", "통제군", "변이", "test_doctor", "guard.sh", "DEPLOY DRIFT".
---

# fixing-doctor-gates

게이트를 세우거나 고치기 전에 부른다. 게이트는 `scripts/doctor.sh`·`.pre-commit-config.yaml` 안에 세운다. CI 에는 세우지 않는다 — CI 는 비밀값 검사·이미지 빌드만 돈다 (`~/.claude/hooks/` 는 늘리지 않는다 — CLAUDE.md §3). 날짜 박힌 사고 이야기는 커밋 본문에 있다. 여기는 규칙만 둔다.

## 1. 못 잼은 빨강이다

도구가 없다, 인증이 안 됐다, 호출이 실패했다, 저장소를 못 읽는다, 분모가 0 이다 — 전부 「읽은 값 없음」이다. 이것을 0 이나 통과로 접지 않는다 (CLAUDE.md §4). 이유를 같은 줄에 찍는다.

- `scripts/doctor.sh` (d5d 문 이미지): `bad "cannot measure door image freshness — $door_why"`. docker 없음·`docker inspect` 실패·`git log` 실패·시각 파싱 실패·COPY 줄 없음이 각자 `door_why` 에 이유를 싣고 빨강이다.
- `agents/shared/uptake_core.py` `recent_duplicates`: `readable` False 와 `rows` 0 은 둘 다 「측정 불가」다. 깨끗한 통과가 아니다. doctor d5c 가 이를 빨강으로 읽는다 (`scripts/doctor.sh` 의 `recent_duplicates=` 처리).

부정형 단정(「0건」「없다」)은 탐지기가 그것을 볼 수 있음을 먼저 보인 뒤에만 쓴다.

## 2. 창은 요청에 싣는다

읽을 때 범위(일수·limit·`per_page`)를 요청에 넣는다. 전부 가져와서 뒤에서 자르지 않는다. 뒤에서 자르면 범위 밖 행이 메모리와 판정에 섞이고, 공집합이면 못 잼과 구분되지 않는다.

- `uptake_core.py` `recent_duplicates`: 읽는 루프 안에서 `ts >= cutoff` 로 거르고 `days` 를 보고서에 싣는다.

분자·분모를 같은 줄에 찍는다 (CLAUDE.md §5). 예: `3 duplicate row(s) of 40 in the last 7 days`.

## 3. 닫힌 판정 창은 정보 줄이다

판정이 끝난 창(`prd-section2-window`)은 빨강이 아니다. 숫자는 그대로 두고 판정 메모를 붙인 정보 줄로 낸다. 빨강은 지금 굴러가는 창(최근 7일)이 맡는다. 닫힌 창에 묶인 경보는 새 사건을 「옛것」으로 분류해 놓치고, 닫힌 창을 지우면 기록이 사라진다.

- `scripts/doctor.sh` d5c: `closed window [$since_day, $until_day) duplicates $dup_in/$total_in — verdict closed (prd-section2-window)`.
- `scripts/peek.py` 의 `duplicate_injections` 메모도 같은 구분을 쓴다 (창 안 = 기록, 최근 7일 경보 = doctor d5c).

죽은 표지 같은 판정 끝난 항목도 같은 모양이다: 빨강 대신 「known losses N (근거)」 정보 줄 (`data/contract/known-losses.tsv`).

## 4. 「가장 최근 X」는 결과 아닌 것을 건너뛴다

최신 항목이 결과가 아닐 수 있다(취소·skipped·진행 중). 그 항목을 판정에 쓰지 않고 더 오래된 결과로 넘어간다. 그리고 **고른 항목**에 나이 한도를 적용한다. 건너뛰기 전의 항목으로 나이를 재지 않는다.

- 모두 건너뛰면 못 잼이다 (규칙 1).

## 5. `--fix` 가 고치는 실패 플래그는 그 행동으로 고쳐지는 것만

`--fix` 가 플래그를 보고 행동한다면, 그 행동이 실패를 실제로 없앨 수 있어야 한다. 없앨 수 없는 실패(`--fix` 가 짓지 않는 이미지, 사람이 해야 할 일)는 별도 플래그를 쓰고 명령 한 줄만 안내한다. 같은 플래그를 공유하면 건강한 것을 「고치거나」 안 고쳐지는 걸 고쳤다고 쓴다.

- `scripts/doctor.sh`: `failed_door_image` 는 `--fix` 목록에서 `fix_*` 를 부르지 않고 `make door-build && make door-up` 한 줄만 찍는다. 명령 문구는 `delivering` 스킬 §1 표에서 그대로 복사한다. 두 번째 표현을 만들지 않는다.
- `failed_hooks` 도 같은 이유로 따로 있다 (doctor.sh 의 주석 참조).

## 6. 시험은 위반 + 통제군 (+ 못 잼 + 분모 0)

- 위반: 게이트가 잡아야 할 입력 → 빨강. **먼저 이 시험을 쓰고 빨강을 본 뒤** 고친다.
- 통제군: 비슷하지만 잡히면 안 되는 입력 → 통과. 통제군이 없으면 그 게이트는 「아무것도 안 잡는 것」과 구분되지 않는다.
- 못 잼: 도구 없음·호출 실패·읽기 실패 → 빨강이고 이유가 줄에 있다.
- 분모 0: 분모가 있는 게이트는 분모 0 도 한 장면이다 → 못 잼이지 통과가 아니다.
- 변이: 시험이 다 초록이면 구현을 망가뜨려 각 assert 가 빨개지는지 본다. 변이 전에 **구현을 커밋**한다. 변이는 `cp` 한 사본에서 하고 사본에서 복원한다 (`git checkout <file>` 금지). 훅이 `cp` 를 막으면 `Edit` 로 되돌리고 `git diff` 가 비어 있음을 보인다. 사본은 코드만 격리한다 — 변이가 라이브 DSN 을 건드리지 않게 한다. 변이 시험의 `__pycache__` 는 비운다.
- 예: `scripts/test_doctor.sh` 의 ledger-dupes(위반)·ledger-closed-only(통제군)·unreadable·empty_week(못 잼/분모 0), `scripts/test_doctor_door.sh`.

## 7. 등록과 기록

- 시험은 CI 에서 돌지 않는다. pre-commit 의 `changed-tests` 가 바뀐 `test_*.py`·`test_*.sh` 만 돌린다. 새 시험 파일은 이 이름을 따라야 커밋 때 돈다 (CLAUDE.md §1: 만들면 부른다).
- 새 게이트·점검은 같은 커밋에서 라이브로 한 번 돌려 줄을 인용한다. `bash -n` 과 임포트 성공은 증거가 아니다.
- `fix(doctor)`·`feat(...)` 커밋은 `CHANGELOG.md` 의 `## [Unreleased]` 에 줄이 있어야 한다 (`scripts/test_changelog.py`, `delivering` §4).
