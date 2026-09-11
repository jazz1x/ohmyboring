# ohmyboring

[English](README.md) · **한국어** · [日本語](README.ja.md)

ohmyboring 은 코딩 에이전트 세션(Claude Code, Kimi Code, Codex)을 로컬 마크다운 위키로 만들고, 전에 풀었던 것을 지금 다시 풀려는 프롬프트에 넣어 준다. 어떻게 풀었는지를 담아 두는 것이고, 회수 정밀도는 공개적으로 재는 중이다(§ 상태). 전부 내 기계의 로컬 LLM 으로 돈다 — 클라우드도 토큰도 없다.

## Quick start

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/jazz1x/ohmyboring/main/install.sh)"
```

한 줄 설치는 `~/oh-my-boring` 에 받아 빌드하고 훅·MCP 등록·워커까지 연결한다. 단계별로는:

```bash
git clone https://github.com/jazz1x/ohmyboring.git ~/oh-my-boring
cd ~/oh-my-boring
make up             # 엔진 시작 (llm.provider 가 ollama 면 Ollama 를 띄우고 모델을 받는다)
make verify-llm     # 제공자 접속, 모델 id 둘 존재, 임베딩 차원 일치
make doctor         # 스택·훅·워커·최신 노트 — 발견한 것은 전부 찍고 숨기지 않는다
make collect N=20   # 과거 Claude Code 세션으로 vault 를 채운다; 갓 받은 저장소는 비어 있다
make ask Q="how did I fix the docker build cache problem?"
```

`make up` 이 0 으로 끝나고 `http://127.0.0.1:7700/health` 가 200 을 주고, `make verify-llm` 이 `configuration looks consistent` 를 찍고, `make doctor` 에 `✗` 가 없으면 끝이다.

필요한 것: Docker, Python 3, jq, curl, git, make, 그리고 로컬 LLM 서버 — Ollama(기본, `make up` 이 띄운다) 또는 LM Studio(서버를 켜고 챗 모델·임베딩 모델을 하나씩 올린 뒤 `llm.provider` 를 `lmstudio` 로, `make verify-llm`). 다른 OpenAI 호환 `/v1` 엔드포인트는 `llm.provider: openai-compatible` 로 쓴다.

## What it does

세션이 끝나면 `SessionEnd` 훅이 로컬 LLM 으로 대화록을 증류해 `vault/wiki/` 에 노트 하나로 만들고 엔진의 `remember` 문으로 저장한다. 프롬프트를 치면 `UserPromptSubmit` 훅이 vault 를 검색해 과거 노트를 최대 셋 — 각각 개념을 공유하는 옛 노트 하나와 함께 — 울타리 뒤에 넣는다. 울타리는 에이전트에게 할 일을 말한다: 맞으면 재사용하고, 코드와 어긋나면 어긋난다고 말하고, 안 맞으면 언급하지 말라. 다음 세션이 끝날 때 엔진은 무슨 일이 있었는지 배운다 — 재사용된 노트와 반박된 노트가 간선이 되고, 다음 주입은 노트마다 `reused n×` / `contested n×` 를 달고 그 순서로 나온다.

Codex 는 세션 훅이 없어 호스트 워커가 20분마다 적격 대화록을 가져간다. `make collect` 는 과거 Claude Code 세션을, `make collect-kimi` 는 과거 Kimi 세션을 채우고, `make distill-now` 는 세션을 끝내지 않고 지금 것을 담으며, `make remember M="…"` 는 직접 쓴 노트를 저장한다.

