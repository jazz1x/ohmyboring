//! Live PostgreSQL guardrails for the isolated code-index corpus.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use drudge::code_index::{CodeIndexStore, sync_repository};
use drudge::config::{CodeIndexSource, CodeLanguage};
use tempfile::tempdir;
use tokio_postgres::{Client, NoTls};

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique_id(prefix: &str) -> String {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{timestamp}")
}

fn source(id: &str, root: &std::path::Path) -> CodeIndexSource {
    source_with_language(id, root, CodeLanguage::Rust)
}

fn source_with_language(
    id: &str,
    root: &std::path::Path,
    language: CodeLanguage,
) -> CodeIndexSource {
    CodeIndexSource::new(id, format!("Test {id}"), root.to_path_buf(), language, true)
        .expect("valid test source")
}

async fn connect(dsn: &str) -> Client {
    let (client, connection) = tokio_postgres::connect(dsn, NoTls)
        .await
        .expect("connect to test Postgres");
    tokio::spawn(connection);
    client
}

async fn initialized_store(dsn: &str) -> CodeIndexStore {
    let store = CodeIndexStore::connect(dsn).expect("connect code index");
    store.initialize().await.expect("initialize code index");
    store
}

async fn repository_counts(db: &Client, repository_id: &str) -> (i64, i64) {
    let row = db
        .query_one(
            "SELECT
                (SELECT count(*) FROM code_index.symbol WHERE repository_id = $1)::bigint,
                (SELECT count(*) FROM code_index.relation WHERE repository_id = $1)::bigint",
            &[&repository_id],
        )
        .await
        .unwrap();
    (row.get(0), row.get(1))
}

#[tokio::test]
async fn sha_skip_missing_root_and_prune_preserve_repository_ownership() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let first_root = tempdir().unwrap();
    let second_root = tempdir().unwrap();
    fs::write(first_root.path().join("lib.rs"), "pub fn first() {}\n").unwrap();
    fs::write(second_root.path().join("lib.rs"), "pub fn second() {}\n").unwrap();
    let first_id = unique_id("code-owner-a");
    let second_id = unique_id("code-owner-b");
    let first = source(&first_id, first_root.path());
    let second = source(&second_id, second_root.path());
    let mut store = initialized_store(&dsn).await;

    let initial = sync_repository(&mut store, &first)
        .await
        .expect("sync first");
    assert_eq!(initial.changed, 1);
    let unchanged = sync_repository(&mut store, &first)
        .await
        .expect("SHA skip first");
    assert_eq!(unchanged.changed, 0);
    assert_eq!(unchanged.unchanged, 1);
    sync_repository(&mut store, &second)
        .await
        .expect("sync second");

    let missing = source(&first_id, &first_root.path().join("missing"));
    assert!(sync_repository(&mut store, &missing).await.is_err());
    assert_eq!(store.status(Some(&first_id)).await.unwrap()[0].files, 1);

    fs::remove_file(first_root.path().join("lib.rs")).unwrap();
    let pruned = sync_repository(&mut store, &first)
        .await
        .expect("prune first");
    assert_eq!(pruned.deleted, 1);
    assert_eq!(store.status(Some(&first_id)).await.unwrap()[0].files, 0);
    assert_eq!(store.status(Some(&second_id)).await.unwrap()[0].files, 1);

    let db = connect(&dsn).await;
    db.execute(
        "DELETE FROM code_index.repository WHERE id = ANY($1)",
        &[&vec![first_id, second_id]],
    )
    .await
    .expect("clean up test repositories");
}

