//! Data integrity torture tests — malformed input, oversized lists, sync idempotency.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
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

fn now() -> SystemTime {
    SystemTime::now()
}

fn emb() -> [f32; 1024] {
    [0.1_f32; 1024]
}

#[tokio::test]
async fn sync_is_idempotent() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    // Re-ingest the same document and claim twice; counts must stay stable.
    let path = unique_path("idempotent");
    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-idem", now())
        .await
        .expect("upsert doc");

    for _ in 0..2 {
        store
            .upsert_claim(
                "omb",
                "idempotent-test",
                "v1",
                &path,
                now(),
                &emb(),
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let claims = store
        .recent_claims(10, Some("omb"), Some(&["fact".to_owned()]), &[], None)
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
    let front = dummy_frontmatter(&path);
    store
        .upsert_document(&front, "sha-big", now())
        .await
        .expect("upsert doc");

    for i in 0..75 {
        store
            .upsert_claim(
                "omb",
                &format!("claim-{i}"),
                &format!("value-{i}").repeat(20),
                &path,
                now(),
                &emb(),
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let count = store
        .recent_claims(200, Some("omb"), Some(&["fact".to_owned()]), &[], None)
        .await
        .expect("claims")
        .into_iter()
        .filter(|c| c.subject == "omb" && c.predicate.starts_with("claim-"))
        .count();
    assert_eq!(count, 75, "all oversized claims should be stored");

    store.delete_document(&path).await.expect("cleanup");
}
