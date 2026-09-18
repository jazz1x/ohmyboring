//! Integration tests for the opt-in `/search` claims handover (`declared_claims`).
//!
//! These exercise the storage contract behind the `claims` parameter against a live PostgreSQL
//! backend: edge-backed selection (a row without a `claims` edge is a claim the note no longer
//! declares), the `fact` exclusion, kind/recency ordering, the per-hit cut, and the canonical
//! `node_id`. The HTTP-level freeze guard lives in `src/serve/http.rs` — it needs serve
//! internals, which `pub(crate)` keeps inside the crate.
//!
//! Run via (serially — they share one DB):
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/boring_test \
//!   `  cargo test -p drudge --test search_claims_integration -- --test-threads=1`
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::{Claim, FrontMatter};
use drudge::store::Store;

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn stamp() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

fn fixture(project: &str, path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("claims fixture".to_string()),
        tags: vec![],
        ..Default::default()
    }
}

/// One edge-backed claim: the row the note declares, plus the `doc —claims→ claim` edge.
async fn declare_claim(
    store: &Store,
    front: &FrontMatter,
    subject: &str,
    predicate: &str,
    value: &str,
    kind: &str,
    valid_from: SystemTime,
) {
    let embedding = vec![0.0_f32; 1024];
    store
        .upsert_claim(
            subject,
            predicate,
            value,
            &front.source_path,
            valid_from,
            &embedding,
            kind,
            "high",
        )
        .await
        .expect("upsert claim");
    store
        .upsert_claim_node(
            &front.source_path,
            &front.project,
            subject,
            predicate,
            &Claim {
                subject: subject.to_string(),
                predicate: predicate.to_string(),
                value: value.to_string(),
                kind: kind.to_string(),
                confidence: "high".to_string(),
            },
        )
        .await
        .expect("upsert claim node");
}

#[tokio::test]
async fn declared_claims_returns_newest_first_and_states_the_cut() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let ts = stamp();
    let project = format!("claims-cut-{ts}");
    let path = format!("/vault/wiki/claims-cut-{ts}.md");
    let front = fixture(&project, &path);
    store
        .upsert_document(&front, "sha-cut", SystemTime::now())
        .await
        .expect("upsert document");

    let base = SystemTime::now();
    for (i, subject) in ["cut-a", "cut-b", "cut-c"].iter().enumerate() {
        declare_claim(
            &store,
            &front,
            &format!("{subject}-{ts}"),
            "decision",
            &format!("decision {i}"),
            "decision",
            base + Duration::from_secs(i as u64),
        )
        .await;
    }

    let mut declared = store
        .declared_claims(std::slice::from_ref(&path), 2)
        .await
        .expect("declared claims");
    let rows = declared.remove(&path).expect("the hit's claims");
    assert_eq!(rows.rows.len(), 2, "claims=2 caps the handover at two");
    assert!(
        rows.rows[0].valid_from >= rows.rows[1].valid_from,
        "newest first by valid_from: {:?} then {:?}",
        rows.rows[0].valid_from,
        rows.rows[1].valid_from
    );
    assert!(
        rows.rows[0].value.contains("decision 2"),
        "the newest claim survives the cut: {}",
        rows.rows[0].value
    );
    assert_eq!(
        rows.total_matching, 3,
        "the cut is stated, not silent: three qualify, two are returned"
    );

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn declared_claims_skips_rows_without_claims_edge() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let ts = stamp();
    let project = format!("claims-edge-{ts}");
    let path = format!("/vault/wiki/claims-edge-{ts}.md");
    let other_path = format!("/vault/wiki/claims-edge-other-{ts}.md");
    let front = fixture(&project, &path);
    let other_front = fixture(&project, &other_path);
    for f in [&front, &other_front] {
        store
            .upsert_document(f, "sha-edge", SystemTime::now())
            .await
            .expect("upsert document");
    }

    // A live row for this source_path the note no longer declares: no `claims` edge points at it.
    // Seeded NEWER than the declared claim on purpose: a source_path regression must surface this
    // row, not lose it to the same-key dedup and slip past the guard.
    let embedding = vec![0.0_f32; 1024];
    let base = SystemTime::now();
    store
        .upsert_claim(
            &format!("orphan-row-{ts}"),
            "decision",
            "stale wording",
            &path,
            base + Duration::from_secs(10),
            &embedding,
            "decision",
            "high",
        )
        .await
        .expect("upsert edgeless claim");
    // The contrasting pair: same shape, but the note does declare it (edge-backed).
    declare_claim(
        &store,
        &front,
        &format!("declared-{ts}"),
        "decision",
        "current wording",
        "decision",
        base,
    )
    .await;
    // A claim of the OTHER note: edge-backed, but not by this document.
    declare_claim(
        &store,
        &other_front,
        &format!("other-note-{ts}"),
        "decision",
        "someone else's solve",
        "decision",
        base,
    )
    .await;

    let mut declared = store
        .declared_claims(std::slice::from_ref(&path), 10)
        .await
        .expect("declared claims");
    let rows = declared.remove(&path).expect("the hit's claims");
    assert_eq!(
        rows.rows.len(),
        1,
        "only the edge-backed claim is handed over: {:?}",
        rows.rows.iter().map(|r| &r.subject).collect::<Vec<_>>()
    );
    assert_eq!(
        rows.rows[0].subject,
        format!("declared-{ts}"),
        "the row without a claims edge must not come back as if the note declared it"
    );
    assert_eq!(rows.total_matching, 1);

    store.delete_document(&path).await.expect("cleanup");
    store
        .delete_document(&other_path)
        .await
        .expect("cleanup other");
}