#[tokio::test]
async fn parser_status_and_relations_persist_in_the_isolated_schema() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let root = tempdir().unwrap();
    fs::write(
        root.path().join("lib.rs"),
        "use crate::Widget;\nstruct Widget;\nfn helper() {}\nfn run(_: Widget) { helper(); }\n",
    )
    .unwrap();
    fs::write(root.path().join("broken.rs"), "fn broken( {").unwrap();
    let repository_id = unique_id("code-parser");
    let configured = source(&repository_id, root.path());
    let mut store = initialized_store(&dsn).await;
    let report = sync_repository(&mut store, &configured)
        .await
        .expect("sync parser fixture");
    assert_eq!(report.scanned, 2);
    assert!(report.parse_errors > 0);

    let status = &store.status(Some(&repository_id)).await.unwrap()[0];
    assert_eq!(status.files, 2);
    assert_eq!(status.files_with_errors, 1);
    assert!(status.symbols >= 3);
    assert!(status.relations >= 4);
    let total_parse_errors = status.parse_errors;

    fs::write(
        root.path().join("lib.rs"),
        "use crate::Widget;\nstruct Widget;\nfn helper() {}\nfn run(_: Widget) { helper(); helper(); }\n",
    )
    .unwrap();
    let changed_valid_file = sync_repository(&mut store, &configured)
        .await
        .expect("resync with unchanged parse-error file");
    assert_eq!(changed_valid_file.changed, 1);
    assert_eq!(changed_valid_file.unchanged, 1);
    assert_eq!(changed_valid_file.parse_errors, total_parse_errors);

    let search_hits = store
        .search("run", Some(&repository_id))
        .await
        .expect("search indexed symbols");
    let run = search_hits
        .iter()
        .find(|hit| hit.name == "run")
        .expect("find run symbol");
    let detail = store
        .symbol(&run.id)
        .await
        .expect("read symbol detail")
        .expect("indexed symbol exists");
    assert!(detail.relations.iter().any(|relation| {
        relation.kind == "calls"
            && relation.target_name.as_deref() == Some("helper")
            && relation.target_symbol_id.is_none()
    }));

    let db = connect(&dsn).await;
    let syntactic_calls_with_target_ids: i64 = db
        .query_one(
            "SELECT count(*) FROM code_index.relation
             WHERE repository_id = $1 AND kind = 'calls'
               AND target_name = 'helper' AND target_symbol_id IS NOT NULL",
            &[&repository_id],
        )
        .await
        .expect("query resolved call")
        .get(0);
    assert_eq!(syntactic_calls_with_target_ids, 0);
    let public_memory_tables_touched: i64 = db
        .query_one(
            "SELECT count(*) FROM information_schema.tables
             WHERE table_schema = 'code_index' AND table_name IN ('document', 'chunk', 'node', 'edge', 'claim')",
            &[],
        )
        .await
        .expect("query schema separation")
        .get(0);
    assert_eq!(public_memory_tables_touched, 0);

    db.execute(
        "DELETE FROM code_index.repository WHERE id = $1",
        &[&repository_id],
    )
    .await
    .expect("clean up parser repository");
}

