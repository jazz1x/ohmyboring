//! C-SURALL2 A4: the copy pass of the four log tables from pg into Surreal.
//!
//! Needs a throwaway pgvector (its four log tables are emptied on each test) and a throwaway
//! SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:x@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test log_copy_surreal -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::store::{LogCheck, Store, SurrealClient};
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

fn client_at(url: &str, namespace: &str) -> SurrealClient {
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

struct Fixture {
    store: Store,
    client: SurrealClient,
    dsn: String,
    db: tokio_postgres::Client,
}

async fn connect(dsn: &str) -> tokio_postgres::Client {
    let (db, conn) = tokio_postgres::connect(dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db
}

/// Empty pg log tables and a Surreal namespace of its own.
async fn fixture() -> Fixture {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let store = Store::open(&dsn, DIM).await.expect("open store");
    let db = connect(&dsn).await;
    db.batch_execute(
        "TRUNCATE event_log, recall_label, sync_state, query_log RESTART IDENTITY CASCADE;",
    )
    .await
    .unwrap();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    Fixture {
        store,
        client: client_at(&url, &format!("a4_{nanos}")),
        dsn,
        db,
    }
}

impl Fixture {
    async fn check(&self) -> Vec<LogCheck> {
        self.client.check_logs(&self.store).await.expect("check")
    }

    async fn pass(&self) {
        self.client.copy_logs(&self.store).await.expect("pass");
    }

    async fn assert_same(&self) {
        let checks = self.check().await;
        for c in &checks {
            println!("{}", c.line());
        }
        assert!(checks.iter().all(LogCheck::same), "{checks:?}");
    }

    async fn event(&self, body: &str) {
        self.db
            .execute(
                "INSERT INTO event_log (component, event_name, body) VALUES ('t', 'e', $1::text::jsonb)",
                &[&body],
            )
            .await
            .unwrap();
    }

    async fn query(&self, text: &str) -> i32 {
        self.db
            .query_one(
                "INSERT INTO query_log (endpoint, query, hit_paths, hit_dists, hit_dist_kinds, answer_snippet)
                 VALUES ('/ask', $1, ARRAY['a/b','c'], ARRAY[0.25, 1]::real[], ARRAY['cosine','cosine'], 'snip') RETURNING id",
                &[&text],
            )
            .await
            .unwrap()
            .get(0)
    }
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn values_survive_the_copy_including_hangul_and_awkward_strings() {
    let f = fixture().await;
    f.event(r#"{"msg": "한글 로그 \"인용\" \\ 줄바꿈\n끝", "n": [1, 2.5, null]}"#)
        .await;
    f.event("{}").await;
    f.db.execute(
        "INSERT INTO event_log (component, trace_id, session_id, time_unix_nano, attributes)
         VALUES ('2024-01-01T00:00:00Z', 'mimir:satellite', NULL, 1761900000123456789, '{\"k\": \"2024-01-01T00:00:00Z\"}')",
        &[],
    )
    .await
    .unwrap();
    let a = f
        .query("한글 쿼리 2024-01-01T00:00:00Z mimir:satellite \"q\" \\ \t")
        .await;
    let b = f.query("").await;
    f.db.execute(
        "INSERT INTO recall_label (query_log_id, hit_index, judge, verdict, note) VALUES ($1, 0, 'llm', 'yes', '메모'), ($1, 1, 'human', 'no', ''), ($2, 0, 'llm', 'yes', '')",
        &[&a, &b],
    )
    .await
    .unwrap();
    f.db.execute(
        "INSERT INTO sync_state (key, value, value_subject, value_predicate) VALUES ('anchor_check_watermark', now(), '주제', 'p')",
        &[],
    )
    .await
    .unwrap();

    f.pass().await;
    f.assert_same().await;
    let counts: Vec<usize> = f.check().await.iter().map(|c| c.surreal_rows).collect();
    assert_eq!(counts, vec![3, 2, 3, 1]);

    // a label's verdict changes in pg: the next pass carries the new value
    f.db.execute(
        "UPDATE recall_label SET verdict = 'maybe' WHERE judge = 'human'",
        &[],
    )
    .await
    .unwrap();
    f.pass().await;
    f.assert_same().await;
}

/// tx A takes id k, tx B (k+1) commits first and is copied, A commits afterwards: the next pass
/// has k. With `WINDOW_IDS` at 0 the row below the copied maximum is never looked at.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_lower_id_that_commits_late_is_copied_by_the_next_pass() {
    let f = fixture().await;
    f.event("{}").await;
    f.query("seed").await;
    f.pass().await;

    let mut a = connect(&f.dsn).await;
    let mut b = connect(&f.dsn).await;
    let ta = a.transaction().await.unwrap();
    let ka: i64 = ta
        .query_one(
            "INSERT INTO event_log (component, body) VALUES ('A', '{}') RETURNING id",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    let qa: i32 = ta
        .query_one(
            "INSERT INTO query_log (endpoint, query) VALUES ('/ask', 'A') RETURNING id",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    let tb = b.transaction().await.unwrap();
    tb.execute(
        "INSERT INTO event_log (component, body) VALUES ('B', '{}')",
        &[],
    )
    .await
    .unwrap();
    tb.execute(
        "INSERT INTO query_log (endpoint, query) VALUES ('/ask', 'B')",
        &[],
    )
    .await
    .unwrap();
    tb.commit().await.unwrap();

    f.pass().await;
    ta.commit().await.unwrap();
    f.pass().await;

    let there: Vec<i64> = f
        .client
        .run("SELECT VALUE pid FROM event_log;")
        .await
        .unwrap()
        .into_iter()
        .flat_map(|v| serde_json::from_value::<Vec<i64>>(v).unwrap())
        .collect();
    assert!(there.contains(&ka), "event id {ka} missing: {there:?}");
    let qs: Vec<i64> = f
        .client
        .run("SELECT VALUE pid FROM query_log;")
        .await
        .unwrap()
        .into_iter()
        .flat_map(|v| serde_json::from_value::<Vec<i64>>(v).unwrap())
        .collect();
    assert!(qs.contains(&i64::from(qa)), "query id {qa} missing: {qs:?}");
    f.assert_same().await;
}

/// The 90-day prune (the compact SQL) with its CASCADE, then a pass: row counts equal, no label
/// without its query.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_pg_prune_is_followed_and_no_label_is_left_without_its_query() {
    let f = fixture().await;
    let old = f.query("old").await;
    let fresh = f.query("fresh").await;
    f.db.execute(
        "UPDATE query_log SET created_at = now() - interval '91 days' WHERE id = $1",
        &[&old],
    )
    .await
    .unwrap();
    f.db.execute(
        "INSERT INTO recall_label (query_log_id, hit_index, judge, verdict) VALUES ($1, 0, 'llm', 'yes'), ($2, 0, 'llm', 'yes')",
        &[&old, &fresh],
    )
    .await
    .unwrap();
    f.pass().await;
    f.assert_same().await;

    f.db.execute(
        "DELETE FROM query_log WHERE created_at < now() - interval '90 days';",
        &[],
    )
    .await
    .unwrap();
    f.pass().await;
    f.assert_same().await;
    let counts: Vec<usize> = f.check().await.iter().map(|c| c.surreal_rows).collect();
    assert_eq!(counts, vec![0, 1, 1, 0]);

    let orphans = f
        .client
        .run(&format!(
            "SELECT VALUE query_log_id FROM recall_label WHERE query_log_id != {fresh};"
        ))
        .await
        .unwrap();
    assert_eq!(orphans, vec![json!([])]);
}

/// A pass that cannot reach Surreal fails and changes nothing in pg; the next pass over a live
/// Surreal converges.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_pass_that_lost_surreal_is_made_up_by_the_next() {
    let f = fixture().await;
    f.event("{}").await;
    f.query("one").await;
    f.pass().await;
    f.event("{}").await;
    f.query("two").await;

    let dead = client_at("http://127.0.0.1:1", "a4_dead");
    assert!(dead.copy_logs(&f.store).await.is_err());

    f.pass().await;
    f.assert_same().await;
}
