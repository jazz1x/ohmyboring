//! `POST /gap` and MCP `gap` against a throwaway Postgres (`BORING_TEST_DATABASE_URL`; skipped
//! without it). Expected answers and rows in `tests/fixtures/gap_python.json` were recorded from
//! the Python door (`_gap_python` → `record_gap`) on the same fixture, 2026-10-28.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use serde_json::{Value, json};

use super::split_tests::{call, dsn, raw_client, seeded_store, state_with};
use crate::serve::build_router;

const PYTHON: &str = include_str!("../../tests/fixtures/gap_python.json");

async fn fresh_router(dsn: &str) -> axum::Router {
    let store = seeded_store(dsn).await;
    raw_client(dsn)
        .await
        .batch_execute(
            "delete from node where kind in ('gap','session');
             delete from edge where kind in ('gap','gap_handed')",
        )
        .await
        .unwrap();
    build_router(state_with(Some(store), None))
}

#[tokio::test]
async fn gap_answers_and_writes_like_the_python_door() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let router = fresh_router(&dsn).await;
    let reference: Value = serde_json::from_str(PYTHON).unwrap();
    let mut wrong = Vec::new();
    let cases = reference["cases"].as_array().unwrap();
    for case in cases {
        let name = case[0].as_str().unwrap();
        let path = if case[1] == "http" { "/gap" } else { "/mcp" };
        let (status, body) = call(&router, "POST", path, &[], case[2].as_str().unwrap()).await;
        let want_status = case[3].as_u64().unwrap();
        let want = case[4].as_str().unwrap();
        // Every answer, HTTP and MCP, is compared as the door's bytes.
        if u64::from(status.as_u16()) != want_status || body != want.as_bytes() {
            wrong.push(format!(
                "{name}: got {status} {} want {want_status} {want}",
                String::from_utf8_lossy(&body)
            ));
        }
    }
    let db = raw_client(&dsn).await;
    let nodes: Vec<(String, String, Option<String>, String)> = db
        .query(
            "select id, kind, outcome, label from node where kind in ('gap','session') order by id",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| (r.get(0), r.get(1), r.get(2), r.get(3)))
        .collect();
    let want_nodes: Vec<(String, String, Option<String>, String)> = reference["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| {
            let kind = n[1].as_str().unwrap();
            let label = if kind == "session" {
                nodes
                    .iter()
                    .find(|got| got.0 == n[0].as_str().unwrap())
                    .map_or(String::new(), |got| got.3.clone())
            } else {
                n[3].as_str().unwrap().to_owned()
            };
            (
                n[0].as_str().unwrap().to_owned(),
                kind.to_owned(),
                n[2].as_str().map(str::to_owned),
                label,
            )
        })
        .collect();
    if nodes != want_nodes {
        wrong.push(format!("nodes: got {nodes:?} want {want_nodes:?}"));
    }
    let edges: Vec<Vec<String>> = db
        .query(
            "select src, dst, kind from edge where kind in ('gap','gap_handed') order by 1,2,3",
            &[],
        )
        .await
        .unwrap()
        .iter()
        .map(|r| vec![r.get(0), r.get(1), r.get(2)])
        .collect();
    let want_edges: Vec<Vec<String>> = serde_json::from_value(reference["edges"].clone()).unwrap();
    if edges != want_edges {
        wrong.push(format!("edges: got {edges:?} want {want_edges:?}"));
    }
    assert!(
        wrong.is_empty(),
        "{} differ (of {} answers + nodes + edges):\n{}",
        wrong.len(),
        cases.len(),
        wrong.join("\n")
    );
}

/// The session node's label is the time of the latest call; a repeat adds no edge.
#[tokio::test]
async fn gap_repeat_overwrites_the_session_label_and_adds_no_edge() {
    let Some(dsn) = dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let router = fresh_router(&dsn).await;
    let db = raw_client(&dsn).await;
    let body = r#"{"session_id":"rep","query":"q","kind":"missing"}"#;
    let label = || async {
        let row = db
            .query_one("select label from node where id = 'session:rep'", &[])
            .await
            .unwrap();
        row.get::<_, String>(0)
    };
    call(&router, "POST", "/gap", &[], body).await;
    let first = label().await;
    tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    call(&router, "POST", "/gap", &[], body).await;
    assert_ne!(first, label().await, "label is the latest observed_at");
    let edges: i64 = db
        .query_one("select count(*) from edge where src = 'session:rep'", &[])
        .await
        .unwrap()
        .get(0);
    assert_eq!(edges, 1);
}

#[tokio::test]
async fn tools_list_shows_gap_exactly_once_as_the_python_tool() {
    let router = build_router(state_with(None, None));
    let (_, body) = call(
        &router,
        "POST",
        "/mcp",
        &[],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
    )
    .await;
    let listed: Value = serde_json::from_slice(&body).unwrap();
    let gaps: Vec<&Value> = listed["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|t| t["name"] == "gap")
        .collect();
    let python: Value =
        serde_json::from_str(include_str!("../../tests/fixtures/gap_tool_python.json")).unwrap();
    assert_eq!(gaps, [&python]);
}

/// The door's MCP answers, byte for byte (`agents/door/door.py` `_mcp_result` / `_mcp_error`):
/// keys in the order the door writes them, not sorted.
#[test]
fn gap_mcp_envelopes_are_the_doors_bytes() {
    use crate::serve::mcp::gap_wire_for_test;
    let ok = gap_wire_for_test(
        &json!(7),
        Ok(r#"{"gap":"gap:ba59c36419069660","kind":"missing","handed":0,"unknown":0}"#),
    );
    assert_eq!(
        ok,
        r#"{"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"{\"gap\":\"gap:ba59c36419069660\",\"kind\":\"missing\",\"handed\":0,\"unknown\":0}"}],"structuredContent":{"gap":"gap:ba59c36419069660","kind":"missing","handed":0,"unknown":0},"isError":false}}"#
    );
    let err = gap_wire_for_test(&json!(7), Err((-32602, "session_id is required")));
    assert_eq!(
        err,
        r#"{"jsonrpc":"2.0","id":7,"error":{"code":-32602,"message":"session_id is required"}}"#
    );
}

/// The same through the router: a gap call's error leads with `jsonrpc`, then `id`, then `error`.
#[tokio::test]
async fn gap_call_error_reaches_the_wire_in_the_doors_key_order() {
    let router = build_router(state_with(None, None));
    let (_, body) = call(
        &router,
        "POST",
        "/mcp",
        &[],
        r#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"gap","arguments":{}}}"#,
    )
    .await;
    let text = String::from_utf8(body.clone()).unwrap();
    assert!(
        text.starts_with(r#"{"jsonrpc":"2.0","id":7,"error":{"code":-32603,"message":"#),
        "{text}"
    );
}