#[tokio::test]
async fn composite_foreign_keys_reject_cross_repository_ownership() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let first_root = tempdir().unwrap();
    let second_root = tempdir().unwrap();
    fs::write(first_root.path().join("lib.rs"), "fn first() {}\n").unwrap();
    fs::write(second_root.path().join("lib.rs"), "fn second() {}\n").unwrap();
    let first_id = unique_id("code-fk-a");
    let second_id = unique_id("code-fk-b");
    let mut store = initialized_store(&dsn).await;
    sync_repository(&mut store, &source(&first_id, first_root.path()))
        .await
        .unwrap();
    sync_repository(&mut store, &source(&second_id, second_root.path()))
        .await
        .unwrap();

    let db = connect(&dsn).await;
    let first_file: String = db
        .query_one(
            "SELECT id FROM code_index.file WHERE repository_id = $1",
            &[&first_id],
        )
        .await
        .unwrap()
        .get(0);
    let second_file: String = db
        .query_one(
            "SELECT id FROM code_index.file WHERE repository_id = $1",
            &[&second_id],
        )
        .await
        .unwrap()
        .get(0);
    let first_symbol: String = db
        .query_one(
            "SELECT id FROM code_index.symbol WHERE repository_id = $1",
            &[&first_id],
        )
        .await
        .unwrap()
        .get(0);
    let second_symbol: String = db
        .query_one(
            "SELECT id FROM code_index.symbol WHERE repository_id = $1",
            &[&second_id],
        )
        .await
        .unwrap()
        .get(0);
    let original_counts = repository_counts(&db, &first_id).await;

    let cross_file = db
        .execute(
            "INSERT INTO code_index.symbol
                (id, repository_id, file_id, kind, name, qualified_name,
                 start_byte, end_byte, start_line, end_line)
             VALUES ($1, $2, $3, 'function', 'cross', 'cross', 0, 1, 0, 0)",
            &[&unique_id("cross-file"), &first_id, &second_file],
        )
        .await;
    assert!(cross_file.is_err());

    let cross_source = db
        .execute(
            "INSERT INTO code_index.relation
                (id, repository_id, file_id, source_symbol_id, kind, target_name,
                 start_byte, end_byte)
             VALUES ($1, $2, $3, $4, 'calls', 'first', 0, 1)",
            &[
                &unique_id("cross-source"),
                &first_id,
                &first_file,
                &second_symbol,
            ],
        )
        .await;
    assert!(cross_source.is_err());

    let cross_target = db
        .execute(
            "INSERT INTO code_index.relation
                (id, repository_id, file_id, source_symbol_id, kind, target_symbol_id,
                 start_byte, end_byte)
             VALUES ($1, $2, $3, $4, 'contains', $5, 0, 1)",
            &[
                &unique_id("cross-target"),
                &first_id,
                &first_file,
                &first_symbol,
                &second_symbol,
            ],
        )
        .await;
    assert!(cross_target.is_err());

    let retained_counts = repository_counts(&db, &first_id).await;
    assert_eq!(retained_counts, original_counts);

    db.execute(
        "DELETE FROM code_index.repository WHERE id = ANY($1)",
        &[&vec![first_id, second_id]],
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn python_symbols_are_indexed_as_functions_and_classes() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let root = tempdir().unwrap();
    fs::write(
        root.path().join("module.py"),
        "class Widget:\n    def draw(self):\n        pass\n\ndef helper():\n    pass\n",
    )
    .unwrap();
    let repository_id = unique_id("code-python");
    let configured = source_with_language(&repository_id, root.path(), CodeLanguage::Python);
    let mut store = initialized_store(&dsn).await;
    let report = sync_repository(&mut store, &configured)
        .await
        .expect("sync python fixture");
    assert_eq!(report.scanned, 1);
    assert_eq!(report.changed, 1);

    let status = &store.status(Some(&repository_id)).await.unwrap()[0];
    assert_eq!(status.files, 1);
    assert!(
        status.symbols >= 2,
        "expected at least 2 symbols, got {}",
        status.symbols
    );

    let db = connect(&dsn).await;
    let kinds: Vec<String> = db
        .query(
            "SELECT DISTINCT kind FROM code_index.symbol WHERE repository_id = $1 ORDER BY kind",
            &[&repository_id],
        )
        .await
        .unwrap()
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert!(kinds.contains(&"function".to_string()));
    assert!(kinds.contains(&"struct".to_string()));

    db.execute(
        "DELETE FROM code_index.repository WHERE id = $1",
        &[&repository_id],
    )
    .await
    .expect("clean up python repository");
}

#[tokio::test]
async fn shell_function_definitions_are_indexed() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let root = tempdir().unwrap();
    fs::write(
        root.path().join("script.sh"),
        "helper() { echo ok; }\nmain() { helper; }\n",
    )
    .unwrap();
    let repository_id = unique_id("code-shell");
    let configured = source_with_language(&repository_id, root.path(), CodeLanguage::Shell);
    let mut store = initialized_store(&dsn).await;
    let report = sync_repository(&mut store, &configured)
        .await
        .expect("sync shell fixture");
    assert_eq!(report.scanned, 1);
    assert_eq!(report.changed, 1);

    let status = &store.status(Some(&repository_id)).await.unwrap()[0];
    assert_eq!(status.files, 1);
    assert!(
        status.symbols >= 1,
        "expected at least 1 symbol, got {}",
        status.symbols
    );

    let db = connect(&dsn).await;
    let kinds: Vec<String> = db
        .query(
            "SELECT DISTINCT kind FROM code_index.symbol WHERE repository_id = $1 ORDER BY kind",
            &[&repository_id],
        )
        .await
        .unwrap()
        .into_iter()
        .map(|row| row.get(0))
        .collect();
    assert!(kinds.contains(&"function".to_string()));

    db.execute(
        "DELETE FROM code_index.repository WHERE id = $1",
        &[&repository_id],
    )
    .await
    .expect("clean up shell repository");
}

