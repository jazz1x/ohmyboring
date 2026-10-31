//! C-VECADAPT2 2b: on `backend=surreal` every write that changes a chunk or an open claim in pg is
//! written into SurrealDB too, and a Surreal failure is an error that says pg already holds the
//! change.
//!
//! Needs a throwaway pgvector (its document and claim tables are emptied on each run) and a
//! throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_writes -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::{BoringConfig, SurrealConfig};
use drudge::frontmatter::FrontMatter;
use drudge::ingest::{DefaultChunker, Embed, FrontmatterGraphExtractor, ingest_file_with};
use drudge::store::{Doc, Store, SurrealClient, VectorRoute};
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;
/// The sentence 2b-2 requires in the error, quoted here so a reworded message turns this red.
const BEHIND: &str =
    "pg 에는 기록됨, Surreal 만 빠짐. 같은 요청을 다시 보내지 말고 채우기를 다시 돌릴 것";

fn vec_at(i: usize) -> Vec<f32> {
    let mut v = vec![0.0; DIM];
    v[i] = 1.0;
    v
}

struct ZeroEmbed;

impl Embed for ZeroEmbed {
    fn embed(
        &self,
        _text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        std::future::ready(Ok(vec![0.5_f32; DIM]))
    }
}

fn client_at(url: &str, namespace: &str) -> SurrealClient {
    let config: SurrealConfig =
        serde_json::from_value(json!({"url": url, "namespace": namespace, "database": namespace}))
            .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

struct Fixture {
    store: Store,
    client: SurrealClient,
    db: tokio_postgres::Client,
    nanos: u128,
}

/// Empty pg, empty Surreal tables (one fill of nothing defines them), a store routed to Surreal.
async fn fixture(namespace: &str) -> Fixture {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    let url = std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL");
    let client = client_at(&url, namespace);
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    let tables = client.fill_from(&pg).await.expect("fill of an empty pg");
    assert!(tables.iter().all(drudge::store::FillTable::same));
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    Fixture {
        store: pg.with_vector_route(VectorRoute::Surreal(client.clone())),
        client,
        db,
        nanos,
    }
}

fn front(path: &str) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_string(),
        project: "writes".to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("2b".to_string()),
        ..Default::default()
    }
}

impl Fixture {
    async fn doc(&self, path: &str) {
        self.store
            .upsert_document(&front(path), "sha", SystemTime::now())
            .await
            .expect("document");
    }

    async fn chunk(&self, path: &str, idx: usize) {
        self.store
            .upsert_chunk(&Doc {
                id: format!("{path}#{idx}"),
                content: format!("chunk {idx}"),
                embedding: vec_at(idx + 1),
                front: front(path),
                chunk_idx: idx,
            })
            .await
            .expect("chunk");
    }

    async fn claim(&self, path: &str, subject: &str, kind: &str, hours_ago: u64) {
        self.store
            .upsert_claim(
                subject,
                "observed",
                &format!("a long enough value from {path}"),
                path,
                SystemTime::now() - Duration::from_hours(hours_ago),
                &vec_at(7),
                kind,
                "certain",
            )
            .await
            .expect("claim");
    }

    /// (pg ids, Surreal ids) of chunks, then of open claims — each side sorted, as the strings the
    /// fill keys them by.
    async fn id_sets(&self) -> [(Vec<String>, Vec<String>); 2] {
        let pg_chunks: Vec<String> = self
            .db
            .query("SELECT id FROM chunk ORDER BY id COLLATE \"C\"", &[])
            .await
            .unwrap()
            .iter()
            .map(|r| r.get(0))
            .collect();
        let mut pg_claims: Vec<String> = self
            .db
            .query(
                "SELECT subject || '|' || predicate || '|' ||
                        (extract(epoch from valid_from) * 1000000)::bigint
                 AS k FROM claim WHERE superseded_at IS NULL",
                &[],
            )
            .await
            .unwrap()
            .iter()
            .map(|r| r.get(0))
            .collect();
        pg_claims.sort();
        let surreal = |rows: Vec<Value>| -> Vec<Vec<Value>> {
            serde_json::from_value(Value::Array(rows)).unwrap()
        };
        let surreal_chunks = self
            .client
            .run("SELECT VALUE cid FROM chunk ORDER BY cid;")
            .await
            .unwrap();
        let surreal_claims = self
            .client
            .run("SELECT subject, predicate, valid_from_us FROM claim;")
            .await
            .unwrap();
        let mut chunks: Vec<String> = surreal(surreal_chunks)[0]
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        chunks.sort();
        let mut claims: Vec<String> = surreal(surreal_claims)[0]
            .iter()
            .map(|v| {
                format!(
                    "{}|{}|{}",
                    v["subject"].as_str().unwrap(),
                    v["predicate"].as_str().unwrap(),
                    v["valid_from_us"]
                )
            })
            .collect();
        claims.sort();
        [(pg_chunks, chunks), (pg_claims, claims)]
    }

