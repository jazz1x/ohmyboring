---
name: migration-slice
description: drudge → Python 스트랭글러 이전의 슬라이스 하나를 도는 절차와 코드 규율. 이전 트렁크(feat/migration-python)에서 문(door)·LangChain·LangGraph·Deep Agents 슬라이스를 계약→구현→독립검증→push 로 돌릴 때, 또는 그 산출물을 리뷰할 때 쓴다. Triggers: "슬라이스", "사이클", "루프 걸어", "migration-slice", "이전 계약서", "door", "P1", "P2", "판단 노드".
---

# migration-slice

트렁크 `feat/migration-python` (draft PR #396) 위에서 슬라이스 하나 = 계약 1개 → 커밋 1개.

## 절차
1. 계약은 `data/loop/cycle-N.yaml` (gitignore, 레포 밖 취급). 형식·필수 필드는 `~/.claude/orca-model-routing.md` §1 Contract Map. `baseline.sha` = 트렁크 HEAD, `given` 에 워커가 뒤질 필요 없는 사실을 심볼 단위로.
2. preflight: `origin/feat/migration-python` 이 baseline 과 같아야 dispatch. 아니면 PARK.
3. 구현: `kimi -m kimi-code/kimi-for-coding -p "$(cat 계약)"` 헤드리스. 커밋 정확히 1개, amend·push 금지.
4. 검증: 계약을 쓴 세션이 아닌 독립 컨텍스트(Opus). 입력은 계약·diff·구현 보고·게이트 출력만. 변이는 사본(`cp`)에서, `__pycache__` 비우고. 판정 필드 `verdict / contract_defects / edge_necessity / mutants_run`.
5. PASS 면 오케스트레이터가 push → #396 CI(계약 파리티 + eval-gate). 빨강이면 게이트가 맞는지 먼저 본다(사이클 1: 게이트가 맞았고 계약이 틀렸다).
6. P3(저장·검색)·P4(Deep Agents) 앞에서는 멈추고 소유자 확인.

## 게이트 (있는 것만 쓴다, 새로 세우지 않는다)
- `make guard` — fmt/clippy/test + py-compile + Python 단위 + changelog·deps 게이트 + **ruff check / format --check**
- `DRUDGE_URL=http://127.0.0.1:<door> python3 scripts/contract-parity.py --check` — 24 tools · 24 routes · GET 4 shapes
- CI eval-gate — recall@3 15/15 + 계약 파리티
- 새 게이트가 떠오르면 먼저 "스킬 한 줄로 되나". 도구는 같은 부류 실수가 반복 관측된 뒤에만.

## 코드 규율
- 주석은 드물게. 0 이 아니라 과다 경계 — 「왜」는 커밋 본문·PR 에. 이름과 구조로 말하고, 주석은 코드가 말 못 하는 것(외부 제약·좌표)만.
- ROP: 조용한 폴백·삼킨 예외·방어적 타임아웃·catch-all 금지. 업스트림 불통은 502 JSON 처럼 **보이는 실패**로.
- 훅(`hooks/*.py`)은 stdlib-only 유지. 새 의존은 `agents/` 서비스에만, `requirements.txt` 와 import 가 같은 커밋에.
- 재시도·폴백은 LangChain 래퍼(`with_retry`/`with_fallbacks`)가 아니라 LangGraph 엣지로.
- 이중 구현 금지: config·frontmatter·LLM 클라이언트는 Python 한 곳으로 접는다(P5 까지 0).
- 시험은 스텁을 테스트 안에서 띄우고 내린다. 라이브 스택(:7700)을 내리지 않는다.

## 좌표
- 계획: 세션 scratchpad `paper/migration-plan-v2.html` (레포 밖) · 계약 스냅샷 `data/contract/engine-contract.json`
- 전례: 사이클 1 = `agents/door/` (8cb3b17), 계약 `data/loop/cycle-1.yaml`
