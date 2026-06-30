#!/usr/bin/env python3
"""Network-free tests for memory-ingest workflow event vocabulary.

Run: python3 agents/shared/test_workflow_contract.py
"""
import re
from pathlib import Path

import workflow_contract as workflow


def test_python_vocabulary_matches_rust_workflow_contract():
    source = _rust_workflow_source()
    assert set(workflow.NODES) == _rust_as_str_values(source, "WorkflowNode")
    assert set(workflow.OUTCOMES) == _rust_as_str_values(source, "WorkflowOutcome")
    assert f'name: "{workflow.WORKFLOW_MEMORY_INGEST}"' in source


def test_resolution_fields_follow_memory_ingest_graph():
    assert workflow.resolution_fields("pass", "remembered") == {
        "workflow": "memory_ingest",
        "workflow_node": "remember_requested",
        "workflow_outcome": "pass",
    }
    assert workflow.resolution_fields("pass", "duplicate")["workflow_outcome"] == "duplicate"
    assert workflow.resolution_fields("failed", "not_called") == {
        "workflow": "memory_ingest",
        "workflow_node": "resolution_repaired",
        "workflow_outcome": "fail",
    }
    assert workflow.skip_fields() == {
        "workflow": "memory_ingest",
        "workflow_node": "skipped",
        "workflow_outcome": "skip",
    }


def test_readiness_fields_project_terminal_state():
    assert workflow.readiness_fields(True) == {
        "workflow": "memory_ingest",
        "workflow_node": "readiness_projected",
        "workflow_outcome": "pass",
    }
    assert workflow.readiness_fields(False)["workflow_outcome"] == "fail"


def test_collector_run_fields_cover_empty_success_and_failure():
    assert workflow.collector_run_fields("ok", 0)["workflow_node"] == "readiness_projected"
    assert workflow.collector_run_fields("ok", 1)["workflow_node"] == "done_marked"
    assert workflow.collector_run_fields("failed", 1)["workflow_node"] == "retry_marked"


def test_worker_fields_cover_offer_and_reconcile_events():
    assert workflow.worker_fields("ingest_offer", "pending")["workflow_node"] == "transcript_prepared"
    assert workflow.worker_fields("ingest_offer", "skipped")["workflow_node"] == "skipped"
    assert workflow.worker_fields("ingest_reconcile", "ok")["workflow_node"] == "done_marked"
    assert workflow.worker_fields("ingest_reconcile", "failed")["workflow_node"] == "retry_marked"


def test_unknown_workflow_projection_is_rejected():
    _assert_raises_value_error(lambda: workflow.resolution_fields("pass", "not_called"))
    _assert_raises_value_error(lambda: workflow.collector_run_fields("weird", 1))
    _assert_raises_value_error(lambda: workflow.worker_fields("ingest_offer", "weird"))


def _rust_workflow_source() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return (repo_root / "drudge" / "src" / "workflow.rs").read_text(encoding="utf-8")


def _rust_as_str_values(source: str, enum_name: str) -> set[str]:
    match = re.search(
        rf"impl {enum_name} \{{.*?pub const fn as_str.*?match self \{{(?P<body>.*?)\n        \}}\n    \}}",
        source,
        re.DOTALL,
    )
    assert match is not None, f"{enum_name}.as_str not found"
    return set(re.findall(r'=> "([^"]+)"', match.group("body")))


def _assert_raises_value_error(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


if __name__ == "__main__":
    test_python_vocabulary_matches_rust_workflow_contract()
    test_resolution_fields_follow_memory_ingest_graph()
    test_readiness_fields_project_terminal_state()
    test_collector_run_fields_cover_empty_success_and_failure()
    test_worker_fields_cover_offer_and_reconcile_events()
    test_unknown_workflow_projection_is_rejected()
    print("ok - workflow contract")
