//! Origin boundary tests — company data must not leak into personal-only recall.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::Store;

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique(prefix: &str) -> (String, String) {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    (format!("{prefix}-{ts}"), format!("/tmp/{prefix}-{ts}"))
}

fn dummy_frontmatter(path: &str, project: &str) -> FrontMatter {
    FrontMatter {
        source_path: path.to_owned(),
        origin: String::new(),
        project: project.to_owned(),
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
async fn current_claims_respects_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let (project, p_path) = unique("origin-personal");
    let c_path = format!("/tmp/{project}-company");
    let mut p_front = dummy_frontmatter(&p_path, &project);
    p_front.origin = "personal".to_owned();
    let mut c_front = dummy_frontmatter(&c_path, &project);
    c_front.origin = "company".to_owned();

    store
        .upsert_document(&p_front, "sha", now())
        .await
        .expect("upsert personal");
    store
        .upsert_document(&c_front, "sha", now())
        .await
        .expect("upsert company");

    store
        .upsert_claim(
            &project,
            "personal-status",
            "ok",
            &p_path,
            now(),
            &emb(),
            "fact",
            "certain",
        )
        .await
        .expect("upsert personal claim");
    store
        .upsert_claim(
            &project,
            "company-status",
            "ok",
            &c_path,
            now(),
            &emb(),
            "fact",
            "certain",
        )
        .await
        .expect("upsert company claim");

    let q = [0.5_f32; 1024];
    let all = store
        .current_claims(&q, 10, &[], Some(&project), None)
        .await
        .expect("all claims");
    assert_eq!(all.len(), 2, "expected both claims without exclusion");

    let personal_only = store
        .current_claims(&q, 10, &["company".to_owned()], Some(&project), None)
        .await
        .expect("personal only");
    assert_eq!(personal_only.len(), 1, "company claim should be excluded");
    assert_eq!(personal_only[0].subject, project);

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}
