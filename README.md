# oh-my-boring

**셀프호스팅 개인 메모리 RAG.** Claude Code(또는 아무 마크다운 노트) 작업 경험이 자동으로
로컬 벡터DB에 쌓이고, "전에 이거 어떻게 했더라"를 다시 꺼내 쓸 수 있다. 클라우드 0 · 데이터 100% 로컬.

```
세션/노트  ──증류──▶  vault/raw  ──sync──▶  pgvector(+그래프)  ──recall──▶  답변
```

## 스택
| 레이어 | 무엇 | 노출 |
|---|---|---|
| **Ollama** (호스트) | 임베딩 `bge-m3` · 합성 `gemma4:12b`(think=false) | localhost:11434 |
| **hermes-rs** (Rust) | ingest·retrieve·그래프추출·compile 엔진 | localhost:7700 |
| **Postgres/pgvector** | `knowledge` 테이블 = 벡터 + BM25 + CTE 그래프RAG | 내부 |
| **joseph** (선택) | Slack 비서 + 크론 (Socket Mode) | 포트 0 |

## 빠른 시작
사전: **Docker** + 호스트 **Ollama**(`ollama serve` 떠 있어야 함).

```bash
git clone git@github.com:<you>/oh-my-boring.git && cd oh-my-boring
cp .env.example .env          # Slack 안 쓰면 그대로 둬도 됨
make up                       # Ollama 확인 → 모델 pull → 빌드 → 기동 → 초기 sync
make ask Q="내가 X 어떻게 했지?"
```
끝. 기본값에 회사/개인 구분 없음 — 모든 문서 `origin=personal`.

## 소스
`HERMES_SOURCE_DIRS`(`.env`/compose)가 흡수 대상. 기본 = `~/.claude/projects` (Claude Code 메모리)
+ `vault/`(증류 노트·메모). `make remember M="..."` 로 한 줄 즉시 적재.

## 자가증강 루프 (핵심)
세션이 끝나면 알아서 쌓인다 — 수동 적재 불필요.

```
① 세션종료  →  SessionEnd 훅(hooks/distill-session.py)이 세션을 '문제해결 서사'로 증류
              →  vault/raw/*.md 저장  →  /sync 즉시 호출(detached)
② sync      →  compile(raw→wiki 큐레이션) → 임베딩 → pgvector upsert
              →  그래프(document/chunk/project/topic 엣지) 추출  [4h 스케줄러 / make sync / 세션종료시]
③ 회수      →  make ask  또는  joseph(Slack)  가 벡터+BM25 RRF 로 top-K 꺼냄
```
훅 설치(영속): `~/.claude/settings.json` 의 `hooks.SessionEnd` 에
`{"type":"command","command":"python3 ~/oh-my-boring/hooks/distill-session.py","timeout":130,"async":true}` 추가.

## Slack 비서 (선택)
`.env` 에 Slack 토큰 채우고 `make up` → 그 챗에서 `/hermes sethome` 한 번 → 크론 결과·알림이 거기로.

## 격리/태깅 (선택 — 기본 끔)
특정 경로 문서를 `origin=company` 로 태깅 + ingest 제외하려면 `.env`:
```
HERMES_COMPANY_SUBSTR=acme:acme-kb    # Rust ingest/origin/audit (경로 substring)
DISTILL_COMPANY_CWD=acme              # 세션 증류 훅 (cwd substring)
```
**코드 수정 0 — env만.** 비우면 회사 개념 자체가 꺼진다.

## 명령
`make help` 로 전체. 자주: `up` · `ask Q=` · `sync` · `remember M=` · `collect` · `guard`(fmt+clippy+test) · `logs` · `reset`(⚠️DB초기화).

## 엔지니어링 가드레일
`hermes-rs/{PHILOSOPHY,RUST-STYLE,ENFORCEMENT}.md` = SSOT. ROP · parse-don't-validate ·
clippy `-D warnings`(unsafe forbid + pedantic deny) 머지 게이트. pre-commit 우회 금지.
