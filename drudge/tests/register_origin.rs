//! Register origin-filter tests — `recent_claims` (control) and the four
//! registers in `ask` must honour `exclude_origins`.
//!
//! These tests exercise the live PostgreSQL backend, NOT the HTTP/MCP surface.
//! They need a Postgres instance reachable via `BORING_TEST_DATABASE_URL` and
//! must never point at the live `boring` database. If the variable is unset,
//! the tests are skipped with a clear message.
//!
//! Run via (serially — they share one test database):
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/boring_test \
//!   `  cargo test -p drudge --test register_origin -- --test-threads=1`
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::ask;
use drudge::config::{BoringConfig, LlmConfig};
use drudge::frontmatter::FrontMatter;
use drudge::llm::Llm;
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

fn frontmatter(path: &str, project: &str, origin: &str) -> FrontMatter {
    FrontMatter {
        source_path: path.to_owned(),
        origin: origin.to_owned(),
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

fn days_ago(n: u64) -> SystemTime {
    now() - Duration::from_secs(n * 86_400)
}

fn emb() -> [f32; 1024] {
    [0.1_f32; 1024]
}

async fn mock_llm() -> Llm {
    let app = axum::Router::new().route(
        "/chat/completions",
        axum::routing::post(|| async {
            axum::Json(serde_json::json!({
                "choices": [{"message": {"content": "mock register answer"}}]
            }))
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind mock llm");
    let addr = listener.local_addr().expect("mock llm addr");
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("mock llm serve");
    });
    let cfg = BoringConfig {
        llm: LlmConfig {
            base_url: format!("http://{addr}"),
            ..Default::default()
        },
        ..Default::default()
    };
    Llm::from_config(&cfg)
}

/// Seed one personal-origin and one company-origin document, each carrying one
/// claim of `kind` about a distinct subject, both under a unique project.
/// Returns (project, personal_path, company_path, personal_subject, company_subject).
async fn seed_origin_pair(
    store: &Store,
    tag: &str,
    kind: &str,
    valid_from: SystemTime,
) -> (String, String, String, String, String) {
    let (project, p_path) = unique(&format!("reg-{tag}-p"));
    let c_path = format!("/tmp/{project}-company");
    store
        .upsert_document(&frontmatter(&p_path, &project, "personal"), "sha", now())
        .await
        .expect("upsert personal doc");
    store
        .upsert_document(&frontmatter(&c_path, &project, "company"), "sha", now())
        .await
        .expect("upsert company doc");
    let p_subject = format!("{project}-personal");
    let c_subject = format!("{project}-company");
    store
        .upsert_claim(
            &p_subject,
            "status",
            "open",
            &p_path,
            valid_from,
            &emb(),
            kind,
            "certain",
        )
        .await
        .expect("upsert personal claim");
    store
        .upsert_claim(
            &c_subject,
            "status",
            "open",
            &c_path,
            valid_from,
            &emb(),
            kind,
            "certain",
        )
        .await
        .expect("upsert company claim");
    (project, p_path, c_path, p_subject, c_subject)
}

fn assert_register_rows(
    excluded: &ask::AnswerOut,
    unfiltered: &ask::AnswerOut,
    p_subject: &str,
    c_subject: &str,
) {
    assert!(
        excluded.sources.iter().any(|s| s == p_subject),
        "personal claim should feed the register"
    );
    assert!(
        !excluded.sources.iter().any(|s| s == c_subject),
        "company-origin subject must be excluded by the filter"
    );
    assert!(
        unfiltered.sources.iter().any(|s| s == p_subject)
            && unfiltered.sources.iter().any(|s| s == c_subject),
        "fixture must feed both origins when unfiltered"
    );
}

#[tokio::test]
async fn recent_claims_excludes_company_origin() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let kinds = ["decision".to_owned()];
    let (project, p_path, c_path, p_subject, c_subject) =
        seed_origin_pair(&store, "ctl", "decision", now()).await;

    let all = store
        .recent_claims(50, Some(&project), Some(&kinds), &[])
        .await
        .expect("recent claims unfiltered");
    assert_eq!(
        all.len(),
        2,
        "both origins should be returned without exclusion"
    );
    assert!(all.iter().any(|c| c.subject == p_subject));
    assert!(all.iter().any(|c| c.subject == c_subject));

    let personal_only = store
        .recent_claims(50, Some(&project), Some(&kinds), &["company".to_owned()])
        .await
        .expect("recent claims excluding company");
    assert_eq!(
        personal_only.len(),
        1,
        "company-origin claim should be excluded"
    );
    assert_eq!(personal_only[0].subject, p_subject);

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}

#[tokio::test]
async fn decision_register_honours_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let llm = mock_llm().await;
    let (project, p_path, c_path, p_subject, c_subject) =
        seed_origin_pair(&store, "decision", "decision", now()).await;

    let excluded =
        ask::decision_register(&store, &llm, Some(&project), &["company".to_owned()], "en")
            .await
            .expect("decision register with exclusion");
    let unfiltered = ask::decision_register(&store, &llm, Some(&project), &[], "en")
        .await
        .expect("decision register without exclusion");
    assert_register_rows(&excluded, &unfiltered, &p_subject, &c_subject);

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}

#[tokio::test]
async fn risk_register_honours_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let llm = mock_llm().await;
    let (project, p_path, c_path, p_subject, c_subject) =
        seed_origin_pair(&store, "risk", "risk", now()).await;

    let excluded = ask::risk_register(&store, &llm, Some(&project), &["company".to_owned()], "en")
        .await
        .expect("risk register with exclusion");
    let unfiltered = ask::risk_register(&store, &llm, Some(&project), &[], "en")
        .await
        .expect("risk register without exclusion");
    assert_register_rows(&excluded, &unfiltered, &p_subject, &c_subject);

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}

#[tokio::test]
async fn next_action_register_honours_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let llm = mock_llm().await;
    let (project, p_path, c_path, p_subject, c_subject) =
        seed_origin_pair(&store, "next", "next", now()).await;

    let excluded =
        ask::next_action_register(&store, &llm, Some(&project), &["company".to_owned()], "en")
            .await
            .expect("next_action register with exclusion");
    let unfiltered = ask::next_action_register(&store, &llm, Some(&project), &[], "en")
        .await
        .expect("next_action register without exclusion");
    assert_register_rows(&excluded, &unfiltered, &p_subject, &c_subject);

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}

#[tokio::test]
async fn stalled_register_honours_exclude_origins() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let llm = mock_llm().await;
    let (project, p_path, c_path, p_subject, c_subject) =
        seed_origin_pair(&store, "stalled", "next", days_ago(10)).await;

    let excluded = ask::stalled_register(
        &store,
        &llm,
        Some(&project),
        &["company".to_owned()],
        "en",
        7,
    )
    .await
    .expect("stalled register with exclusion");
    let unfiltered = ask::stalled_register(&store, &llm, Some(&project), &[], "en", 7)
        .await
        .expect("stalled register without exclusion");
    assert_register_rows(&excluded, &unfiltered, &p_subject, &c_subject);

    store.delete_document(&p_path).await.expect("cleanup p");
    store.delete_document(&c_path).await.expect("cleanup c");
}
