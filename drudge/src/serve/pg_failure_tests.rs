//! 5xx bodies of the routes that reach Postgres, against a real throwaway database: the engine's
//! bytes must be the door's (psycopg `str(e)`). Needs `BORING_TEST_DATABASE_URL`; run serially.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;

use axum::http::StatusCode;
use tokio_postgres::NoTls;
use tower::ServiceExt;

use super::{AppState, build_router};
use crate::store::Store;

async fn admin(dsn: &str) -> tokio_postgres::Client {
    let (client, conn) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("admin connect");
    tokio::spawn(conn);
    client
}

/// A fresh database with the schema opened on it, so nothing a test breaks leaks to the next.
async fn fresh_store(admin_dsn: &str, name: &str) -> (Store, String) {
    admin(admin_dsn)
        .await
        .batch_execute(&format!("CREATE DATABASE {name}"))
        .await
        .expect("create database");
    let (base, _) = admin_dsn.rsplit_once('/').expect("dsn has a database");
    let dsn = format!("{base}/{name}");
    let store = Store::open(&dsn, 1024).await.expect("open store");
    (store, dsn)
}

fn unique(prefix: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}_{nanos}")
}

/// An `/embeddings` endpoint that answers a zero vector, so /search reaches its store stages.
async fn embed_stub() -> String {
    let app = axum::Router::new().route(
        "/embeddings",
        axum::routing::post(|| async {
            axum::Json(serde_json::json!({"data": [{"embedding": vec![0.0_f32; 1024]}]}))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await });
    url
}

async fn state(store: Store) -> AppState {
    let mut cfg = crate::config::BoringConfig::default();
    cfg.llm.base_url = embed_stub().await;
    AppState {
        store: Some(Arc::new(store)),
        code_index: None,
        llm: Arc::new(crate::llm::Llm::from_config(&cfg)),
        vault_dir: Arc::new(None),
        pii: Arc::new(None),
        cfg: Arc::new(cfg),
        cfg_path: Arc::new(None),
        sync_lock: Arc::default(),
        remember_gate: Arc::default(),
        note_index: Arc::default(),
        wiki_index: Arc::default(),
        last_compact: Arc::default(),
        compact_failure: Arc::default(),
        db_healthy_last: Arc::default(),
        owner_token: Arc::new(None),
    }
}

async fn call(state: AppState, method: &str, uri: &str, body: &str) -> (StatusCode, String) {
    let req = axum::extract::Request::builder()
        .method(method)
        .uri(uri)
        .header("content-type", "application/json")
        .body(axum::body::Body::from(body.to_owned()))
        .unwrap();
    let res = build_router(state).oneshot(req).await.unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, String::from_utf8(bytes.to_vec()).unwrap())
}

/// The server now refuses every new connection to `name` (what a `template0` dial gets) and the
/// pooled ones are gone, so the next request has to dial.
async fn refuse_connections(admin_dsn: &str, name: &str) {
    let admin = admin(admin_dsn).await;
    admin
        .batch_execute(&format!(
            "ALTER DATABASE {name} WITH ALLOW_CONNECTIONS false"
        ))
        .await
        .expect("disallow connections");
    admin
        .execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            &[&name],
        )
        .await
        .expect("terminate backends");
}

fn expected_target(dsn: &str) -> String {
    let cfg: tokio_postgres::Config = dsn.parse().unwrap();
    let port = cfg.get_ports().first().copied().unwrap();
    match cfg.get_hosts().first().unwrap() {
        tokio_postgres::config::Host::Tcp(host) => {
            format!("connection to server at \"{host}\", port {port}")
        }
        tokio_postgres::config::Host::Unix(dir) => {
            format!("connection to server on socket \"{}\"", dir.display())
        }
    }
}

