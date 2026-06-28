//! Rust integration tests for the Storage Layer contract.
//!
//! These tests exercise the live PostgreSQL backend, NOT the HTTP/MCP surface
//! (that belongs in `scripts/e2e.sh` and `data/eval/run_eval.py`). They need a
//! Postgres instance reachable via `BORING_TEST_DATABASE_URL`. If the variable is
//! unset, the tests are skipped with a clear message.
//!
//! Run via (serially — they share one DB and `compact()` does a global `REINDEX CONCURRENTLY`,
//! which conflicts with other tests' open connections under the default parallel runner):
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/boring_test \`
//!   `  cargo test -p drudge --test store_integration -- --test-threads=1`
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::frontmatter::{Claim, FrontMatter};
use drudge::store::Store;
use tokio_postgres::{Client, NoTls};

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

async fn connect(dsn: &str) -> Client {
    let (client, conn) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("connect to Postgres");
    tokio::spawn(conn);
    client
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/vault/wiki/{prefix}-{ts}.md")
}

fn dummy_frontmatter(path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: "test".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("test note".to_string()),
        tags: vec!["test".to_string()],
        ..Default::default()
    }
}

async fn count_claims(db: &Client, path: &str) -> i64 {
    db.query_one(
        "SELECT count(*) FROM claim WHERE source_path = $1;",
        &[&path],
    )
    .await
    .expect("count claims")
    .get(0)
}

/// Ensure VACUUM/REINDEX CONCURRENTLY run outside a transaction block.
#[tokio::test]
async fn compact_succeeds_in_autocommit_mode() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let summary = store.compact().await.expect("compact should not fail");
    assert!(summary.total_ms > 0, "compact should report elapsed time");
}

/// Ensure current_claims honors exclude_origins (a claim's origin comes from its parent document via
/// the JOIN), so a claim can't bypass the same origin boundary the recalled chunks in an answer respect.
#[tokio::test]
async fn current_claims_honors_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // Two docs with claims, distinct origins. Unique subjects so we can identify them in results.
    let p_path = unique_path("claim-origin-personal");
    let c_path = unique_path("claim-origin-company");
    let p_subj = format!("subj-personal-{}", p_path.len());
    let c_subj = format!("subj-company-{}", c_path.len());

    let mut p_front = dummy_frontmatter(&p_path);
    p_front.origin = "personal".to_string();
    let mut c_front = dummy_frontmatter(&c_path);
    c_front.origin = "company".to_string();

    let mut emb_p = [0.0_f32; 1024];
    emb_p[0] = 1.0;
    let mut emb_c = [0.0_f32; 1024];
    emb_c[1] = 1.0;

    for (front, subj, emb) in [(&p_front, &p_subj, &emb_p), (&c_front, &c_subj, &emb_c)] {
        store
            .upsert_document(front, "sha-claim-origin", SystemTime::now())
            .await
            .expect("upsert document");
        store
            .upsert_claim(subj, "is", "x", &front.source_path, SystemTime::now(), emb)
            .await
            .expect("upsert claim");
    }

    let query = [0.5_f32; 1024]; // near both
    let subjects = |rows: Vec<(String, String, String)>| -> Vec<String> {
        rows.into_iter().map(|(s, _, _)| s).collect()
    };

    // No exclusion → both visible.
    let all = subjects(
        store
            .current_claims(&query, 20, &[], None)
            .await
            .expect("claims all"),
    );
    assert!(
        all.contains(&p_subj) && all.contains(&c_subj),
        "both origins visible with no exclusion"
    );

    // Exclude company → company claim must be filtered out, personal kept.
    let filtered = subjects(
        store
            .current_claims(&query, 20, &["company".to_string()], None)
            .await
            .expect("claims filtered"),
    );
    assert!(
        filtered.contains(&p_subj),
        "personal claim must survive the company exclusion"
    );
    assert!(
        !filtered.contains(&c_subj),
        "company claim must be excluded"
    );

    store
        .delete_document(&p_path)
        .await
        .expect("cleanup personal");
    store
        .delete_document(&c_path)
        .await
        .expect("cleanup company");
}

/// Ensure delete_document removes not only document/edge rows but also claims,
/// because claim has no FK to document.
#[tokio::test]
async fn delete_document_removes_claims() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let path = unique_path("delete-claim-test");
    let mut front = dummy_frontmatter(&path);
    front.claims.push(Claim {
        subject: "test-subject".to_string(),
        predicate: "has".to_string(),
        value: "value".to_string(),
    });

    store
        .upsert_document(&front, "sha1", SystemTime::now())
        .await
        .expect("upsert document");
    store
        .upsert_claim(
            &front.claims[0].subject,
            &front.claims[0].predicate,
            &front.claims[0].value,
            &path,
            SystemTime::now(),
            &[0.0_f32; 1024],
        )
        .await
        .expect("upsert claim");

    assert_eq!(
        count_claims(&db, &path).await,
        1,
        "claim should exist before delete"
    );

    store.delete_document(&path).await.expect("delete document");

    assert_eq!(
        count_claims(&db, &path).await,
        0,
        "claim should be removed with document"
    );
}