#[tokio::test]
async fn declared_claims_never_returns_fact_claims() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let ts = stamp();
    let project = format!("claims-fact-{ts}");
    let path = format!("/vault/wiki/claims-fact-{ts}.md");
    let front = fixture(&project, &path);
    store
        .upsert_document(&front, "sha-fact", SystemTime::now())
        .await
        .expect("upsert document");

    // The note declares exactly one claim, and it is a fact: context, not the record of a solve.
    declare_claim(
        &store,
        &front,
        &format!("fact-only-{ts}"),
        "stack",
        "the service runs on pgvector",
        "fact",
        SystemTime::now(),
    )
    .await;

    let declared = store
        .declared_claims(std::slice::from_ref(&path), 10)
        .await
        .expect("declared claims");
    assert!(
        !declared.contains_key(&path),
        "a fact-only note hands over nothing, even when nothing else qualifies"
    );

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn declared_claims_node_id_is_canonical() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let ts = stamp();
    let project = format!("claims-node-id-{ts}");
    let path = format!("/vault/wiki/claims-node-id-{ts}.md");
    let front = fixture(&project, &path);
    store
        .upsert_document(&front, "sha-node-id", SystemTime::now())
        .await
        .expect("upsert document");

    let subject = format!("canonical subject {ts}");
    let predicate = "decision".to_string();
    declare_claim(
        &store,
        &front,
        &subject,
        &predicate,
        "use the register row's key",
        "decision",
        SystemTime::now(),
    )
    .await;

    let mut declared = store
        .declared_claims(std::slice::from_ref(&path), 10)
        .await
        .expect("declared claims");
    let rows = declared.remove(&path).expect("the hit's claims");
    assert_eq!(rows.rows.len(), 1);
    assert_eq!(
        rows.rows[0].node_id,
        format!("claim:{subject}:{predicate}"),
        "the canonical claim key — the same spelling the registers answer with"
    );

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn declared_claims_prefers_decisions_then_next_then_risk() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let ts = stamp();
    let project = format!("claims-kind-{ts}");
    let path = format!("/vault/wiki/claims-kind-{ts}.md");
    let front = fixture(&project, &path);
    store
        .upsert_document(&front, "sha-kind", SystemTime::now())
        .await
        .expect("upsert document");

    let base = SystemTime::now();
    // Deliberately seeded newest-to-oldest in reverse preference order: recency would put the
    // risk first, the preference must still pull the decision to the front.
    declare_claim(
        &store,
        &front,
        &format!("risk-{ts}"),
        "risk",
        "might slip",
        "risk",
        base + Duration::from_secs(4),
    )
    .await;
    declare_claim(
        &store,
        &front,
        &format!("next-{ts}"),
        "next",
        "write the test",
        "next",
        base + Duration::from_secs(3),
    )
    .await;
    declare_claim(
        &store,
        &front,
        &format!("blocked-{ts}"),
        "blocked",
        "waiting on review",
        "blocked",
        base + Duration::from_secs(2),
    )
    .await;
    declare_claim(
        &store,
        &front,
        &format!("decision-{ts}"),
        "decision",
        "the call we made",
        "decision",
        base,
    )
    .await;

    let mut declared = store
        .declared_claims(std::slice::from_ref(&path), 10)
        .await
        .expect("declared claims");
    let rows = declared.remove(&path).expect("the hit's claims");
    let order: Vec<&str> = rows.rows.iter().map(|r| r.kind.as_str()).collect();
    assert_eq!(
        order,
        ["decision", "next", "blocked", "risk"],
        "decision first, then next/blocked, then risk, whatever the timestamps say: {order:?}"
    );

    store.delete_document(&path).await.expect("cleanup");
}
