//! Integration tests for the structured context card (`ask::context_card` and `/context`).
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::ask::context_card;
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
        origin: "personal".to_owned(),
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
async fn context_card_returns_structured_sections() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let (project, path) = unique("ctx-card");
    let front = dummy_frontmatter(&path, &project);
    store
        .upsert_document(&front, "sha-ctx", now())
        .await
        .expect("upsert doc");

    for (pred, value, kind) in [
        ("decided", "use context cards", "decision"),
        ("risk", "token noise", "risk"),
        ("has", "claims", "fact"),
        ("is", "a temporal fact", "term"),
    ] {
        store
            .upsert_claim(&project, pred, value, &path, now(), &emb(), kind, "certain")
            .await
            .expect("upsert claim");
    }

    let card = context_card(&store, Some(&project), &[], 5, "ko")
        .await
        .expect("context card");

    assert_eq!(card.language, "ko");
    assert_eq!(card.decisions.len(), 1);
    assert_eq!(card.decisions[0].kind, "decision");
    assert_eq!(card.risks.len(), 1);
    assert_eq!(card.risks[0].kind, "risk");
    assert_eq!(card.facts.len(), 1);
    assert_eq!(card.facts[0].kind, "fact");
    assert_eq!(card.glossary.len(), 1);
    assert_eq!(card.glossary[0].kind, "term");

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn context_card_respects_max_items() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let (project, path) = unique("ctx-max");
    let front = dummy_frontmatter(&path, &project);
    store
        .upsert_document(&front, "sha-max", now())
        .await
        .expect("upsert doc");

    for i in 0..10 {
        store
            .upsert_claim(
                &project,
                &format!("fact-{i}"),
                &format!("value-{i}"),
                &path,
                now(),
                &emb(),
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }

    let card = context_card(&store, Some(&project), &[], 3, "ko")
        .await
        .expect("context card");
    assert_eq!(card.facts.len(), 3, "max_items should cap facts section");

    store.delete_document(&path).await.expect("cleanup");
}

#[tokio::test]
async fn context_card_excludes_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");

    let (project, p_path) = unique("ctx-personal");
    let c_path = format!("/tmp/{project}-company");
    let mut p_front = dummy_frontmatter(&p_path, &project);
    p_front.origin = "personal".to_owned();
    let mut c_front = dummy_frontmatter(&c_path, &project);
    c_front.origin = "company".to_owned();

    store
        .upsert_document(&p_front, "sha-p", now())
        .await
        .expect("upsert p");
    store
        .upsert_document(&c_front, "sha-c", now())
        .await
        .expect("upsert c");

    store
        .upsert_claim(
            &project,
            "personal-decision",
            "v",
            &p_path,
            now(),
            &emb(),
            "decision",
            "certain",
        )
        .await
        .expect("upsert p claim");
    store
        .upsert_claim(
            &project,
            "company-decision",
            "v",
            &c_path,
            now(),
            &emb(),
            "decision",
            "certain",
        )
        .await
        .expect("upsert c claim");

    let all = context_card(&store, Some(&project), &[], 5, "ko")
        .await
        .expect("all");
    assert_eq!(all.decisions.len(), 2);

    let personal_only = context_card(&store, Some(&project), &["company".to_owned()], 5, "ko")
        .await
        .expect("personal only");
    assert_eq!(personal_only.decisions.len(), 1);
    assert_eq!(personal_only.decisions[0].predicate, "personal-decision");

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}
