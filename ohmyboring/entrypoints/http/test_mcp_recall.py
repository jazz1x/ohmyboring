#!/usr/bin/env python3
"""MCP 회상 보강 — augment 가 번호 있는 recall 에만 블록을 앞에 붙이고 나머지는 그대로 지나치는가 (WI-2).

Run: python3 ohmyboring/entrypoints/http/test_mcp_recall.py   (no pytest dependency, stdlib only)

왜 있는가. 엔진 recall 은 검색이라 번호를 못 푼다(wiki-2226 를 물으면 그 번호를 언급한
wiki-2227 을 준다). 문(door)이 recall 응답의 content[0].text 앞에 번호 노트 블록을 붙이는
것 — 이 시험은 그 붙이기의 문을 못 박는다. 통제군(번호 없는 recall·recall 아닌 도구·
isError 응답)은 입력 response 객체를 그대로(identity) 돌려받아 바이트 그대로 지나가게 한다.

Mutation targets: augment 가 isError 도 붙이게 하면 isError 시험이 빨개진다; 번호 없음도
붙이게 하면 통제군 시험이 빨개진다; 새 dict 대신 입력을 고치면 입력-불변 시험이 빨개진다.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# String form on purpose: scripts/test_python_deps.py matches literal import statements
# against requirements.txt, and ohmyboring is this repo's own package, not a distribution.
mcp_recall = importlib.import_module("ohmyboring.entrypoints.http.mcp_recall")

RECALL_REQUEST = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "recall", "arguments": {"query": "wiki-2226 봐"}},
}

RECALL_RESPONSE = {
    "id": 7,
    "jsonrpc": "2.0",
    "result": {
        "content": [{"type": "text", "text": "- [wiki-2229.md] 회상 본문"}],
        "isError": False,
    },
}


def _block_for(note_id: str) -> str:
    return f"- 노트 {note_id} — 더미" if note_id != "wiki-9999" else f"- {note_id} 은 볼트에 없음"


def test_a_numbered_recall_gets_the_blocks_prepended():
    out = mcp_recall.augment(RECALL_REQUEST, RECALL_RESPONSE, _block_for)
    assert out is not RECALL_RESPONSE, "붙인 답은 새 dict 여야 한다"
    text = out["result"]["content"][0]["text"]
    assert text.startswith("- 노트 wiki-2226 — 더미\n\n- [wiki-2229.md] 회상 본문")
    # 나머지 자리는 엔진 답 그대로
    assert out["result"]["isError"] is False
    assert out["id"] == 7
    # 입력은 건드리지 않는다
    assert RECALL_RESPONSE["result"]["content"][0]["text"] == "- [wiki-2229.md] 회상 본문"


def test_a_missing_number_is_said_and_the_engine_answer_follows():
    request = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "recall", "arguments": {"query": "wiki-9999 어디 있지"}},
    }
    out = mcp_recall.augment(request, RECALL_RESPONSE, _block_for)
    assert out["result"]["content"][0]["text"].startswith(
        "- wiki-9999 은 볼트에 없음\n\n- [wiki-2229.md] 회상 본문"
    )


def test_several_numbers_join_with_blank_lines_in_order():
    request = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "recall", "arguments": {"query": "wiki-3000 과 wiki-1000"}},
    }
    out = mcp_recall.augment(request, RECALL_RESPONSE, _block_for)
    assert out["result"]["content"][0]["text"].startswith(
        "- 노트 wiki-3000 — 더미\n\n- 노트 wiki-1000 — 더미\n\n- [wiki-2229.md] 회상 본문"
    )


def test_a_recall_without_numbers_passes_through_unchanged():
    request = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "recall", "arguments": {"query": "그냥 검색"}},
    }
    assert mcp_recall.augment(request, RECALL_RESPONSE, _block_for) is RECALL_RESPONSE


def test_a_non_recall_tool_passes_through_unchanged():
    request = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "wiki-2226"}},
    }
    assert mcp_recall.augment(request, RECALL_RESPONSE, _block_for) is RECALL_RESPONSE


def test_an_error_response_passes_through_unchanged():
    response = {
        "id": 7,
        "jsonrpc": "2.0",
        "result": {
            "content": [{"type": "text", "text": "-32000: boom"}],
            "isError": True,
        },
    }
    assert mcp_recall.augment(RECALL_REQUEST, response, _block_for) is response


def test_a_malformed_pair_passes_through_unchanged():
    for request, response in (
        ({"method": "initialize"}, RECALL_RESPONSE),
        (RECALL_REQUEST, {"result": {}}),
        (RECALL_REQUEST, {"result": {"content": [{"type": "image"}], "isError": False}}),
        ({"method": "tools/call", "params": {"name": "recall"}}, RECALL_RESPONSE),
    ):
        assert mcp_recall.augment(request, response, _block_for) is response, (request, response)


if __name__ == "__main__":
    test_a_numbered_recall_gets_the_blocks_prepended()
    test_a_missing_number_is_said_and_the_engine_answer_follows()
    test_several_numbers_join_with_blank_lines_in_order()
    test_a_recall_without_numbers_passes_through_unchanged()
    test_a_non_recall_tool_passes_through_unchanged()
    test_an_error_response_passes_through_unchanged()
    test_a_malformed_pair_passes_through_unchanged()
    print("ok - mcp recall augment: 번호 있는 recall 만 블록 앞에, 통제군 셋은 같은 객체 그대로")
