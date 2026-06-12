# oh-my-boring

[![CI](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml/badge.svg)](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml)
![Rust](https://img.shields.io/badge/engine-Rust%20edition%202024-000?logo=rust)
![Postgres](https://img.shields.io/badge/store-Postgres%2016%20%2B%20pgvector-336791?logo=postgresql&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama%20(local)-000)
![cloud](https://img.shields.io/badge/cloud-none-success)

**셀프호스팅 개인 메모리 RAG.** Claude Code(또는 아무 마크다운 노트)에서 일한 경험이 자동으로 로컬 벡터DB에 쌓이고, *"전에 이거 어떻게 했더라"* 를 다시 꺼내 쓴다. **클라우드 0 · 데이터 100% 로컬.**

```text
세션·노트 ──증류──▶ vault/raw ──compile──▶ vault/wiki ──ingest──▶ pgvector(+그래프) ──recall──▶ 답변
   ▲ Claude Code            (LLM 큐레이션)              (임베딩·BM25·CTE)         ▲ make ask / Slack
   └ SessionEnd 훅이 자동 트리거 ──────────────────────────────────────────────────┘
```

---

## 왜 쓰나

- **자동 축적** — 세션 끝나면 훅이 알아서 '문제해결 서사'로 증류해 적재. 수동 정리 불필요.
- **로컬 전용** — 임베딩·합성 모두 호스트 Ollama. 외부 API·토큰 0. 노트는 네 디스크 밖으로 안 나감.
- **벡터 + 그래프** — 단순 유사도 검색이 아니라 problem/solution/tool/concept 노드·엣지까지 추출(GraphRAG).
- **회사/개인 분리 옵션** — env 토큰 하나로 특정 경로를 `origin=company` 태깅·격리. 기본은 전부 personal.

---

## 레이어

| # | 레이어 | 역할 | 기술 | 노출 | `make up` 기본 |
|---|---|---|---|---|:---:|
| 1 | **Ollama** (호스트) | 임베딩 `bge-m3`(1024d) · 합성 `gemma4:12b`(think=false) | 호스트 프로세스 | `127.0.0.1:11434` | 필요[^ollama] |
| 2 | **hermes-rs** (Rust) | ingest·retrieve·graph·compile·distill 엔진 (HTTP + 4h 스케줄러) | axum / tokio | `127.0.0.1:7700` | ✓ |
| 3 | **Postgres + pgvector** | `knowledge` = 벡터(HNSW) + BM25 + node/edge 재귀 CTE 그래프 | `pgvector/pgvector:pg16` | `127.0.0.1:5432` | ✓ |
| 4 | **훅** (호스트, Python) | 세션 → 엔진 연결 접착제 (distill·recall·collect) | `python3` | — | 수동 설치[^hooks] |
| 5 | **hermes-agent** (옵션) | Slack 비서 + 자율 크론 (Socket Mode) | 외부 이미지 | — | ✗ (`--profile agent`)[^agent] |

[^ollama]: 호스트에서 `ollama serve` 가 떠 있어야 함. 컨테이너는 `host.docker.internal` 로 도달.
[^hooks]: `~/.claude/settings.json` 에 직접 등록 — 아래 [자가증강 루프](#자가증강-루프) 참고.
[^agent]: `hermes-agent` 는 레포 미포함 서드파티 이미지(Nous Hermes Agent). 별도 빌드 후 `docker compose --profile agent up -d`.

> 코어는 **2·3번 + 호스트 1번**. 4번(훅)을 붙이면 자동 축적이 돌고, 5번은 순수 옵션.

---

## 사전 준비

| 깔 것 | 용도 | 확인 |
|---|---|---|
| **Docker** (Compose v2) | 컨테이너 스택 | `docker compose version` |
| **Ollama** | 로컬 임베딩·합성 | `ollama --version` · [ollama.com](https://ollama.com) 또는 `brew install ollama` |
| **Python 3** | 호스트 훅 실행 | `python3 --version` (macOS 기본 탑재) |
| 디스크 ~10GB | 모델 2개 | `gemma4:12b`(~8GB) + `bge-m3`(~1.2GB) — `make up`/`make models` 가 자동 pull |

> **클론 위치**: `~/oh-my-boring` 권장. 훅·`start.sh`·vault 경로가 이 위치를 기준으로 한다. 다른 곳에 두면 [훅 경로](#자가증강-루프)를 맞춰줘야 함.

---

## 빠른 시작

```bash
git clone git@github.com:jazz1x/oh-my-boring.git ~/oh-my-boring
cd ~/oh-my-boring
cp .env.example .env          # Slack 안 쓰면 그대로 둬도 됨 (코어는 .env 없이도 돈다)
make up                       # Ollama 확인 → 모델 pull → 빌드 → 기동 → 초기 sync
make smoke                    # end-to-end 한 번 확인
make ask Q="내가 도커 빌드 캐시 문제 어떻게 풀었지?"
```

`make up` = `start.sh`: Ollama 헬스체크 → 모델 pull → `docker compose up -d --build`(postgres+hermes-rs) → `/health` 대기. 첫 적재(startup sync)는 백그라운드로 수 분.

---

## 자가증강 루프

세션이 끝나면 알아서 쌓인다 — 핵심 가치. 호스트 훅 3종이 트리거고, **무거운 일(LLM 증류·스크럽·기록)은 엔진(`/distill`)이 SSOT 로 수행**한다.

```text
① 종료/중단  →  distill-session.py (SessionEnd/Stop 훅)
                 트랜스크립트 추출 → POST /distill → 엔진이 증류·시크릿 스크럽·vault/raw 기록
② sync       →  compile(raw→wiki 큐레이션) → 임베딩 → pgvector upsert → 그래프 추출
                 [4h 스케줄러 · make sync · 세션종료 직후 자동]
③ 회수       →  make ask / recall.py(프롬프트마다 자동 주입) / hermes-agent(Slack)
                 벡터 + BM25 RRF 로 top-K
```

| 훅 | Claude Code 이벤트 | 하는 일 |
|---|---|---|
| `hooks/distill-session.py` | `SessionEnd` / `Stop` | 세션 추출 → `/distill` POST → raw 노트 기록 + mtime 보정 |
| `hooks/recall.py` | `UserPromptSubmit` | 프롬프트 관련 과거 경험을 `/search` 로 회수해 컨텍스트 주입 |
| `hooks/collect-sessions.py` | 크론 / `make collect` | SessionEnd 놓친 과거 세션 백필(1회 소량씩) |

**훅 설치**(영속) — `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionEnd": [
      { "type": "command", "command": "python3 ~/oh-my-boring/hooks/distill-session.py", "timeout": 130, "async": true }
    ],
    "UserPromptSubmit": [
      { "type": "command", "command": "python3 ~/oh-my-boring/hooks/recall.py", "timeout": 10 }
    ]
  }
}
```

> 엔진(`hermes-rs`)이 떠 있어야 distill/recall 이 동작한다. 안 떠 있으면 조용히 no-op — **세션을 절대 막지 않음**.

---

## 소스 & 회수

- **흡수 대상**(`HERMES_SOURCE_DIRS`, compose 기본): `~/.claude/projects`(Claude Code 메모리) + `vault/wiki`(증류·큐레이션 노트).
- **즉시 기록**: `make remember M="bge-m3 임베딩은 1024차원"` → raw 기록 후 sync.
- **회수**: `make ask Q="..."` (1회 질의) · `recall.py`(프롬프트마다 자동) · Slack(`hermes-agent` 켰을 때).

---

## 회사/개인 태깅 (옵션 · 기본 꺼짐)

특정 경로 문서를 `origin=company` 로 태깅하고 ingest 에서 제외하려면 `.env` 에 토큰만:

```bash
HERMES_COMPANY_SUBSTR=acme:acme-kb    # Rust ingest/origin/audit (경로 substring)
DISTILL_COMPANY_CWD=acme              # 세션 증류 훅 (cwd substring)
```

**코드 수정 0 — env만.** 비우면 회사 개념 자체가 꺼지고 전부 `personal`.

---

## 명령 레퍼런스

`make help` 로 전체. 자주 쓰는 것:

| 명령 | 설명 |
|---|---|
| `make up` | 셋업+기동 (Ollama 확인·모델 pull·빌드·기동) |
| `make ask Q="질문"` | 질의 1회 (회수 + LLM 합성 + 출처) |
| `make sync` | 수동 적재 (compile → ingest → extract) |
| `make remember M="내용"` | 한 줄 메모 즉시 기록 + 적재 |
| `make collect [N=3]` | 과거 세션 백필 (1회 N개) |
| `make smoke` | end-to-end 스모크 테스트 |
| `make logs` | hermes-rs 엔진 로그 |
| `make psql` | Postgres 직접 접속 (그래프 들여다보기) |
| `make guard` | 구조 게이트 (fmt + clippy + test) — CI 와 동일 |
| `make down` | 정지 (데이터 `./data` 유지) |
| `make reset` | ⚠️ Postgres 데이터까지 초기화 (소스에서 재적재) |

---

## 설정 (env)

코어는 `.env` 없이 돈다. 기본값은 `docker-compose.yml` 의 `hermes-rs` 환경에 박혀 있다.

| 변수 | 기본 | 용도 |
|---|---|---|
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | — | `hermes-agent`(Slack) 켤 때만 |
| `HERMES_LLM_MODEL` | `gemma4:12b` | 합성 모델 (think=false 고정) |
| `HERMES_EMBED_MODEL` | `bge-m3` | 임베딩 (1024d) |
| `HERMES_SOURCE_DIRS` | `~/.claude/projects:vault/wiki` | 흡수 소스(`:` 구분) |
| `HERMES_SYNC_HOURS` | `4` | 백그라운드 sync 주기 |
| `HERMES_COMPANY_SUBSTR` / `DISTILL_COMPANY_CWD` | — | 회사 태깅(위 참고) |

---

## 개발 · 가드레일

- **SSOT 문서**: `hermes-rs/{PHILOSOPHY,RUST-STYLE,ENFORCEMENT}.md`.
- **원칙**: ROP(Result 레일) · Parse-don't-validate · Clean Architecture · 단순함 우선.
- **게이트**(로컬 `make guard` == CI): `rustfmt --check` + `clippy -D warnings`(`unsafe` forbid + `all`/`pedantic` deny) + `cargo test`. 테스트는 스택-프리(DB 불필요).
- **CI**(`.github/workflows/ci.yml`): PR·main push 마다 `rust-gate`(guard.sh) + `gitleaks`(시크릿 스캔). main 브랜치 보호가 둘 다 필수 — admin도 우회 불가, 직접 push·force-push·삭제 금지.

---

## 디렉터리

```text
oh-my-boring/
├─ hermes-rs/          # Rust 엔진 (ingest·retrieve·graph·compile·distill·serve)
│  └─ src/{ingest,retrieve,extract,graph,vault,distill,serve,store,ollama,...}.rs
├─ hooks/              # 호스트 훅 (distill-session · recall · collect-sessions)
├─ scripts/            # guard.sh(게이트) · smoke.sh · eval-gate.sh
├─ vault/              # raw(증류) → compile → wiki(큐레이션). ingest 흡수 대상
├─ data/              # Postgres 영속(pgdata) — gitignore
├─ docker-compose.yml  # postgres + hermes-rs (+ --profile agent: hermes-agent)
├─ start.sh            # make up 실체 (Ollama·모델·빌드·헬스)
└─ Makefile            # 명령 진입점
```