    /// Every document as one line `path|origin|project|kind|title|sha|author`, pg then Surreal.
    async fn document_rows(&self) -> (Vec<String>, Vec<String>) {
        let mut pg: Vec<String> = self
            .db
            .query(
                "SELECT source_path || '|' || origin || '|' || project || '|' || kind || '|' ||
                        coalesce(title, '-') || '|' || sha || '|' || author || '|' ||
                        array_to_string(tags, ',') FROM document",
                &[],
            )
            .await
            .unwrap()
            .iter()
            .map(|r| r.get(0))
            .collect();
        pg.sort();
        let rows = self.client.run("SELECT * FROM document;").await.unwrap();
        let rows: Vec<Vec<Value>> = serde_json::from_value(Value::Array(rows)).unwrap();
        let mut surreal: Vec<String> = rows[0]
            .iter()
            .map(|v| {
                let s = |k: &str| v[k].as_str().unwrap_or("-").to_string();
                let tags: Vec<&str> = v["tags"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|t| t.as_str().unwrap())
                    .collect();
                format!(
                    "{}|{}|{}|{}|{}|{}|{}|{}",
                    s("source_path"),
                    s("origin"),
                    s("project"),
                    s("kind"),
                    s("title"),
                    s("sha"),
                    s("author"),
                    tags.join(",")
                )
            })
            .collect();
        surreal.sort();
        (pg, surreal)
    }

    /// Every chunk as `id|origin|project|content`, pg then Surreal.
    async fn chunk_text_rows(&self) -> (Vec<String>, Vec<String>) {
        let mut pg: Vec<String> = self
            .db
            .query(
                "SELECT id || '|' || origin || '|' || project || '|' || content FROM chunk",
                &[],
            )
            .await
            .unwrap()
            .iter()
            .map(|r| r.get(0))
            .collect();
        pg.sort();
        let rows = self.client.run("SELECT * FROM chunk;").await.unwrap();
        let rows: Vec<Vec<Value>> = serde_json::from_value(Value::Array(rows)).unwrap();
        let mut surreal: Vec<String> = rows[0]
            .iter()
            .map(|v| {
                format!(
                    "{}|{}|{}|{}",
                    v["cid"].as_str().unwrap(),
                    v["origin"].as_str().unwrap(),
                    v["project"].as_str().unwrap(),
                    v["content"].as_str().unwrap()
                )
            })
            .collect();
        surreal.sort();
        (pg, surreal)
    }

