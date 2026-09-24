# r5e3 보고 — 소유자가 말한 사실을 엔진이 세고 검색기가 받는다 (고침 라운드)

## 결론

1. 고침 라운드를 마쳤고, 판정에서 거짓이던 1.1절 (b) 가 참이 됐다. test_deep_agent 는 requirements.txt 를 설치한 사본 venv 에서 「Ran 2 tests OK」다.
2. 고친 것은 agents/memory/test_deep_agent.py 의 HITS 픽스처 두 항목에 `"said_by_owner": 0` 한 줄씩이다. retriever.py 는 그대로 엄격히 읽는다(엔진이 모르는 칸을 0 으로 채우면 「소유자가 말한 것 없음」이라는 거짓이 된다).
3. 오케스트레이터 지시에 따라 트렁크 feat/migration-python 에는 커밋하지 않았다. diff 전체(r5e3 엔진·소비자 절반 + 고침)를 wip/r5e3 브랜치 커밋 하나로 보관했다. 트렁크는 aefbbb2 그대로다.

## 결정 요청

1. 트렁크 반영. 페르소나 must_fix 는 「커밋 1개 → feat/migration-python 푸시」였지만 이 실행은 wip/r5e3 보관으로 끝났다. 반영하려면 wip/r5e3 의 커밋 하나를 트렁크에 cherry-pick 하면 된다. 누가 할지는 오케스트레이터나 소유자가 정한다.

## 실측 (고침 뒤)

1. 사본 venv(/private/tmp/claude-501/r5e3-fix, 치움)에서 test_deep_agent 2 OK, test_retriever 7 OK, test_store 8 OK.
2. 통제군: 픽스처 두 줄을 뺀 사본은 KeyError 로 errors=1 빨강. 고침이 실제로 그 자리를 잡는다.
3. guard.sh 5) 의 파이썬 시험 파일 45개 중 44개가 rc=0 이다. 나머지 test_door 는 호스트에 libpq 가 없어 psycopg ImportError 였고, `DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib` 를 주면 45 tests OK 다. agents/door 는 aefbbb2 와 diff 가 없으니 환경 문제다.
4. ruff check 깨끗, ruff format --check 「120 files already formatted」, test_changelog rc=0.
5. 같은 diff 에 실린 test_transcript 픽스처(여러 줄 `<command-args>`)도 확인했다. 사본 transcript.py 에서 re.S 를 지운 변이는 rc=1 빨강, 원본 복원 후 rc=0. 열림 목록의 re.S 픽스처 줄을 닫음(보관 브랜치)으로 표시했다.
6. cargo 는 이 라운드에서 다시 돌리지 않았다. 고침이 파이썬 픽스처뿐이라 판정 근거(cargo 324→327, SKIP 0, 변이 R1~R8·P1·P2 빨강)가 그대로다.

## 장부

1. 결정 기록에 페르소나 ledger 7줄과 고침 라운드 실측 1줄을 붙였다.
2. 열림 목록에 (r5e3) 9줄과 고침 라운드에서 본 2줄(test_door 의 libpq 원인, 트렁크 대신 wip 보관)을 붙였다. 닫음 표시: re.S 픽스처 줄, store_integration HNSW 줄(unique_direction 이 이 diff 에 실림). 둘 다 트렁크 미반영이다.
3. 이 보고서는 data/* 가 gitignore 라 `git add -f` 로 wip/r5e3 에 넣었다. 장부 파일은 추적하지 않는다.

## 다음 바퀴 (페르소나가 지명)

r5e4 생산자 절반. 「이 사이클이 끝나면 LangChain 검색기가 우리 기억을 꽂아 쓰고, 소유자가 실제로 친 글에서 뽑힌 사실만 said_by: owner 를 받는다 — 순위는 아직 안 올린다」. 닫을 것: aefbbb2 의 [user] 위에 distill_core owner_turns 올리기, 두 번째 모델 호출을 SessionEnd 130초 밖으로 빼기(먼저 p95 측정), /loop 재발화는 첫 발화만 세기, 헤드리스 세션 빼기. D1·D5 는 소유자 대기 그대로다.
