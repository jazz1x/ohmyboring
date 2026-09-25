# RUNBOOK — 켜져 있는 것을 굴리는 법

README 는 **설치하고 쓰는 법**이다. 이 파일은 **이미 돌고 있는 것을 굴리고 고치는 법**이다.
둘은 독자가 다르다 — README 의 독자는 처음 온 사람이고, 여기 독자는 어제 켜둔 것이 오늘 조용한
이유를 찾는 사람이다.

이 파일의 모든 명령·경로는 `scripts/test_runbook.py` 가 실재를 검사한다. 이름이 바뀌면 게이트가
빨개진다 — 런북이 조용히 거짓말하는 상태로는 머지되지 않는다.

---

## 1. 지금 무엇이 돌고 있나

세 층이 따로 돈다. **한 층이 죽어도 다른 층은 조용히 계속 돈다** — 이게 고장이 안 보이는 이유다.

### (가) 컨테이너 — 항상

| 서비스 | 하는 일 | 죽으면 |
|---|---|---|
| `boring-drudge` | 엔진. 임베드·저장·그래프·`/search`·MCP | 회수와 쓰기 둘 다 멈춘다. 훅은 조용히 no-op |
| `boring-door` | 문. 엔진 앞 프록시(:7710) — 엔진 경로를 대신 전달하고 `/approved`·`/claim-source`·`/claim-sources`·`/projects?active_days=`·`/repairs/split-subjects` 는 스스로 답한다 | 아침 카드가 스스로 거부되고(등록 스크립트가 exit 2), 세션 시작 카드의 「오늘 승인한 것」 절이 빠진다. 엔진 자체는 뒤의 `:7700` 에 살아 있다 |
| `boring-postgres` | pgvector 저장소 | 엔진이 못 뜬다 |
| `boring-agent` | hermes — 크론 잡의 실행기 | 브리핑·수집 워커가 전부 안 돈다 |

```bash
make logs          # 엔진 로그
make agent-logs    # hermes 로그 (MCP 연결 진단)
```

### (나) 호스트 스케줄러 — launchd(macOS) / crontab(Linux)

| 라벨 | 주기 | 하는 일 |
|---|---|---|
| `com.ohmyboring.codex-ingest` | 20분 | Codex 세션 하나를 집어 증류·저장 |
| `com.ohmyboring.maintenance` | 매일 | `scripts/schedule-maintenance.sh run` — data-steward + retention |
| `com.ohmyboring.night-drain` | 03:20 | 밀린 Codex·Claude 세션을 한 번에 최대 40개 |
| `com.ohmyboring.morning-card` | 매일 08:00 | `scripts/schedule-card.sh run` — 아침 카드. 문(:7710)이 살아 있어야 돌고, 로그는 `/tmp/com.ohmyboring.morning-card.log` |

```bash
launchctl list | grep ohmyboring     # 세 번째 칸이 라벨, 두 번째가 마지막 종료 코드
make maintenance-status              # 등록 상태
make maintenance                     # 지금 한 번 돌린다
```

### (다) hermes 크론 — `~/.hermes/cron/jobs.json`

| 잡 | 주기 | 스크립트 |
|---|---|---|
| `memory-ingest-worker` | 20분 | `ingest-worker.py` — Claude 세션 하나를 증류 |
| `morning-briefing` | — | 꺼짐, 아침 카드로 대체 — 08:00 은 launchd `com.ohmyboring.morning-card` 가 받는다 |
| `weekly-briefing` | 월 09:00 | `weekly-briefing.py` |