    async fn assert_same(&self, step: &str, chunks: usize, claims: usize) {
        let [(pg_chunks, s_chunks), (pg_claims, s_claims)] = self.id_sets().await;
        assert_eq!(pg_chunks, s_chunks, "{step}: chunk ids differ");
        assert_eq!(pg_claims, s_claims, "{step}: open claim ids differ");
        assert_eq!(pg_chunks.len(), chunks, "{step}: chunk count {pg_chunks:?}");
        assert_eq!(pg_claims.len(), claims, "{step}: claim count {pg_claims:?}");
        let (pg_docs, surreal_docs) = self.document_rows().await;
        assert_eq!(pg_docs, surreal_docs, "{step}: document rows differ");
        let (pg_text, surreal_text) = self.chunk_text_rows().await;
        assert_eq!(pg_text, surreal_text, "{step}: chunk content/origin differ");
        let tables = self.client.compare(&self.store).await.unwrap();
        assert!(
            tables.iter().all(drudge::store::FillTable::same),
            "{step}: compare says different: {tables:?}"
        );
    }
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn upsert_supersede_and_delete_leave_the_two_stores_with_the_same_ids() {
    let f = fixture("w_ids").await;
    let a = format!("/vault/wiki/w-{}-a.md", f.nanos);
    let b = format!("/vault/wiki/w-{}-b.md", f.nanos);
    let slot = format!("slot-{}", f.nanos);

    // upsert: two chunks, an open fact and an open risk
    f.doc(&a).await;
    f.chunk(&a, 0).await;
    f.chunk(&a, 1).await;
    f.claim(&a, &slot, "fact", 48).await;
    f.claim(&a, &format!("{slot}-risk"), "risk", 48).await;
    f.assert_same("after upsert", 2, 2).await;

    // the sha-only move (legacy sha -> content hash) reaches the Surreal document too
    f.store.set_doc_sha(&a, "sha-moved").await.expect("sha");
    f.assert_same("after set_doc_sha", 2, 2).await;

    // supersede by a later fact in the same slot, from another note: pg seals a's fact
    f.doc(&b).await;
    f.chunk(&b, 0).await;
    f.claim(&b, &slot, "fact", 1).await;
    f.assert_same("after the slot's fact was superseded", 3, 2)
        .await;

    // supersede by an edge: b retires a, which seals a's remaining open claim
    f.store
        .supersedes_in_tx(&[[b.clone(), a.clone()]], None)
        .await
        .expect("supersedes");
    f.assert_same("after b superseded a", 3, 1).await;

    // delete: a chunk tail, then a document (its chunks go by CASCADE, its claims explicitly)
    f.store.prune_chunks_from(&a, 1).await.expect("prune tail");
    f.assert_same("after pruning a#1", 2, 1).await;
    f.store.delete_document(&a).await.expect("delete a");
    f.assert_same("after deleting a (CASCADE chunks)", 1, 1)
        .await;
    f.store.delete_document(&b).await.expect("delete b");
    f.assert_same("after deleting b", 0, 0).await;
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn sync_of_a_note_file_writes_surreal_too() {
    let f = fixture("w_sync").await;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join(format!("w-{}.md", f.nanos));
    let tag = format!("w{}", f.nanos);
    std::fs::write(
        &path,
        format!(
            "---\norigin: personal\nproject: \"\"\nkind: note\ntags:\n  - {tag}\nclaims:\n  - subject: {tag}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain\n---\n\nbody {tag}\n"
        ),
    )
    .unwrap();
    let cfg = BoringConfig::from_env();
    ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &f.store,
        &ZeroEmbed,
        &cfg,
        &path.to_string_lossy(),
    )
    .await
    .expect("ingest");
    f.assert_same("after sync of one note", 1, 1).await;
}

impl Fixture {
    /// Every node as `id|kind|label|outcome` and every edge as `src|dst|kind|judge`, pg then
    /// Surreal, sorted.
    async fn graph_rows(&self) -> [(Vec<String>, Vec<String>); 2] {
        let pg = |sql: &'static str| async move {
            let mut rows: Vec<String> = self
                .db
                .query(sql, &[])
                .await
                .unwrap()
                .iter()
                .map(|r| r.get(0))
                .collect();
            rows.sort();
            rows
        };
        let surreal = |sql: &'static str, cols: &'static [&'static str]| async move {
            let rows = self.client.run(sql).await.unwrap();
            let rows: Vec<Vec<Value>> = serde_json::from_value(Value::Array(rows)).unwrap();
            let mut out: Vec<String> = rows[0]
                .iter()
                .map(|v| {
                    cols.iter()
                        .map(|c| v[*c].as_str().unwrap_or("-").to_string())
                        .collect::<Vec<_>>()
                        .join("|")
                })
                .collect();
            out.sort();
            out
        };
        [
            (
                pg("SELECT id || '|' || kind || '|' || label || '|' || coalesce(outcome, '-') FROM node")
                    .await,
                surreal("SELECT * FROM node;", &["cid", "kind", "label", "outcome"]).await,
            ),
            (
                pg("SELECT src || '|' || dst || '|' || kind || '|' || coalesce(judge, '-') FROM edge")
                    .await,
                surreal("SELECT * FROM edge;", &["src", "dst", "kind", "judge"]).await,
            ),
        ]
    }