#[tokio::test]
async fn read_routes_502_carry_error_then_detail_in_the_door_wording() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("t5xx_read");
    let (store, dsn) = fresh_store(&admin_dsn, &name).await;
    let state = state(store).await;
    refuse_connections(&admin_dsn, &name).await;
    let detail = format!(
        "connection failed: {} failed: FATAL:  database \"{name}\" is not currently accepting connections",
        expected_target(&dsn)
    );
    // keys in the door's order: error, then detail
    let want = format!(
        "{{\"error\":\"store unreachable\",\"detail\":{}}}",
        serde_json::to_string(&detail).unwrap()
    );
    for uri in [
        "/rules",
        "/approved",
        "/claim-source?subject=x",
        "/claim-sources?predicate=decision",
        "/projects?active_days=7",
    ] {
        let (status, body) = call(state.clone(), "GET", uri, "").await;
        assert_eq!(
            (status, body.as_str()),
            (StatusCode::BAD_GATEWAY, want.as_str()),
            "{uri}"
        );
    }
    admin(&admin_dsn)
        .await
        .batch_execute(&format!("DROP DATABASE {name} WITH (FORCE)"))
        .await
        .ok();
}

#[tokio::test]
async fn search_502_names_the_failed_stage_and_uses_the_door_detail() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("t5xx_search");
    let (store, _) = fresh_store(&admin_dsn, &name).await;
    let state = state(store).await;

    // cell 3: a query-stage server error is `vector: <message>`
    let (status, body) = call(
        state.clone(),
        "POST",
        "/search",
        r#"{"query":"q","since_hours":2147483647}"#,
    )
    .await;
    assert_eq!(
        (status, body.as_str()),
        (
            StatusCode::BAD_GATEWAY,
            r#"{"error":"search failed: vector: timestamp out of range"}"#
        )
    );
    admin(&admin_dsn)
        .await
        .batch_execute(&format!("DROP DATABASE {name} WITH (FORCE)"))
        .await
        .ok();
}

/// A refused connection is `store: connection failed: …`, whichever query dialled first.
#[tokio::test]
async fn search_502_on_a_refused_connection_is_the_store_stage() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("t5xx_refused");
    let (store, dsn) = fresh_store(&admin_dsn, &name).await;
    let state = state(store).await;
    refuse_connections(&admin_dsn, &name).await;
    let (status, body) = call(state, "POST", "/search", r#"{"query":"q"}"#).await;
    let want = serde_json::json!({"error": format!(
        "search failed: store: connection failed: {} failed: FATAL:  database \"{name}\" is not currently accepting connections",
        expected_target(&dsn)
    )});
    assert_eq!(
        (status, body.as_str()),
        (
            StatusCode::BAD_GATEWAY,
            serde_json::to_string(&want).unwrap().as_str()
        )
    );
    admin(&admin_dsn)
        .await
        .batch_execute(&format!("DROP DATABASE {name} WITH (FORCE)"))
        .await
        .ok();
}

#[tokio::test]
async fn handover_502_is_the_libpq_message_block() {
    let Some(admin_dsn) = std::env::var("BORING_TEST_DATABASE_URL").ok() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let name = unique("t5xx_handover");
    let (store, dsn) = fresh_store(&admin_dsn, &name).await;
    let state = state(store).await;
    admin(&dsn)
        .await
        .batch_execute(
            "CREATE FUNCTION parity_fail() RETURNS trigger LANGUAGE plpgsql AS
               $$ BEGIN RAISE EXCEPTION 'parity forced handover failure'; END $$;
             CREATE TRIGGER parity_fail BEFORE INSERT ON node
               FOR EACH ROW EXECUTE FUNCTION parity_fail();",
        )
        .await
        .expect("install failing trigger");
    let (status, body) = call(
        state,
        "POST",
        "/search",
        r#"{"query":"q","session_id":"s-1"}"#,
    )
    .await;
    assert_eq!(
        (status, body.as_str()),
        (
            StatusCode::BAD_GATEWAY,
            r#"{"error":"handover failed: parity forced handover failure\nCONTEXT:  PL/pgSQL function parity_fail() line 1 at RAISE"}"#
        )
    );
    admin(&admin_dsn)
        .await
        .batch_execute(&format!("DROP DATABASE {name} WITH (FORCE)"))
        .await
        .ok();
}
