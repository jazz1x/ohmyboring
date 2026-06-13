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

## 단계
1. **wiki-recall 모듈** (`wiki_recall.rs`) — vault/wiki 마크다운 직독, 키워드 스코어링, top-K. Postgres 불필요. 순수+테스트. (additive, 무중단) ← **이번 PR**
2. **`DRUDGE_VECTOR` 토글 + Store 옵션화** — off면 wiki-recall, on이면 현 vector 경로. 엔진이 Postgres 없이 기동.
3. **기본값 wiki-primary 로 flip** — vector opt-in. compose pgvector 조건부.
4. **쓰기 게이트 → 에이전트 정문** (두 문: SessionEnd 훅 `hermes` 경유, 읽기 옆문 직통). distill/compile 게이트는 엔진 폴백 유지.

## 두 문 (확정)
- 쓰기(적재+게이트): 에이전트 정문 (SessionEnd→hermes, session_id 전달, agent 게이트). 직통 뒷문 없음.
- 읽기(회수): 옆문 — wiki 직독(기본) 또는 pgvector(켰을 때). 읽기전용. Claude Code recall.py·make ask·MCP recall.