    async fn assert_graph_same(&self, step: &str, nodes: usize, edges: usize) {
        let [(pg_nodes, s_nodes), (pg_edges, s_edges)] = self.graph_rows().await;
        assert_eq!(pg_nodes, s_nodes, "{step}: node rows differ");
        assert_eq!(pg_edges, s_edges, "{step}: edge rows differ");
        assert_eq!(pg_nodes.len(), nodes, "{step}: node count {pg_nodes:?}");
        assert_eq!(pg_edges.len(), edges, "{step}: edge count {pg_edges:?}");
        let tables = self.client.compare(&self.store).await.unwrap();
        assert!(
            tables.iter().all(drudge::store::FillTable::same),
            "{step}: compare says different: {tables:?}"
        );
    }
}

/// C-SURALL2 A2: node and edge follow every pg write that changes them, by value.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn node_and_edge_writes_leave_the_two_stores_with_the_same_rows() {
    let f = fixture("w_graph").await;
    f.db.batch_execute("DELETE FROM edge; DELETE FROM node;")
        .await
        .unwrap();
    f.client
        .fill_from(&f.store)
        .await
        .expect("fill of an empty graph");
    let a = format!("/vault/wiki/g-{}-a.md", f.nanos);
    let b = format!("/vault/wiki/g-{}-b.md", f.nanos);

    // document: project node + in_project edge, topic nodes + tagged edges
    let mut fm = front(&a);
    fm.tags = vec!["t1".to_string(), "t2".to_string()];
    f.store
        .upsert_document(&fm, "sha", SystemTime::now())
        .await
        .expect("document with tags");
    f.assert_graph_same("after a tagged document", 3, 3).await;

    // a re-written document with one tag fewer: the dropped tagged edge leaves Surreal too
    fm.tags = vec!["t1".to_string()];
    f.store
        .upsert_document(&fm, "sha2", SystemTime::now())
        .await
        .expect("document with fewer tags");
    f.assert_graph_same("after a tag was dropped", 3, 2).await;

    // semantic entities
    f.store.upsert_tool("tl", "Tool").await.unwrap();
    f.store.upsert_concept("cn", "Concept").await.unwrap();
    f.store.relate_doc_tool(&a, "tl").await.unwrap();
    f.store.relate_doc_concept(&a, "cn").await.unwrap();
    f.assert_graph_same("after uses/about", 5, 4).await;
    f.store.clear_semantic_edges(&a).await.unwrap();
    f.assert_graph_same("after clear_semantic_edges", 5, 2)
        .await;

    // a non-fact claim: +3 nodes (claim, typed, owner speaker) and +5 edges (is_a, claims,
    // claim_of_project, said speaker→doc, said speaker→claim) on top of 5 nodes / 2 edges
    f.claim(&a, &format!("g-{}", f.nanos), "decision", 1).await;
    f.store
        .upsert_claim_node(
            &a,
            "writes",
            &format!("g-{}", f.nanos),
            "observed",
            &drudge::frontmatter::Claim {
                subject: format!("g-{}", f.nanos),
                predicate: "observed".to_string(),
                value: "a long enough value".to_string(),
                kind: "decision".to_string(),
                confidence: "high".to_string(),
                said_by: Some(drudge::frontmatter::SaidBy::Owner),
            },
        )
        .await
        .expect("claim node");
    f.assert_graph_same("after a decision claim", 8, 7).await;

    // consumption doors: session, gap, supersedes edge with a judge
    f.doc(&b).await;
    f.store
        .handover_in_tx(
            &format!("s-{}", f.nanos),
            "2026-10-30",
            std::slice::from_ref(&a),
        )
        .await
        .expect("handover");
    f.store
        .record_gap_in_tx(
            &drudge::gap::GapArgs {
                session_id: format!("s-{}", f.nanos),
                query: "what".to_string(),
                kind: drudge::gap::Kind::Missing,
                handed: vec![a.clone()],
            },
            "2026-10-30",
        )
        .await
        .expect("gap");
    f.store
        .supersedes_in_tx(&[[b.clone(), a.clone()]], Some("owner"))
        .await
        .expect("supersedes");
    let [(_, s_nodes), (_, s_edges)] = f.graph_rows().await;
    assert!(
        s_edges.iter().any(|e| e.ends_with("|supersedes|owner")),
        "judge must reach Surreal: {s_edges:?}"
    );
    assert!(s_nodes.iter().any(|n| n.starts_with("gap:")), "{s_nodes:?}");

    // delete + GC: the note's edges leave, then the orphans nobody points at leave
    f.store.delete_document(&a).await.expect("delete a");
    f.store.delete_document(&b).await.expect("delete b");
    f.store.gc_orphans().await.expect("gc");
    let [(pg_nodes, s_nodes), (pg_edges, s_edges)] = f.graph_rows().await;
    assert_eq!(pg_nodes, s_nodes, "after gc: node rows differ");
    assert_eq!(pg_edges, s_edges, "after gc: edge rows differ");
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn a_legacy_sha_move_reaches_the_surreal_document() {
    use drudge::ingest::FileOutcome;
    use sha2::Digest;
    let f = fixture("w_sha").await;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join(format!("w-{}.md", f.nanos));
    let path_s = path.to_string_lossy().into_owned();
    // `relates_to` is a derived key, so the content hash differs from the whole-file hash.
    let text = "---\norigin: personal\nproject: \"\"\nkind: note\nrelates_to:\n  - x\n---\n\nbody for the sha move\n";
    std::fs::write(&path, text).unwrap();
    let cfg = BoringConfig::from_env();
    let ingest = || {
        ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
            &f.store, &ZeroEmbed, &cfg, &path_s,
        )
    };
    let (first, _) = ingest().await.expect("first ingest");
    assert_eq!(first, FileOutcome::New);

    // The old stored sha: the whole file's hash. pg and Surreal both hold it before the move.
    let legacy = hex::encode(sha2::Sha256::digest(text.as_bytes()));
    f.db.execute(
        "UPDATE document SET sha = $2 WHERE source_path = $1",
        &[&path_s, &legacy],
    )
    .await
    .unwrap();
    f.store
        .mirror_surreal(&drudge::store::Scope::notes([path_s.clone()]))
        .await
        .expect("mirror the legacy sha");
    f.assert_same("legacy sha in both stores", 1, 0).await;

    let (second, _) = ingest().await.expect("second ingest");
    assert_eq!(second, FileOutcome::ShaMoved);
    f.assert_same("after the sha move", 1, 0).await;
    let moved: String =
        f.db.query_one(
            "SELECT sha FROM document WHERE source_path = $1",
            &[&path_s],
        )
        .await
        .unwrap()
        .get(0);
    assert_ne!(
        moved, legacy,
        "pg sha must have moved off the whole-file hash"
    );
}