vault 가 원본이다: [Obsidian](https://obsidian.md) 으로 그대로 여는 평문 마크다운(태그와 `[[wiki-NNNN]]` 링크가 이미 있다). `BORING_VECTOR=on` 이면 엔진이 pgvector 인덱스와 그래프(노트·개념·도구·주장·세션)를 두고 `make sync` 로 vault 에서 다시 만든다; 없으면 회수가 마크다운을 직접 읽는다. `make peek` 는 무엇이 세션에 주입됐고 쓰였는지 보여 주는 읽기 전용 로컬 페이지(`127.0.0.1:7788`)를 연다.

## Configuration

정책은 `boring.json` 에 있다. `make up` 이 `boring.example.json` 에서 만들고 `boring.schema.json` 으로 검사한다. 손대게 되는 키:

| 키 | 뜻 | 읽는 곳 |
|---|---|---|
| `llm.provider` | `ollama`(모델을 받는다) · `lmstudio`(앱에서 올린다) · `openai-compatible` | `scripts/llm-providers/<provider>.sh`, `agents/shared/omb_env.py` |
| `llm.base_url` · `llm.model` | OpenAI 호환 `/v1` 엔드포인트와 증류·`ask` 에 쓰는 챗 모델 | 같음 |
| `llm.embed_model` · `llm.embed_dim` | 임베딩 모델과 벡터 크기 — 모델을 바꾸면 dim 을 맞추고 `make reset` | 같음; 엔진이 시작할 때 dim 을 검사한다 |
| `note_lang` | `auto` · `ko` · `en` — 노트를 쓰는 언어 | `agents/shared/boring_config.py` |
| `repos[]` | 경로/원격 규칙 → `origin`(`personal` / `company` / `mirror` / `community`); 회사 출처 산문은 엔진 밖으로 안 나간다 | `agents/shared/boring_config.py` |
| `agents[]` | 어느 에이전트를 연결할지(훅·MCP·워커) | `agents/shared/agent_wiring.py` |

컨테이너 안에서는 LLM 을 `host.docker.internal` 로, 호스트에서는 `localhost` 로 부른다 — `boring.example.json` 은 컨테이너 형태로 되어 있다.

`.env` 에는 비밀과 런타임 덮어쓰기만 둔다. 코드가 읽는 `BORING_*` 변수는 전부 기본값과 함께 `.env.example` 에 있다; 둘이 다르면 코드가 이기고, 그 표를 여기 다시 적지 않는다. 실제로 손대는 것: `BORING_VECTOR=on`(pgvector + 그래프), `BORING_LLM_API_KEY`(제공자가 요구할 때), `BORING_EVENT_SINK=spool`(이벤트를 엔진 대신 로컬 파일에 — 시험과 탐침이 쓰는 것).

## Commands

`make help` 가 50개 대상을 한 줄씩 보여 준다. 매일 쓰는 것:

| 명령 | 하는 일 |
|---|---|
| `make up` / `make down` | 스택 시작 / 중지 |
| `make doctor` | 스택·훅·최신 노트·Codex 워커 진단; `make readiness` 는 발견 하나에도 실패하는 엄격판 |
| `make ask Q="…"` | 기억에서 출처와 함께 답 하나 |
| `make remember M="…"` | 지금 노트 저장 |
| `make collect [N=1]` · `make collect-kimi [N=1]` · `make distill-now` | 과거 세션 채우기 · 현재 세션 담기 |
| `make sync` | vault 에서 인덱스와 그래프 재구성(4시간마다도 돈다) |
| `make peek` · `make events [N=20]` · `make usage` | 주입·사용 현황 · 최근 워크플로 이벤트 · 로컬 대화록 기준 토큰 사용량 |
| `make guard` · `make quality` · `make eval` | 구조 게이트(fmt·clippy·테스트·Python) · 릴리스 계약 게이트 · 회수 회귀 게이트 |

엔진은 `http://localhost:7700/mcp` 에서 MCP 도 말한다. `install.sh` 가 Claude Code·Kimi·Cursor·Codex 에 등록하고, 저장소 루트의 `.mcp.json` 이 다른 클라이언트용 표준 항목이다.

사용 가능한 tools (22개): `recall`, `neighbors`, `claims`(기억 회수) · `code_search`, `code_symbol`, `code_index_status`(별도 AST 코드 코퍼스) · `ask`, `brief`, `weekly_brief`, `project_status`, `decisions`, `risks`, `next_actions`, `stalled`(생성형 — LLM 을 돌린다) · `context`, `corpus_status`, `events`, `config_get`(구조화 / 자기진단) · `remember`, `forget`, `classify_repo`, `sync`(쓰기 / 유지).

기본인 wiki 우선 모드(`BORING_VECTOR=off`)에서는 그래프·최근성 순서·이벤트 DB 가 필요한 도구가 `BORING_VECTOR=on` 으로 켜기 전까지 JSON-RPC `-32603` 을 돌려준다: `neighbors`, `claims`, `corpus_status`, `events`, `brief`, `weekly_brief`, `project_status`, `decisions`, `risks`, `next_actions`, `stalled`. 나머지는 없이도 돈다: `recall`, `ask`, `context`, `remember`, `forget`, `sync`, `config_get`, `classify_repo`, `code_index_status`, `code_search`, `code_symbol`(코드 도구 셋은 켜진 `code_index` 소스와 앞선 `code-sync` 가 필요).

## Status

아래 숫자는 전부 그것을 낸 명령이 있다.

- 회수 회귀 바닥: 18문서 픽스처 코퍼스에서 골든 질의 22/22, MRR 1.000(`make eval`). 배선 바닥이지 품질 주장이 아니다 — 18문서에는 근접 경쟁자가 없고 프로덕션에는 있다.
- 회수 정밀도: LLM 판정 관련 6 / 판정 24; 그 판정자를 교정할 사람 감사가 하한 30 미만이라 아직 정밀도 수치를 내지 않는다(`make peek`, `label_core.py`).
- 주입된 노트가 쓰이는가: 사전 등록된 측정(주입 노트의 프롬프트당 업테이크 대 같은 풀의 대조군, `docs/PRD.md` §2)이 `agents/shared/verdict_core.py` 에 등록된 창에서 돈다; `make peek` 가 어디까지 왔는지 보여 준다. 표본 하한(세션 20, 주입 프롬프트 200)을 채우기 전엔 판정을 인용하지 않는다.
- 임베딩: `bge-m3` 가 MacBook Pro M5 Pro 48 GB 로컬 Ollama 에서 텍스트당 평균 0.105 s(`make bench-embed`). RAM 등급별 증류 모델 쌍과 지연: `make bench-llm-tier TIER=16gb|32gb`; 결과는 `docs/reports/llm-pair-matrix.md`.
- 배달: `/health` 가 `build_sha` 를 보고하고, 엔진·호스트 CLI 바이너리·설치된 훅 스크립트가 체크아웃보다 뒤처지면 `make doctor` 가 실패한다.

## Non-goals

- 클라우드 없음, 팀 공유 기억 없음, 회사 지식 베이스 적재 없음 — vault 는 한 사람의 세션 경험이다.
- 인터랙티브 UI 제품 없음. `make peek` 는 읽기 전용 루프백 페이지이고 거기가 경계다.
- Windows 는 아직: `hooks/` 가 하위 호환용 심링크를 쓴다. macOS 와 Linux 에서 시험됐다.