**codex 수집기는 여기 없다.** 호스트 스케줄러가 정본이고, 설치기가 hermes 쪽 사본을 지운다(#370).
둘 다 켜져 있으면 진 쪽이 LLM 호출을 쓰고 빈손으로 끝난다.

### (라) 에이전트 훅 — 프롬프트마다

`~/.claude/settings.json` 의 `UserPromptSubmit`·`SessionStart`·`SessionEnd` 셋. 앞의 둘이 회수를
주입하고, `SessionEnd` 가 세션을 노트로 증류한다. 훅은 **실패해도 프롬프트를 막지 않는다** —
엔진이 죽어 있으면 조용히 아무것도 안 넣는다. 그래서 훅의 침묵은 정상과 구분되지 않는다.

---

## 2. 배달 — 머지가 끝이 아니다

`main` 에 들어간 것은 **아직 아무 데서도 돌지 않는다.** 바뀐 것에 따라 경로가 셋이다.

| 바꾼 것 | 해야 하는 것 |
|---|---|
| Python·훅·hermes 스크립트 | `python3 agents/shared/agent_wiring.py --install --boring-home "$PWD"` |
| Rust | `make build` 후 `docker compose up -d` |
| 둘 다 | 위 둘 다, 순서 무관 |

확인은 둘로 한다.

```bash
sh scripts/doctor.sh          # ✗ 가 0개인지
```

그리고 **소비자 쪽에서 한 번 돌려 값을 인용한다.** `doctor` 가 초록인 것은 배달됐다는 뜻이 아니다 —
브리핑 PR 다섯 개가 게이트를 전부 통과한 채 12일간 크론에 도달하지 못한 적이 있다.

- 엔진이면 `curl -s localhost:7700/health`
- 훅이면 훅을 직접 태워 본다 (§4 의 탐침)
- hermes 스크립트면 `~/.hermes/scripts/` 안의 사본이 체크아웃과 같은지 — `doctor` 가 대조한다

`docker compose up -d` 가 **`Recreated` 를 안 찍으면 옛 이미지가 그대로 돈다.** `Started` 만 보고
배달됐다고 읽지 않는다.

---

## 3. 증상 → 첫 명령

| 증상 | 첫 명령 | 무엇을 보나 |
|---|---|---|
| 회수가 안 들어온다 | `curl -s localhost:7700/health` | 엔진이 떴는지, `corpus_count` 가 0 아닌지 |
| 노트가 안 쌓인다 | `sh scripts/doctor.sh` | `note_freshness` 가 최신인지 |
| 크론 잡이 안 돈다 | `ls -t ~/.hermes/cron/output/<job-id>/ \| head -1` | **최신 파일의 시각**. 개수는 50에서 회전하므로 신호가 아니다 |
| 그 파일이 0바이트다 | `curl -s 'localhost:7700/events?limit=5&component=hermes-ingest-worker'` | **유휴인지 고장인지는 여기서 갈린다** (§4) |
| 브리핑이 안 온다 | `make agent-logs` | hermes 가 스크립트를 찾았는지, 경로가 막혔는지 |
| 카드가 안 왔다 | `./scripts/schedule-card.sh status` | 등록이 됐는지, 마지막 로그 줄 (`/tmp/com.ohmyboring.morning-card.log`) |
| 무엇이 정체돼 있나 | `make doctor` | `readiness_issue` 줄 |
| 판정 창 상태 | `make peek` | 표본·바닥·판정 (localhost 전용) |
| 엄격 점검 | `make readiness` | doctor 결함 하나라도 있으면 실패 |

**0 을 데이터로 읽지 않는다.** 명령 실패·계기 부재·모집단 공백이 전부 0 으로 보인다. 부정형 단정
("0건", "안 돈다")은 **탐지기가 그것을 볼 수 있음을 먼저 보인 뒤**에만 쓴다.

---

## 4. 안 돌 때 읽을 자리

**크론 잡의 출력이 이유를 적어 둔다.** 설정을 추측하기 전에 그 파일을 읽는다.

```bash
ls -t ~/.hermes/cron/output/cc33a556631a/ | head -3   # memory-ingest-worker
```

파일 안에 프롬프트와 응답이 통째로 있다. `Blocked:` 로 시작하는 줄이 있으면 hermes 가 스크립트를
거부한 것이고, `stored → wiki/wiki-NNNN.md` 가 있으면 성공한 것이다.

**0바이트 파일은 고장이 아니다.** 먹을 세션이 없으면 워커는 아무것도 출력하지 않고, 출력이 없으면
hermes 는 빈 파일을 남긴다. 유휴와 고장은 크론 파일로는 못 가르고, **이벤트로 갈린다** — 워커는
유휴 틱에도 `ingest_offer / offered=0` 를 남긴다.

```bash
curl -s 'localhost:7700/events?limit=5&component=hermes-ingest-worker' \
  | python3 -c 'import json,sys; [print(e["observed_at"], e["event"], e["status"]) for e in json.load(sys.stdin)["entries"]]'
```

응답의 최상위 키는 `entries` 다. `events` 로 읽으면 언제나 빈손이고, 그건 계기가 죽은 것처럼
보인다 — 실제로 그렇게 읽고 "계기가 없다"고 두 번 적었다.

호스트 스케줄러는 `/tmp` 에 남긴다.

```bash
tail -20 /tmp/com.ohmyboring.night-drain.log
tail -20 /tmp/com.ohmyboring.codex-ingest.log
```

**훅을 직접 태워 보는 탐침** — 주입이 무엇을 싣는지 눈으로 본다. 반드시 원장과 이벤트를 격리해서
돌린다. 안 그러면 판정 계열에 날조 행이 남는다.

```bash
echo '{"prompt":"어떻게 했더라, 다시 하기 전에","session_id":"probe"}' \
  | BORING_INJECTION_LEDGER=/tmp/probe-ledger.jsonl BORING_EVENT_SINK=spool \
    RECALL_SESSION_THROTTLE_SECONDS=0 python3 hooks/recall.py
```

끝나면 `/tmp/probe-ledger.jsonl` 을 지운다.

---

## 5. 멈추고 되돌리기

```bash
make down          # 스택 정지 (./data 는 남는다)
make up            # 다시
make backup-db     # 되돌릴 수 없는 일을 하기 전에
```

`~/.hermes/cron/jobs.json` 을 **손으로 고치지 않는다.** 데몬이 되돌린다. 잡의 모양은
`agents/shared/agent_wiring.py` 가 정본이고, 바꾸려면 거기를 고쳐 `--install` 을 돌린다.

---

## 6. 이 파일이 썩지 않게 하는 것

`scripts/test_runbook.py` 가 여기 적힌 `make` 대상·스크립트 경로·레포 파일이 실제로 있는지
검사하고, `scripts/guard.sh` 가 그걸 돌린다. 이름을 바꾸면서 이 파일을 안 고치면 커밋 시점에
빨개진다.

게이트가 못 보는 것은 **문장이 참인지**다. 주기·동작 설명이 바뀌면 그건 사람이 고쳐야 한다.