/// Rows the Python door writes straight to pg (no mirror there) reach Surreal on the next sync:
/// the catch-up the sync ends with writes what pg has and Surreal lacks, deletes the reverse.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn rows_written_straight_to_pg_reach_surreal_on_catch_up() {
    let f = fixture("w_catchup").await;
    f.db.batch_execute("DELETE FROM edge; DELETE FROM node;")
        .await
        .unwrap();
    f.client
        .fill_from(&f.store)
        .await
        .expect("fill of an empty graph");
    let path = format!("/vault/wiki/py-{}.md", f.nanos);
    f.db.batch_execute(&format!(
        "INSERT INTO document (source_path, origin, project, kind, title, tags, sha, updated_at, author)
           VALUES ('{path}', 'personal', 'writes', 'note', 'py', '{{}}', 'sha', now(), 'inferred');
         INSERT INTO node (id, kind, label, outcome) VALUES ('gap:py-{n}', 'gap', 'q', 'missing'),
                                                            ('session:py-{n}', 'session', 't', NULL);
         INSERT INTO edge (src, dst, kind, judge) VALUES ('session:py-{n}', 'gap:py-{n}', 'gap', NULL),
                                                         ('gap:py-{n}', 'doc:{path}', 'gap_handed', 'owner');",
        n = f.nanos
    ))
    .await
    .unwrap();
    let (written, deleted) = f.store.catch_up_surreal().await.expect("catch up");
    assert_eq!((written, deleted), (5, 0));
    f.assert_graph_same("after catch-up of python-door rows", 2, 2)
        .await;
    let (pg_docs, surreal_docs) = f.document_rows().await;
    assert_eq!(pg_docs, surreal_docs, "document rows differ");

    // a row only Surreal holds leaves on the next catch-up
    f.db.batch_execute("DELETE FROM edge; DELETE FROM node;")
        .await
        .unwrap();
    let (written, deleted) = f.store.catch_up_surreal().await.expect("catch up again");
    assert_eq!((written, deleted), (0, 4));
    f.assert_graph_same("after pg dropped the rows", 0, 0).await;
}