/// An index created before python/shell existed still carries `CHECK (language IN ('rust'))`.
/// `CREATE TABLE IF NOT EXISTS` never revisits it, so widening the literal in SCHEMA reaches new
/// databases only — and every test that starts from a fresh container passes while the owner's
/// actual index rejects the first python repository it is handed. This test starts from the old
/// constraint on purpose: it is the only place that can fail.
#[tokio::test]
async fn initialize_widens_a_pre_existing_rust_only_language_check() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    // Reach the current schema first, then put the pre-widening constraint back to simulate an
    // index built before this change shipped.
    initialized_store(&dsn).await;
    let db = connect(&dsn).await;
    db.batch_execute(
        "ALTER TABLE code_index.repository DROP CONSTRAINT IF EXISTS repository_language_check;
         ALTER TABLE code_index.repository ADD CONSTRAINT repository_language_check
             CHECK (language IN ('rust'));",
    )
    .await
    .expect("install the legacy rust-only constraint");

    let repository_id = unique_id("legacy-check");
    let legacy_insert = db
        .execute(
            "INSERT INTO code_index.repository (id, name, root_path, language, last_synced_at)
             VALUES ($1, $1, '/tmp', 'python', now())",
            &[&repository_id],
        )
        .await;
    assert!(
        legacy_insert.is_err(),
        "the legacy constraint must reject python — otherwise this test proves nothing"
    );

    // The migration must repair it without anyone running SQL by hand.
    initialized_store(&dsn).await;
    db.execute(
        "INSERT INTO code_index.repository (id, name, root_path, language, last_synced_at)
         VALUES ($1, $1, '/tmp', 'python', now())",
        &[&repository_id],
    )
    .await
    .expect("initialize() must widen the language check on an existing index");

    db.execute(
        "DELETE FROM code_index.repository WHERE id = $1",
        &[&repository_id],
    )
    .await
    .expect("clean up legacy-check repository");
}

#[tokio::test]
async fn python_under_claude_and_venv_directories_is_not_indexed() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let root = tempdir().unwrap();
    fs::write(root.path().join("app.py"), "def kept():\n    pass\n").unwrap();
    for ignored in [".claude/worktrees/agent-x", ".venv/lib"] {
        let dir = root.path().join(ignored);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("dep.py"), "def skipped():\n    pass\n").unwrap();
    }
    let repository_id = unique_id("code-ignored-dirs");
    let configured = source_with_language(&repository_id, root.path(), CodeLanguage::Python);
    let mut store = initialized_store(&dsn).await;
    let report = sync_repository(&mut store, &configured)
        .await
        .expect("sync ignored-dir fixture");
    assert_eq!(report.scanned, 1, "only app.py may be scanned");
    assert_eq!(
        store.status(Some(&repository_id)).await.unwrap()[0].files,
        1
    );

    let db = connect(&dsn).await;
    db.execute(
        "DELETE FROM code_index.repository WHERE id = $1",
        &[&repository_id],
    )
    .await
    .expect("clean up test repository");
}

async fn index_state(db: &Client, name: &str) -> Option<(u32, bool)> {
    db.query_opt(
        "SELECT c.oid, i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
          WHERE c.relnamespace = 'code_index'::regnamespace AND c.relname = $1",
        &[&name],
    )
    .await
    .unwrap()
    .map(|row| (row.get(0), row.get(1)))
}

