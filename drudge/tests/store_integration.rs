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

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::{Claim, FrontMatter};
use drudge::store::{Doc, Store};
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
            .upsert_claim(
                subj,
                "is",
                "x",
                &front.source_path,
                SystemTime::now(),
                emb,
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let query = [0.5_f32; 1024]; // near both
    let subjects =
        |rows: Vec<Claim>| -> Vec<String> { rows.into_iter().map(|c| c.subject).collect() };

    // No exclusion → both visible.
    let all = subjects(
        store
            .current_claims(&query, 20, &[], None, None)
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
            .current_claims(&query, 20, &["company".to_string()], None, None)
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
        kind: "fact".to_string(),
        confidence: "certain".to_string(),
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
            &front.claims[0].kind,
            &front.claims[0].confidence,
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

/// nearest_document returns the closest document only when within the distance threshold.
#[tokio::test]
async fn nearest_document_respects_threshold() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let a_path = unique_path("nearest-a");
    let b_path = unique_path("nearest-b");
    let a_front = dummy_frontmatter(&a_path);
    let b_front = dummy_frontmatter(&b_path);
    store
        .upsert_document(&a_front, "sha-a", SystemTime::now())
        .await
        .expect("upsert a");
    store
        .upsert_document(&b_front, "sha-b", SystemTime::now())
        .await
        .expect("upsert b");

    let mut emb_a = [0.0_f32; 1024];
    emb_a[0] = 1.0;
    let mut emb_b = [0.0_f32; 1024];
    emb_b[1] = 1.0;

    store
        .upsert_chunk(&Doc {
            id: format!("{a_path}#0"),
            content: "A note".to_string(),
            embedding: emb_a.to_vec(),
            front: a_front.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("chunk a");
    store
        .upsert_chunk(&Doc {
            id: format!("{b_path}#0"),
            content: "B note".to_string(),
            embedding: emb_b.to_vec(),
            front: b_front.clone(),
            chunk_idx: 0,
        })
        .await
        .expect("chunk b");

    // Query close to A → should return A.
    let mut query_near_a = [0.0_f32; 1024];
    query_near_a[0] = 0.9;
    query_near_a[1] = 0.1;
    let near = store
        .nearest_document(&query_near_a, 0.2)
        .await
        .expect("nearest")
        .map(|(p, _)| p);
    assert_eq!(near, Some(a_path.clone()), "query near A should return A");

    // Distant query with tight threshold → none.
    let far = store
        .nearest_document(&[0.5_f32; 1024], 0.01)
        .await
        .expect("nearest far");
    assert!(far.is_none(), "distant query below threshold returns none");

    store.delete_document(&a_path).await.expect("cleanup a");
    store.delete_document(&b_path).await.expect("cleanup b");
}

/// Claims can carry kind/confidence and be filtered by kind.
#[tokio::test]
async fn claim_kind_and_confidence_round_trip() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-kind");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "omb",
            "release-version",
            "0.2.0",
            &path,
            SystemTime::now(),
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert decision claim");
    store
        .upsert_claim(
            "omb",
            "auth-flow",
            "unverified",
            &path,
            SystemTime::now(),
            &emb,
            "risk",
            "likely",
        )
        .await
        .expect("upsert risk claim");

    let decisions = store
        .recent_claims(10, Some("omb"), Some(&["decision".to_owned()]), &[])
        .await
        .expect("recent decisions");
    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].kind(), "decision");
    assert_eq!(decisions[0].confidence(), "certain");

    let risks = store
        .recent_claims(
            10,
            Some("omb"),
            Some(&[
                "risk".to_owned(),
                "assumption".to_owned(),
                "blocked".to_owned(),
            ]),
            &[],
        )
        .await
        .expect("recent risks");
    assert_eq!(risks.len(), 1);
    assert_eq!(risks[0].kind(), "risk");

    store.delete_document(&path).await.expect("cleanup");
}

/// `next` claims are stored and filterable alongside blockers.
#[tokio::test]
async fn next_claim_is_recallable() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-next");
    let mut front = dummy_frontmatter(&path);
    front.project = "omb".to_owned();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            "omb",
            "follow-up",
            "add next_actions endpoint",
            &path,
            SystemTime::now(),
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert next claim");

    let nexts = store
        .recent_claims(
            10,
            Some("omb"),
            Some(&["next".to_owned(), "blocked".to_owned()]),
            &[],
        )
        .await
        .expect("recent next actions");
    assert_eq!(nexts.len(), 1);
    assert_eq!(nexts[0].kind(), "next");
    assert_eq!(nexts[0].predicate, "follow-up");

    store.delete_document(&path).await.expect("cleanup");
}

/// Stalled backlog should respect the requested action kinds; old decisions stay in the decision register.
#[tokio::test]
async fn stalled_claims_honor_requested_kinds() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("claim-stalled");
    let project = format!(
        "stalled-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let mut front = dummy_frontmatter(&path);
    front.project = project.clone();
    store
        .upsert_document(&front, "sha", SystemTime::now())
        .await
        .expect("upsert doc");

    let older = SystemTime::now()
        .checked_sub(Duration::from_hours(192))
        .expect("valid older timestamp");
    let emb = [0.1_f32; 1024];
    store
        .upsert_claim(
            &project,
            "follow-up",
            "ship release checklist",
            &path,
            older,
            &emb,
            "next",
            "certain",
        )
        .await
        .expect("upsert next claim");
    store
        .upsert_claim(
            &project,
            "release-decision",
            "keep stable wiki ids",
            &path,
            older,
            &emb,
            "decision",
            "certain",
        )
        .await
        .expect("upsert decision claim");

    let stalled = store
        .stalled_claims(
            10,
            Some(&project),
            Some(&["next".to_owned(), "blocked".to_owned()]),
            &[],
            7,
        )
        .await
        .expect("stalled claims");
    assert_eq!(stalled.len(), 1);
    assert_eq!(stalled[0].kind(), "next");
    assert_eq!(stalled[0].predicate, "follow-up");

    store.delete_document(&path).await.expect("cleanup");
}
