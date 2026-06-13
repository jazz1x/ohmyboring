# 세션 노트 — 2026-01-01
> 자동 증류 (Claude Code · 종료) · origin: personal · repo: jazz1x/oh-my-boring · cwd: ~/oh-my-boring

> **이건 예시 노트다.** `vault/raw/` 에 쌓이는 증류 노트의 포맷을 보여주고, fresh clone 에서
> `make ask` 가 답할 거리를 1개 시드한다. 실제 노트는 SessionEnd 훅이 자동으로 만든다.
> 지워도 무방 — 다음 sync 때 wiki 에서 함께 사라진다.

🎯 **풀던 문제** — oh-my-boring 를 처음 띄우고 동작을 확인하려 했다.

🧪 **시도/실패** — `docker compose up` 만 했더니 hermes-agent(Slack 비서)까지 떠서 실패.
그 이미지는 레포에 없다(외부 Nous Hermes Agent).

✅ **통한 해결** — 코어는 `make up` 으로만 띄운다(postgres + drudge). Slack 비서는 옵션이라
`docker compose --profile agent up -d` 로 따로 켠다. 호스트 Ollama(`ollama serve`)가 먼저 떠 있어야
컨테이너가 `host.docker.internal` 로 임베딩·합성을 호출한다.

🔄 **미완/다음** — 세션을 더 쌓아 `make ask` 회수 품질을 본다. `~/.claude/settings.json` 에
SessionEnd/UserPromptSubmit 훅을 등록하면 이후로는 자동 축적된다.