#[tokio::test]
async fn an_invalid_file_id_index_is_dropped_and_recreated_valid() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let store = initialized_store(&dsn).await;
    for name in [
        "code_index_symbol_file",
        "code_index_relation_file",
        "code_index_relation_source_symbol",
        "code_index_relation_target_symbol",
    ] {
        let (before_oid, valid) = index_state(&db, name)
            .await
            .expect("index missing after initialize");
        assert!(valid, "{name} must be valid after a fresh initialize");
        db.execute(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = $1::oid",
            &[&before_oid],
        )
        .await
        .expect("mark index invalid");

        store.initialize().await.expect("re-initialize");
        let (after_oid, valid) = index_state(&db, name).await.expect("recreated");
        assert!(valid, "{name} still invalid after initialize");
        assert_ne!(before_oid, after_oid, "{name} was not rebuilt");
    }
}

/// Seeds `background` unrelated relation rows plus one file whose `symbols` symbols are each the
/// source and target of one relation, deletes that file, and returns microseconds per symbol.
async fn delete_file_micros_per_symbol(db: &Client, background: i64, symbols: i64) -> f64 {
    let repo = unique_id("code-symdel");
    let bg_file = format!("{repo}-bg");
    let victim_file = format!("{repo}-victim");
    db.execute(
        "INSERT INTO code_index.repository (id, name, root_path, language, last_synced_at)
         VALUES ($1, $1, '/x', 'rust', now())",
        &[&repo],
    )
    .await
    .unwrap();
    for file in [&bg_file, &victim_file] {
        db.execute(
            "INSERT INTO code_index.file
               (id, repository_id, relative_path, sha256, parse_status, error_count, updated_at)
             VALUES ($1, $2, $1, 'x', 'parsed', 0, now())",
            &[file, &repo],
        )
        .await
        .unwrap();
    }
    for (file, count) in [(&bg_file, 1_i64), (&victim_file, symbols)] {
        db.execute(
            "INSERT INTO code_index.symbol
               (id, repository_id, file_id, kind, name, qualified_name,
                start_byte, end_byte, start_line, end_line)
             SELECT $1 || '-s' || g, $2, $1, 'function', 'n', 'n', 0, 1, 1, 1
               FROM generate_series(1, $3::bigint) g",
            &[file, &repo, &count],
        )
        .await
        .unwrap();
    }
    db.execute(
        "INSERT INTO code_index.relation
           (id, repository_id, file_id, source_symbol_id, kind, target_symbol_id,
            start_byte, end_byte)
         SELECT $1 || '-bg' || g, $2, $1, $1 || '-s1', 'calls', $1 || '-s1', 0, 1
           FROM generate_series(1, $3::bigint) g",
        &[&bg_file, &repo, &background],
    )
    .await
    .unwrap();
    db.execute(
        "INSERT INTO code_index.relation
           (id, repository_id, file_id, source_symbol_id, kind, target_symbol_id,
            start_byte, end_byte)
         SELECT $1 || '-r' || g, $3, $1, $1 || '-s' || g, 'calls', $1 || '-s' || g, 0, 1
           FROM generate_series(1, $2::bigint) g",
        &[&victim_file, &symbols, &repo],
    )
    .await
    .unwrap();
    db.batch_execute("ANALYZE code_index.relation; ANALYZE code_index.symbol")
        .await
        .unwrap();

    let started = std::time::Instant::now();
    db.execute("DELETE FROM code_index.file WHERE id = $1", &[&victim_file])
        .await
        .unwrap();
    let micros = started.elapsed().as_secs_f64() * 1e6 / f64::from(u32::try_from(symbols).unwrap());

    db.execute("DELETE FROM code_index.repository WHERE id = $1", &[&repo])
        .await
        .unwrap();
    micros
}

#[tokio::test]
async fn deleting_a_file_costs_the_same_per_symbol_whatever_the_relation_table_size() {
    let Some(dsn) = test_dsn() else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let db = connect(&dsn).await;
    let _store = initialized_store(&dsn).await;
    let small = delete_file_micros_per_symbol(&db, 20_000, 200).await;
    let large = delete_file_micros_per_symbol(&db, 400_000, 200).await;
    eprintln!(
        "per-symbol delete: small={small:.1}us large={large:.1}us ratio={:.1}",
        large / small
    );
    assert!(
        large < small * 3.0,
        "per-symbol delete grew with relation size: {small:.1}us -> {large:.1}us"
    );
}
