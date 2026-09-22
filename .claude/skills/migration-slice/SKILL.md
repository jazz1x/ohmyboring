---
name: migration-slice
description: drudge → Python 스트랭글러 이전의 슬라이스 하나를 도는 절차와 코드 규율. 이전 트렁크(feat/migration-python)에서 문(door)·LangChain·LangGraph·Deep Agents 슬라이스를 계약→구현→독립검증→push 로 돌릴 때, 또는 그 산출물을 리뷰할 때 쓴다. Triggers: "슬라이스", "사이클", "루프 걸어", "migration-slice", "이전 계약서", "door", "P1", "P2", "판단 노드".
---

# migration-slice

트렁크 `feat/migration-python` (draft PR #396) 위에서 슬라이스 하나 = 계약 1개 → 커밋 1개.

## 시작 전 — 이 둘을 못 하면 시작하지 않는다
- 설계·계약·슬라이스를 **쓰기 직전** `ls -t ~/Documents/ohmyboring/*/*.html | head` 로 소유자의 최신 문서를 열고 그 위에 얹는다. 순서·어휘는 그 문서 것을 쓴다(각서 「기억이 되묻는 법」 `feedback-loop/design.html`, 볼트 사본 wiki-1736).
- **다음 사이클은 고르지 않고 집는다.** 각서 6장 그래프(C12 넘겨받기 → C13 두 칸 → C15 자가수리 · C12 → C14 채점 줄 · ◇C16 권한 넘김 · ◇C17 hermes 제거, 볼트 wiki-1780)에서 선행 노드가 끝난 것을 집는다. ◇ 는 사이클이 아니라 소유자에게 숫자를 들고 가는 자리 — 거기서 멈춘다. 그래프를 바꿔야 하면 각서 6장을 먼저 고치고(첫 주 뒤 손질은 예정된 것) 계약은 그 뒤.
- dispatch **직전** 한 줄: 「이 사이클이 끝나면 소유자가 쓰는 것은 ___ 이다」. 발판·게이트·도구만 나오는 사이클은 시작하지 않는다(2026-09-21 문 트랙 4사이클 = 기능 0).
- 그 한 줄은 **소유자 손에 닿는 곳**을 가리켜야 한다 — 슬랙에 온 카드, 크론이 낸 산출물, 훅이 주입한 줄. 「브랜치에 있다」는 닿은 것이 아니다(사이클 1~11: 초록 16커밋, 건넨 것 0 — 재고는 단계형 개발의 산물이다).

## 절차
1. 계약은 `data/loop/cycle-N.yaml` (gitignore, 레포 밖 취급). 형식·필수 필드는 `~/.claude/orca-model-routing.md` §1 Contract Map. `baseline.sha` = 트렁크 HEAD, `given` 에 워커가 뒤질 필요 없는 사실을 심볼 단위로 — **한 줄마다 그 주장을 만든 명령 출력이 같은 턴에 있어야 적는다**(사이클 2·3 의 given 이 틀려 워커가 30분씩 헤맸다).
2. preflight: `origin/feat/migration-python` 이 baseline 과 같아야 dispatch. 아니면 PARK.
3. 구현: `kimi -m kimi-code/kimi-for-coding -p "$(cat 계약)"` 헤드리스. 커밋 정확히 1개, amend·push 금지.
4. 검증: 계약을 쓴 세션이 아닌 독립 컨텍스트(Opus). 입력은 계약·diff·구현 보고·게이트 출력만. 변이는 사본(`cp`)에서, `__pycache__` 비우고. 판정 필드 `verdict / contract_defects / edge_necessity / mutants_run`.
5. PASS 면 오케스트레이터가 push → #396 CI(계약 파리티 + eval-gate). 빨강이면 게이트가 맞는지 먼저 본다(사이클 1: 게이트가 맞았고 계약이 틀렸다).
6. **배달까지가 사이클이다.** 계약의 마지막 work_item 은 언제나 소비자 쪽 설치·기동(`agent_wiring.py --install`, 크론 등록, `make build`+재기동)과 **그 소비자가 낸 산출물 인용**(카드 ts, 크론 output 파일, 훅 주입 줄). `~/.hermes/scripts`·크론·호스트 바이너리는 레포와 별도 아티팩트라 머지가 곧 배달이 아니다. 인용이 없으면 사이클은 열려 있다.
7. 카드 뜻은 각서 6장이 정본이다 — 「해」는 승인(학습 신호 아님), 제안은 실행할 것/짚어 둔 것 두 칸, 「미뤄」는 실행 칸에만·순위에 안 들어감, 제안은 문제지로 기록. 계약에 옮겨 적지 말고 가리킨다.
8. ◇C16·◇C17 앞에서는 멈추고 숫자를 들고 소유자에게 간다.

## 게이트 (있는 것만 쓴다, 새로 세우지 않는다)
- `make guard` — fmt/clippy/test + py-compile + Python 단위 + changelog·deps 게이트 + **ruff check / format --check**
- `DRUDGE_URL=http://127.0.0.1:<door> python3 scripts/contract-parity.py --check` — 24 tools · 24 routes · GET 4 shapes
- CI eval-gate — recall@3 15/15 + 계약 파리티
- 새 게이트가 떠오르면 먼저 "스킬 한 줄로 되나". 도구는 같은 부류 실수가 반복 관측된 뒤에만.

## compose 규칙
- `up`/`build` 전량은 정본 체크아웃(/Users/jongyun/Development/mine/oh-my-boring)에서만 친다. 워크트리에서는 `make door-build`·`make door-up`(--no-deps) 만.
- 엔진 이미지·컨테이너(boring-drudge/boring-postgres/boring-agent)는 워크트리에서 절대 건드리지 않는다. compose 는 `name: oh-my-boring` 고정이라 어느 디렉터리에서 쳐도 같은 프로젝트다. 문만 다룰 때도 언제나 `--no-deps` 로 — depends_on 수렴이 엔진을 재생성했다(사이클 4 사고).

## 코드 규율
- 주석은 드물게. 0 이 아니라 과다 경계 — 「왜」는 커밋 본문·PR 에. 이름과 구조로 말하고, 주석은 코드가 말 못 하는 것(외부 제약·좌표)만.
- ROP: 조용한 폴백·삼킨 예외·방어적 타임아웃·catch-all 금지. 업스트림 불통은 502 JSON 처럼 **보이는 실패**로.
- 훅(`hooks/*.py`)은 stdlib-only 유지. 새 의존은 `agents/` 서비스에만, `requirements.txt` 와 import 가 같은 커밋에.
- 재시도·폴백은 LangChain 래퍼(`with_retry`/`with_fallbacks`)가 아니라 LangGraph 엣지로.
- 이중 구현 금지: config·frontmatter·LLM 클라이언트는 Python 한 곳으로 접는다(P5 까지 0).
- 시험은 스텁을 테스트 안에서 띄우고 내린다. 라이브 스택(:7700)을 내리지 않는다.

## 좌표
- 계획: 각서 6장 `~/Documents/ohmyboring/feedback-loop/design.html` (볼트 wiki-1780) · 계약 스냅샷 `data/contract/engine-contract.json`
- 전례: 사이클 1 = `agents/door/` (8cb3b17), 계약 `data/loop/cycle-1.yaml`
