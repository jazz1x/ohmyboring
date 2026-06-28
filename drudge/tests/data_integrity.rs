//! Data integrity torture tests — malformed input, oversized lists, sync idempotency.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::frontmatter::{Claim, FrontMatter};
use drudge::store::Store;

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/tmp/{prefix}-{ts}")
}

fn dummy_frontmatter(path: &str) -> FrontMatter {
    FrontMatter {
        source_path: path.to_owned(),
        origin: "personal".to_owned(),
        project: "omb".to_owned(),
        title: Some("t".to_owned()),
        kind: "note".to_owned(),
        tags: vec![],
        ..Default::default()
    }
}

#[tokio::test]
async fn sync_is_idempotent() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // We can't run the full scheduler without a vault dir, but we can re-ingest the same document
    // twice and assert counts are stable.
    let path = unique_path("idempotent");
    let mut front = dummy_frontmatter(&path);
    front.claims.push(Claim {
        subject: "omb".to_owned(),
        predicate: "idempotent-test".to_owned(),
        value: "v1".to_owned(),
        kind: "fact".to_owned(),
        confidence: "certain".to_owned(),
    });

    for _ in 0..2 {
        store
            .upsert_document(&front, "sha-idem", SystemTime::now())
            .await
            .expect("upsert doc");
    }

    let docs = store
        .all_meta()
        .await
        .expect("meta")
        .into_iter()
        .filter(|m| m.source_path == path)
        .count();
    assert_eq!(
        docs, 1,
        "duplicate upsert should keep a single document row"
    );

    let claims = store
        .recent_claims(10, Some("omb"), Some(&["fact".to_owned()]))
        .await
        .expect("claims")
        .into_iter()
        .filter(|c| c.predicate == "idempotent-test")
        .count();
    assert_eq!(
        claims, 1,
        "duplicate upsert should keep a single current claim"
    );

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn oversized_claim_list_is_ingested_without_panic() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let path = unique_path("oversized-claims");
    let mut front = dummy_frontmatter(&path);
    for i in 0..75 {
        front.claims.push(Claim {
            subject: "omb".to_owned(),
            predicate: format!("claim-{i}"),
            value: format!("value-{i}").repeat(20),
            kind: "fact".to_owned(),
            confidence: "certain".to_owned(),
        });
    }

    store
        .upsert_document(&front, "sha-big", SystemTime::now())
        .await
        .expect("upsert oversized doc");

    let count = store
        .recent_claims(200, Some("omb"), Some(&["fact".to_owned()]))
        .await
        .expect("claims")
        .into_iter()
        .filter(|c| c.subject == "omb" && c.predicate.starts_with("claim-"))
        .count();
    assert_eq!(count, 75, "all oversized claims should be stored");

    store.delete_document(&path).await.expect("cleanup");
}
