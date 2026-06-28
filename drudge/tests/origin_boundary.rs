//! Origin boundary tests — company data must not leak into personal-only recall.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::SystemTime;

use drudge::frontmatter::{Claim, FrontMatter};
use drudge::store::Store;

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/tmp/{prefix}-{ts}")
}

fn dummy_frontmatter(path: &str) -> FrontMatter {
    FrontMatter {
        source_path: path.to_owned(),
        origin: String::new(),
        project: String::new(),
        title: Some("t".to_owned()),
        kind: "note".to_owned(),
        tags: vec![],
        ..Default::default()
    }
}

#[tokio::test]
async fn current_claims_respects_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let p_path = unique_path("origin-personal");
    let c_path = unique_path("origin-company");
    let mut p_front = dummy_frontmatter(&p_path);
    p_front.origin = "personal".to_owned();
    p_front.claims.push(Claim {
        subject: "personal-subj".to_owned(),
        predicate: "status".to_owned(),
        value: "ok".to_owned(),
        kind: "fact".to_owned(),
        confidence: "certain".to_owned(),
    });
    let mut c_front = dummy_frontmatter(&c_path);
    c_front.origin = "company".to_owned();
    c_front.claims.push(Claim {
        subject: "company-subj".to_owned(),
        predicate: "status".to_owned(),
        value: "ok".to_owned(),
        kind: "fact".to_owned(),
        confidence: "certain".to_owned(),
    });

    store
        .upsert_document(&p_front, "sha", SystemTime::now())
        .await
        .expect("upsert personal");
    store
        .upsert_document(&c_front, "sha", SystemTime::now())
        .await
        .expect("upsert company");

    let q = [0.5_f32; 1024];
    let all = store
        .current_claims(&q, 10, &[], None, None)
        .await
        .expect("all claims");
    assert_eq!(all.len(), 2, "expected both claims without exclusion");

    let personal_only = store
        .current_claims(&q, 10, &["company".to_owned()], None, None)
        .await
        .expect("personal only");
    assert_eq!(personal_only.len(), 1, "company claim should be excluded");
    assert_eq!(personal_only[0].subject, "personal-subj");

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}
