"""backend=surreal 에서 pg 를 직접 읽거나 쓰는 파이썬 길을 거부하는 문장 한 곳. 문 기동(door.py)과 brief_once 가
둘 다 여기서 부른다 — brief_once 가 door.py 를 import 하면 그 모듈 수준 env 파싱·스레드 풀·FastAPI 앱까지 끌려오므로
(door.py:188·191·192·296) 이 모듈은 설정 읽기 하나에만 기댄다."""

from __future__ import annotations

from ohmyboring import config as boring_config


def pg_path_refusal(python_on: list[str]) -> str | None:
    """backend=surreal 이고 python_on 이 비지 않으면 pg 를 직접 읽거나 쓰는 파이썬 길을 이름 짓는 거부 문장을, 아니면
    None."""
    backend = (boring_config.load().get("vector") or {}).get("backend")
    if backend != "surreal" or not python_on:
        return None
    return (
        f"[door] refusing to start: boring.json vector.backend=surreal but {', '.join(python_on)} reads or writes "
        "pg directly (document/chunk/node/edge/claim writes, edge/project/brief reads), which Surreal never sees. "
        "Set the DOOR_* switches to engine, or roll back as a pair (vector.backend=pgvector + DOOR_*=python)."
    )
