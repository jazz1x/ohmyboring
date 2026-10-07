"""사건 쓰기·읽기 — drudge 엔진의 /events 와 MCP events 를 문(파이썬)이 답하는 조각 (E4-6a).

parse.py 는 http.rs event_batch·EventLogReq·validate_event_since_hours 와 mcp.rs mcp_events 의
인자 규약을, pg.py 는 store.rs log_event·recent_events 의 칸 대열과 SQL 을, run.py 는 핸들러의
순서(저장소 확인 → 검증 → 트랜잭션)를 그대로 옮긴다. 문(door)만 이 패키지를 부른다.
"""

from ohmyboring.events import parse, pg, run  # noqa: F401
