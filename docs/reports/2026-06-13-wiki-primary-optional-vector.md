# 결정: vault/wiki 1급 + pgvector 옵셔널 (2026-06-13)

## 결정
- **1급 메모리 = `vault/wiki/*.md`** (Karpathy-wiki). 에이전트·엔진이 마크다운을 직접 읽고 회수. 임베딩 불필요.
- **pgvector(vector + graph RAG) = 옵셔널** (`DRUDGE_VECTOR` 토글). graph+vector 를 "맛보고" 싶을 때 켬. 규모/정확도 trigger 넘을 때 주역.
- **엔진 폴백 유지** — 에이전트 없이도 코어(적재·회수) 동작. niche 에이전트 하드의존 회피.

## 근거 (트렌드)
- Karpathy 2026.4: 개인·소규모(수백 문서)는 **LLM-Wiki(마크다운 직독) > RAG** — 단순·신뢰·디버그.
- Letta 벤치: 평문 파일시스템 74% > 전용 벡터-스토어 메모리.
- repo 자체 철학(`drudge/CLAUDE.md`): "Use the simplest thing that works, trigger 전엔 escalate 안 함."
- 정석 유지: MCP·OpenAI호환·세션엔드 기록/주입·CQRS 두 문은 트렌드 정통 → 그대로.

## 현재 결합도 (실측)
- `Store::open` = 모든 명령 startup DB 연결 (main.rs). retrieve = 100% Postgres(`vector_search`+`text_search`). wiki 직독 경로 0.
- → "wiki 1급"은 신규 wiki-recall 경로 + Store 옵션화 필요.

## 단계 (전부 완료 2026-06-13)
1. ✅ **wiki-recall 모듈** (`wiki_recall.rs`) — vault/wiki 직독, 부분일치 스코어링. (PR #16)
2. ✅ **`DRUDGE_VECTOR` 토글 + Store 옵션화** — 엔진 Postgres 없이 기동, 핸들러 wiki/vector 분기. (PR #17)
3. ✅ **기본값 wiki-primary flip** — DRUDGE_VECTOR 기본 off, postgres `--profile vector` opt-in, Store::open connect 재시도. (PR #18)
4. ✅ **쓰기 게이트 → 에이전트 정문** (opt-in `DISTILL_VIA_AGENT`, 엔진 distill 폴백 유지). (PR #19)

## 두 문 (확정)
- 쓰기(적재+게이트): 에이전트 정문 (SessionEnd→hermes, session_id 전달, agent 게이트). 직통 뒷문 없음.
- 읽기(회수): 옆문 — wiki 직독(기본) 또는 pgvector(켰을 때). 읽기전용. Claude Code recall.py·make ask·MCP recall.
