---
name: routine-hand-runs
description: oh-my-boring 판정 자리가 정기로 손으로 돌리는 명령 두 건 — 비서 일간(secretary_daily.py)과 월 1회 도구 누수(tool_scope.py). "일과 시작", "비서 일간", "secretary_daily", "도구 누수", "tool_scope", "정기 손 실행" 때 부른다. 창을 정하고 세는 일은 measuring-windows.
---

# routine-hand-runs

두 명령 모두 트렁크(`feat/migration-python`) 워크트리에서 돌린다 (main 에 합쳐지면 이 줄을 지운다). 자동으로 깨우는 장치는 없다 — 부르는 자리는 판정 자리이고, 그날 첫 턴에 부른다.

## 1. 평일 하루 1회 — 비서 일간

1. `python3 scripts/secretary_daily.py --day <끝난 날, 보통 어제 KST>` (`verdict_core.window_day` 와 같은 +09:00 달력 날짜). 돌리는 것은 평일이고, 채우는 것은 주말 포함 빈 달력 날짜 전부다 — 월요일에는 토·일도 `--day` 로 하나씩, 오래된 것부터 채운다. 모델 호출·장부 쓰기 없음.
2. 종료 코드가 0 이 아니거나 그 날 `~/Documents/ohmyboring/secretary/<day>-daily.html` 이 없으면 「못 함」과 stderr 마지막 줄을 노트에 적는다(어제 파일을 열지 않는다, CLAUDE.md §4).
3. 있으면 열어 평소 범위를 벗어난 지표를 읽는다. 같은 폴더의 `<day>-daily.json` 은 다음 날 비교 기준이니 지우지 않는다.
4. 판정 노트 하나(`mcp__ohmyboring__remember`).
5. 직전 달력 7일 중 JSON 이 있는 날이 7개 미만이면 (다) 세션 메시지·(라) 같은 시험 재실행 비교는 「아직」이고 비율을 내지 않는다(`measuring-windows` §6).

## 2. 월 1회(매월 첫 평일) — 도구 누수

1. `python3 scripts/tool_scope.py write` (`write` 가 기본 모드). `run-tool-scope.py` 는 card_runner 를 거쳐 Slack 토큰이 필요하니 쓰지 않는다. 시험 삼아 다시 돌릴 때는 `table` 을 쓴다 — `write` 는 지난 판정 노트를 대체해 다음 비교 대상을 바꾼다.
2. 종료 코드가 0 이 아니면 「못 함」과 stderr 마지막 줄을 노트에 적고 이전 달 HTML 을 읽지 않는다.
3. 이 명령은 장부에 `tool-scope:<profile>:<unit>` claim 을 쓰고 `~/Documents/ohmyboring/tool-scope/<day>.html` 을 만든다. 설정은 바꾸지 않는다 — 바꾸는 것은 소유자다. 모집단 = 프로필 3개(`--profiles` 기본값).
4. 출력의 `틀린 끔 a / 이전 끔 b`(b = runs/ 의 가장 새 이전 실행에서 끔이던 단위 수, a = 그중 그 뒤 호출된 수)를 판정 노트에 옮긴다. 출력에 `첫 실행 · prior_read=0` 이 있으면 `0/0` 은 「비교 없음」이다.
