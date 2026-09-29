"""MCP 회상 보강 — 문(door)이 엔진의 recall 답 앞에 번호 노트 블록을 붙이는 순수 함수.

엔진 recall 은 검색이라 번호를 못 푼다(wiki-2226 를 물으면 그 번호를 언급한 2227 을 준다 —
본문에 자기 번호가 없어서). 문은 recall 요청의 query 에서 wiki-NNNN 을 뽑아(ohmyboring.recall.
named) 블록을 만들어(호출자가 주는 block_for — 볼트 읽기·머리말 쪼개기·노트 블록을 묶은 것)
엔진이 준 본문 앞에 붙여 돌려준다. 엔진·drudge 는 그대로 — 문이 앞에 붙일 뿐.

순수 함수: HTTP·볼트·로그를 모르고 dict 만 다룬다. 못 붙이는 경우(번호 없음·recall 아닌
도구·isError)는 입력 response 를 그대로(같은 객체) 돌려준다 — 통제군은 바이트 그대로
지나가야 하니까.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...recall.named import named_ids

#: 번호 하나를 받아 노트 블록 한 장을 돌려주는 호출자 쪽 묶음 — 문이면
#: vault.read_note + vault_note.split_frontmatter + named.note_block, hermes 훅이면
#: 볼트 디렉터리를 둘러싼 같은 세 조각이다.
BlockFor = Callable[[str], str]


def _recall_query(request: Any) -> str | None:
    """tools/call recall 의 query 문자열, 아니면 None."""
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict) or params.get("name") != "recall":
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict) or not isinstance(arguments.get("query"), str):
        return None
    return arguments["query"]


def _text_content(response: Any) -> list | None:
    """isError 가 거짓인 결과의 content 목록 — 첫 항이 text 를 품은 dict 일 때만."""
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return None
    content = result.get("content")
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, dict) or not isinstance(first.get("text"), str):
        return None
    return content


def augment(request: dict, response: dict, block_for: BlockFor) -> dict:
    """request 가 recall tools/call 이고 response 가 성공한 텍스트 결과며 query 에 번호가 있을
    때만 — 번호마다 block_for 으로 만든 블록을 「\\n\\n」로 이어 content[0].text 앞에 붙인
    새 dict. 그 밖엔 입력 response 를 그대로(같은 객체) 돌려준다."""
    query = _recall_query(request)
    content = _text_content(response)
    if query is None or content is None:
        return response
    ids = named_ids(query)
    if not ids:
        return response
    first = content[0]
    prefix = "\n\n".join(block_for(note_id) for note_id in ids)
    new_first = {**first, "text": f"{prefix}\n\n{first['text']}"}
    new_response = dict(response)
    new_response["result"] = {**response["result"], "content": [new_first, *content[1:]]}
    return new_response
