---
name: delivering
description: oh-my-boring 저장소에서 변경을 머지 뒤 소비자 쪽(~/.hermes/scripts, 컨테이너, 호스트 바이너리, hermes cron)까지 배달하는 절차. 변경 종류별 배달 명령, 배달 증거, update-branch 뒤 차이 확인, 머지 명령 규칙. Triggers - "배달", "deliver", "머지 뒤", "머지했다", "door-build", "door-up", "agent_wiring", "hermes cron", "Recreated", "DEPLOY DRIFT", "update-branch".
---

# delivering

머지는 끝이 아니다. `~/.hermes/scripts`·컨테이너 이미지·호스트 CLI 바이너리·hermes cron 은 레포와 별도 아티팩트다. 자리·머지 조건은 전역 `working-in-seats`·`merging-and-deploying` 이 정본이고, 여기는 이 저장소의 명령만 둔다.

## 1. 변경 종류 → 배달 명령

| 바뀐 것 | 명령 | 통과 조건 |
|---|---|---|
| Python · 훅 · `~/.hermes/scripts` 로 복사되는 것 | `python3 agents/shared/agent_wiring.py --install --boring-home "$PWD"` | `sh scripts/doctor.sh` 에 `DEPLOY DRIFT` 줄이 없다 |
| Rust (`drudge/`) | `make build` 후 컨테이너 재기동 | `make build` 가 compose 이미지(엔진·문)와 호스트 CLI 바이너리를 함께 만든다. doctor 가 engine build_sha 와 호스트 CLI 스탬프를 체크아웃과 대조한다 |
| 문 이미지가 COPY 하는 경로 전부: `src/`, `agents/door/`, `agents/slack/`, `agents/shared/`, `agents/memory/`, `agents/hermes/`, `pyproject.toml`, `requirements.txt`, `data/contract/engine-contract.json` | 위 `agent_wiring --install` **에 더해** `make door-build && make door-up` | 출력에 `Recreated` |
| hermes cron 작업 추가·수정·삭제 | `docker exec boring-agent hermes cron create …` (`edit` · `pause` · `resume` · `remove` 도 같은 CLI) | 한 주기 뒤 `docker exec boring-agent hermes cron list` 재확인 |

문 이미지 경로는 `agents/door/**` 만이 아니다. 목록의 정본은 `agents/door/Dockerfile` 의 `COPY` 줄이다 — 목록과 다르면 Dockerfile 이 이긴다. `agents/shared/`·`agents/hermes/`·`src/` 를 고치고 `door-build` 를 빼먹기 쉽다.

hermes cron 등록 예(스크립트는 `~/.hermes/scripts/` 안 이름, 모델 없이 스크립트만 돌림):

```sh
docker exec boring-agent sh -c 'hermes cron create --name advice-write --no-agent --script run-advice-write.py --deliver local --failure-deliver slack:<DM> "45 7 * * *"'
```

한 번만 울리는 알림은 `--repeat 1` 과 날짜를 박은 식(`"12 8 20 10 *"`)을 쓴다. 출력의 `Next run:` 을 인용한다.

- hermes 는 외부 이미지(`make hermes-build`)다. `make build` 에 들어 있지 않다.
- `~/.hermes/cron/jobs.json` 은 손으로 고치지 않는다. 데몬이 되돌린다.
- `up`/`build` 전량은 정본 체크아웃(`/Users/jongyun/Development/mine/oh-my-boring`)에서만 친다. 워크트리에서는 `make door-build`·`make door-up` 만 친다.
- 엔진·DB·agent 컨테이너(`boring-drudge`·`boring-postgres`·`boring-agent`)는 워크트리에서 건드리지 않는다. 문만 다룰 때는 `--no-deps` 를 빼지 않는다 (`depends_on` 수렴이 엔진을 재생성한다. `door-up` 은 이미 `--no-deps`).
- 이전 트렁크(`feat/migration-python`) 위 슬라이스의 배달 시점은 `migration-slice` 가 정한다.

## 2. 배달 증거

명령이 성공했다는 출력은 증거가 아니다. 셋을 모두 인용한다.

1. 컨테이너 생성 시각이 머지 시각보다 뒤다. `Recreated` 만으로는 부족하다.
   - `docker inspect -f '{{.Created}}' <컨테이너>` (문은 `boring-door`, 엔진은 `boring-drudge`)
   - `gh pr view <N> --json mergedAt`
2. 소비자 쪽에서 한 번 돌려 값을 인용한다. 카드 ts, 크론 output 파일(`~/.hermes/cron/output/<job-id>/`), 훅이 주입한 줄, `/health` 필드처럼 소비자가 낸 산출물이다. 인용이 없으면 배달은 열려 있다.
3. `sh scripts/doctor.sh` 가 ✗ 0.

프로세스 생존(`pgrep`)·로그 꼬리(`tail`)는 증거가 아니다.

## 3. 검증한 head 뒤 브랜치가 main 에 뒤처졌을 때

1. `gh pr update-branch <N>`
2. PR 자신의 diff 를 갱신 전후로 비교한다. `CHANGELOG.md` 는 제외한다 (main 이 앞서 가며 항상 달라진다).
   - `git diff <base> <head> -- . ':!CHANGELOG.md' | git patch-id`
3. 판정:
   - patch-id 같음 → 재검증하지 않는다.
   - 다름 → 달라진 부분만 재검증한다.
4. 두 경우 모두 **새 head** 에서 CI 가 초록인 뒤에만 머지한다.

## 4. CHANGELOG

`feat(...)`·`fix(...)` 커밋이 있는 브랜치는 `CHANGELOG.md` 의 `## [Unreleased]` 아래에 줄이 있어야 한다 (`scripts/test_changelog.py`, `scripts/guard.sh` 와 CI rust-gate 가 돌린다). `docs`·`chore`·`test`·`refactor`·`ci` 는 요구하지 않는다.

## 5. 머지 명령

머지 명령(`gh pr merge …`)은 **자기 줄 하나로만** 친다. 다른 명령과 `&&`·`;`·파이프로 잇지 않는다 (저장소 훅이 막는다). 머지 뒤 정리는 `~/.claude/teardown.md` 를 따른다.