#[tokio::test]
#[ignore = "needs throwaway pg: see the run command in this file's header"]
async fn a_surreal_write_failure_is_an_error_that_says_pg_has_it() {
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").expect("BORING_TEST_DATABASE_URL");
    // Nothing listens on port 1: every Surreal request fails after pg has committed.
    let dead = client_at("http://127.0.0.1:1", "w_dead");
    let store = Store::open(&dsn, DIM)
        .await
        .expect("open store")
        .with_vector_route(VectorRoute::Surreal(dead));
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute("DELETE FROM claim; DELETE FROM document;")
        .await
        .unwrap();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = format!("/vault/wiki/w-dead-{nanos}.md");
    let err = store
        .upsert_document(&front(&path), "sha", SystemTime::now())
        .await
        .expect_err("a dead Surreal must be an error on the document write too");
    assert!(
        err.to_string().contains(BEHIND),
        "document error must carry the 2b-2 sentence, got: {err:#}"
    );
    let doc_in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM document WHERE source_path = $1",
            &[&path],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(doc_in_pg, 1, "the sentence says pg has the document");

    let session = format!("s-dead-{nanos}");
    let err = store
        .handover_in_tx(&session, "2026-10-30", std::slice::from_ref(&path))
        .await
        .expect_err("a graph-only write must not swallow a dead Surreal either");
    assert!(
        err.to_string().contains(BEHIND),
        "graph error must carry the sentence, got: {err:#}"
    );
    let node_in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM node WHERE id = $1",
            &[&format!("session:{session}")],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(node_in_pg, 1, "the sentence says pg has the session node");

    let err = store
        .upsert_chunk(&Doc {
            id: format!("{path}#0"),
            content: "chunk".to_string(),
            embedding: vec_at(1),
            front: front(&path),
            chunk_idx: 0,
        })
        .await
        .expect_err("a dead Surreal must be an error, not a silent pg-only write");
    assert!(
        err.to_string().contains(BEHIND),
        "error must carry the 2b-2 sentence, got: {err:#}"
    );
    let in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM chunk WHERE id = $1",
            &[&format!("{path}#0")],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(in_pg, 1, "the sentence says pg has it, so pg must have it");

    // The same on the sync path: the file's result carries the error, pg holds the note.
    let dir = tempfile::tempdir().unwrap();
    let note = dir.path().join(format!("w-dead-{nanos}.md"));
    std::fs::write(
        &note,
        "---\norigin: personal\nproject: \"\"\nkind: note\n---\n\nbody for the dead surreal\n",
    )
    .unwrap();
    let cfg = BoringConfig::from_env();
    let note_path = note.to_string_lossy().into_owned();
    let err = ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor, _>(
        &store, &ZeroEmbed, &cfg, &note_path,
    )
    .await
    .expect_err("sync result must carry the Surreal failure");
    assert!(
        err.to_string().contains(BEHIND),
        "sync error must carry the 2b-2 sentence, got: {err:#}"
    );
    let note_rows: i64 = db
        .query_one(
            "SELECT count(*) FROM document WHERE source_path = $1",
            &[&note_path],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(note_rows, 1, "pg committed the note before Surreal failed");
}
