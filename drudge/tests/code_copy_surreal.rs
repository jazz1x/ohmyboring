//! C-SURALL2 A5: the per-repository copy of the four `code_index` tables from pg into Surreal.
//!
//! Needs a throwaway pgvector (its `code_index` tables are emptied on each test) and a throwaway
//! SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:x@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test code_copy_surreal -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use drudge::code_index::{CodeIndexStore, sync_repository};
use drudge::config::{CodeIndexSource, CodeLanguage, SurrealConfig};
use drudge::store::{CodeCheck, SurrealClient};
use serde_json::json;
use tempfile::{TempDir, tempdir};
use tokio_postgres::NoTls;

fn client_at(url: &str, namespace: &str) -> SurrealClient {
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

struct Fixture {
    pg: CodeIndexStore,
    db: tokio_postgres::Client,
    client: SurrealClient,
}

/// Empty `code_index` tables and a Surreal namespace of its own.
async fn fixture() -> Fixture {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let pg = CodeIndexStore::connect(&dsn).unwrap();
    pg.initialize().await.expect("initialize");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute(
        "TRUNCATE code_index.relation, code_index.symbol, code_index.file, code_index.repository CASCADE;",
    )
    .await
    .unwrap();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    Fixture {
        pg,
        db,
        client: client_at(&url, &format!("a5_{nanos}")),
    }
}

fn source(id: &str, root: &Path) -> CodeIndexSource {
    CodeIndexSource::new(
        id,
        format!("Test {id}"),
        root.to_path_buf(),
        CodeLanguage::Rust,
        true,
    )
    .unwrap()
}

fn repo(files: &[(&str, &str)]) -> TempDir {
    let dir = tempdir().unwrap();
    for (name, body) in files {
        fs::write(dir.path().join(name), body).unwrap();
    }
    dir
}

impl Fixture {
    async fn sync(&mut self, src: &CodeIndexSource) {
        sync_repository(&mut self.pg, src).await.expect("sync");
    }

    async fn copy(&self, id: &str) -> Vec<String> {
        self.client
            .copy_code_repo(&self.pg, id)
            .await
            .expect("copy")
            .iter()
            .map(drudge::store::CodeCopy::line)
            .collect()
    }

    /// pg relation rows of a repository: all, and those with a NULL target.
    async fn relation_counts(&self, id: &str) -> (i64, i64) {
        let r = self
            .db
            .query_one(
                "SELECT count(*), count(*) FILTER (WHERE target_symbol_id IS NULL)
                   FROM code_index.relation WHERE repository_id = $1",
                &[&id],
            )
            .await
            .unwrap();
        (r.get(0), r.get(1))
    }

    async fn assert_same(&self) {
        let checks = self.client.check_code(&self.pg).await.expect("check");
        for c in &checks {
            println!("{}", c.line());
        }
        assert!(checks.iter().all(CodeCheck::same), "{checks:?}");
        assert!(
            checks
                .iter()
                .all(|c| c.pg_rows > 0 || c.table == "relation")
        );
    }
}

const LIB: &str =
    "use crate::Widget;\nstruct Widget;\nfn helper() {}\nfn run(_: Widget) { helper(); }\n";

#[tokio::test]
#[ignore = "needs BORING_TEST_DATABASE_URL and BORING_TEST_SURREAL_URL (throwaway pg + surreal)"]
async fn first_fill_then_one_repository_replace_leaves_the_tables_equal() {
    let mut fx = fixture().await;
    let a = repo(&[("lib.rs", LIB), ("extra.rs", "fn extra() {}\n")]);
    let b = repo(&[("lib.rs", "fn other() {}\n")]);
    let (sa, sb) = (source("a5-a", a.path()), source("a5-b", b.path()));
    fx.sync(&sa).await;
    fx.sync(&sb).await;
    println!("first fill a: {:?}", fx.copy("a5-a").await);
    println!("first fill b: {:?}", fx.copy("a5-b").await);
    fx.assert_same().await;

    // A repository that did not change costs no write.
    let idle = fx.copy("a5-a").await;
    assert!(
        idle.iter().all(|l| l.ends_with("upserted=0 deleted=0")),
        "{idle:?}"
    );

    // One file changed, one deleted: new rows, replaced rows and vanished rows in one repository.
    fs::write(a.path().join("lib.rs"), format!("{LIB}fn added() {{}}\n")).unwrap();
    fs::remove_file(a.path().join("extra.rs")).unwrap();
    fx.sync(&sa).await;
    println!("after replace a: {:?}", fx.copy("a5-a").await);
    fx.assert_same().await;
}

#[tokio::test]
#[ignore = "needs BORING_TEST_DATABASE_URL and BORING_TEST_SURREAL_URL (throwaway pg + surreal)"]
async fn a_symbol_that_relations_target_vanishing_leaves_the_copy_equal() {
    let mut fx = fixture().await;
    let a = repo(&[
        ("lib.rs", "fn run() { helper(); helper(); }\n"),
        ("other.rs", "fn helper() {}\n"),
    ]);
    let sa = source("a5-v", a.path());
    fx.sync(&sa).await;
    fx.copy("a5-v").await;
    fx.assert_same().await;
    let (before_total, before_null) = fx.relation_counts("a5-v").await;
    assert!(
        before_total > before_null,
        "no relation targets a symbol: fixture is vacuous"
    );

    // code-sync's own path: the file that holds the target symbol vanishes from source.
    fs::remove_file(a.path().join("other.rs")).unwrap();
    fx.sync(&sa).await;
    let (after_total, after_null) = fx.relation_counts("a5-v").await;
    println!(
        "pg relation rows of a5-v: before total={before_total} NULL-target={before_null}; after \
         total={after_total} NULL-target={after_null}; deleted={} newly-NULL={}",
        before_total - after_total,
        after_null - before_null
    );
    println!("after vanish: {:?}", fx.copy("a5-v").await);
    fx.assert_same().await;
}

#[tokio::test]
#[ignore = "needs BORING_TEST_DATABASE_URL and BORING_TEST_SURREAL_URL (throwaway pg + surreal)"]
async fn a_surreal_failure_leaves_pg_and_the_next_pass_converges() {
    let mut fx = fixture().await;
    let a = repo(&[("lib.rs", LIB)]);
    let sa = source("a5-f", a.path());
    fx.sync(&sa).await;
    fx.copy("a5-f").await;

    fs::write(a.path().join("lib.rs"), "fn changed() {}\n").unwrap();
    fx.sync(&sa).await;
    let down = client_at("http://127.0.0.1:1", "a5_down");
    assert!(down.copy_code_repo(&fx.pg, "a5-f").await.is_err());
    let checks = fx.client.check_code(&fx.pg).await.expect("check");
    assert!(
        checks.iter().any(|c| !c.same()),
        "surreal should be behind: {checks:?}"
    );

    println!("converging pass: {:?}", fx.copy("a5-f").await);
    fx.assert_same().await;
}
