//! C-VECADAPT2 2b, as C0b1 bundles 1-3 left it: on `backend=surreal` document, chunk, node, edge,
//! claim and claim_row writes go straight into SurrealDB and pg receives none of them. Each test
//! writes the same thing through a pgvector store (the oracle) and the Surreal-routed store, then
//! compares the two.
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
use drudge::store::{Doc, NoteChunk, NoteWrite, Store, SurrealClient, VectorRoute};
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;
/// The sentence 2b-2 requires in the error, quoted here so a reworded message turns this red.
const BEHIND: &str =
    "pg 에는 기록됨, Surreal 만 빠짐. 같은 요청을 다시 보내지 말고 채우기를 다시 돌릴 것";

/// The sentence a failed node/edge write carries: those rows have no pg copy to fill from.
const GRAPH_FAILED: &str = "Surreal 그래프(node·edge) 쓰기 실패";

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
    /// The same pg without the Surreal route: the oracle. Whatever a test writes through `store`
    /// (Surreal only) it writes through this one too, so the two can be compared.
    pg_store: Store,
    client: SurrealClient,
    db: tokio_postgres::Client,
    nanos: u128,
    /// path -> (front matter, sha, chunk indexes): what each note holds, so every write can send
    /// Surreal the note's whole set (`write_note`). pg gets the same rows through `pg_store`, which
    /// stays the oracle the comparisons read.
    notes: std::sync::Mutex<std::collections::BTreeMap<String, HeldNote>>,
}

type HeldNote = (FrontMatter, String, Vec<usize>);

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
    let pg_store = Store::open(&dsn, DIM).await.expect("open store");
    Fixture {
        pg_store,
        store: pg.with_vector_route(VectorRoute::Surreal(client.clone())),
        client,
        db,
        nanos,
        notes: std::sync::Mutex::new(std::collections::BTreeMap::new()),
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
    /// Send Surreal the note as the fixture holds it (document + all its chunks, one transaction),
    /// then its graph half.
    async fn put(&self, path: &str) {
        let (fm, sha, positions) = self.notes.lock().unwrap().get(path).cloned().unwrap();
        let ids: Vec<String> = positions.iter().map(|i| format!("{path}#{i}")).collect();
        let contents: Vec<String> = positions.iter().map(|i| format!("chunk {i}")).collect();
        let embeddings: Vec<Vec<f32>> = positions.iter().map(|i| vec_at(i + 1)).collect();
        let chunks: Vec<NoteChunk<'_>> = (0..positions.len())
            .map(|k| NoteChunk {
                id: &ids[k],
                content: &contents[k],
                embedding: &embeddings[k],
            })
            .collect();
        let author = String::from(fm.author.clone());
        self.client
            .write_note(&NoteWrite {
                path,
                origin: &fm.origin,
                project: &fm.project,
                kind: &fm.kind,
                title: fm.title.as_deref(),
                tags: &fm.tags,
                sha: &sha,
                author: &author,
                updated_us: 1_700_000_000_000_000,
                chunks: &chunks,
                dim: DIM,
            })
            .await
            .expect("surreal document and chunks");
        // C0b1 bundle 2: the graph half goes into Surreal directly, as `write_note_on` does.
        Store::upsert_document_graph_on(
            self.store.doc_lookup(),
            &self.store.conn().await.expect("conn"),
            &fm,
        )
        .await
        .expect("surreal graph");
    }

    async fn doc(&self, path: &str) {
        self.doc_fm(&front(path), "sha").await;
    }

    async fn doc_fm(&self, fm: &FrontMatter, sha: &str) {
        self.pg_store
            .upsert_document(fm, sha, SystemTime::now())
            .await
            .expect("pg document");
        let path = fm.source_path.clone();
        self.notes
            .lock()
            .unwrap()
            .entry(path.clone())
            .and_modify(|n| {
                n.0 = fm.clone();
                sha.clone_into(&mut n.1);
            })
            .or_insert((fm.clone(), sha.to_owned(), Vec::new()));
        self.put(&path).await;
    }

    async fn chunk(&self, path: &str, idx: usize) {
        self.pg_store
            .upsert_chunk(&Doc {
                id: format!("{path}#{idx}"),
                content: format!("chunk {idx}"),
                embedding: vec_at(idx + 1),
                front: front(path),
                chunk_idx: idx,
            })
            .await
            .expect("pg chunk");
        self.notes
            .lock()
            .unwrap()
            .get_mut(path)
            .unwrap()
            .2
            .push(idx);
        self.put(path).await;
    }

    async fn set_sha(&self, path: &str, sha: &str) {
        self.pg_store.set_doc_sha(path, sha).await.expect("pg sha");
        self.store
            .set_doc_sha(path, sha)
            .await
            .expect("surreal sha");
        sha.clone_into(&mut self.notes.lock().unwrap().get_mut(path).unwrap().1);
    }

    async fn prune(&self, path: &str, from: usize) {
        self.pg_store
            .prune_chunks_from(path, from)
            .await
            .expect("pg prune");
        self.notes
            .lock()
            .unwrap()
            .get_mut(path)
            .unwrap()
            .2
            .retain(|i| *i < from);
        self.put(path).await;
    }

    async fn set_legacy_sha(&self, path: &str, sha: &str) {
        self.store
            .set_doc_sha(path, sha)
            .await
            .expect("surreal sha");
    }

    async fn surreal_sha(&self, path: &str) -> String {
        let rows = self
            .client
            .run(&format!(
                "SELECT VALUE sha FROM document WHERE source_path = \"{path}\";"
            ))
            .await
            .unwrap();
        let rows: Vec<Vec<Value>> = serde_json::from_value(Value::Array(rows)).unwrap();
        rows[0][0].as_str().unwrap().to_owned()
    }

    async fn delete(&self, path: &str) {
        self.pg_store
            .delete_document(path)
            .await
            .expect("pg delete");
        self.store
            .delete_document(path)
            .await
            .expect("surreal delete");
        self.notes.lock().unwrap().remove(path);
    }

    /// The same claim through both routes (C0b1 bundle 3): `pg_store` writes pg, the oracle;
    /// `store` writes Surreal directly. Nothing copies one to the other.
    async fn claim(&self, path: &str, subject: &str, kind: &str, hours_ago: u64) {
        let valid_from = SystemTime::now() - Duration::from_hours(hours_ago);
        for s in [&self.pg_store, &self.store] {
            s.upsert_claim(
                subject,
                "observed",
                &format!("a long enough value from {path}"),
                path,
                valid_from,
                &vec_at(7),
                kind,
                "certain",
            )
            .await
            .expect("claim");
        }
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
        // Every write above went through both routes, so the compare is pg oracle against Surreal
        // for every table, claims included.
        let tables = self.client.compare(&self.store).await.unwrap();
        for t in &tables {
            assert!(t.same(), "{step}: compare says different: {tables:?}");
        }
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
    f.set_sha(&a, "sha-moved").await;
    f.assert_same("after set_doc_sha", 2, 2).await;

    // supersede by a later fact in the same slot, from another note: pg seals a's fact
    f.doc(&b).await;
    f.chunk(&b, 0).await;
    f.claim(&b, &slot, "fact", 1).await;
    f.assert_same("after the slot's fact was superseded", 3, 2)
        .await;

    // supersede by an edge: b retires a, which seals a's remaining open claim
    for s in [&f.pg_store, &f.store] {
        s.supersedes_in_tx(&[[b.clone(), a.clone()]], None)
            .await
            .expect("supersedes");
    }
    // b-seal: the edge and the seal run in Surreal on the Surreal route, in pg on the oracle.
    assert!(
        f.client
            .supersedes_edges()
            .await
            .unwrap()
            .contains(&(format!("doc:{b}"), format!("doc:{a}"))),
        "the supersedes edge is in Surreal"
    );
    f.assert_same("after b superseded a", 3, 1).await;

    // delete: a chunk tail, then a document (its chunks go by CASCADE, its claims explicitly)
    f.prune(&a, 1).await;
    f.assert_same("after pruning a#1", 2, 1).await;
    f.delete(&a).await;
    f.assert_same("after deleting a (CASCADE chunks)", 1, 1)
        .await;
    f.delete(&b).await;
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
    // New contract (C0b1 bundles 1-3): the engine wrote the note's document, chunk and claim into
    // Surreal only; pg holds none of them.
    let [(pg_chunks, s_chunks), (pg_claims, s_claims)] = f.id_sets().await;
    assert!(
        pg_chunks.is_empty(),
        "pg must not receive the chunk: {pg_chunks:?}"
    );
    assert_eq!(s_chunks.len(), 1, "Surreal holds the chunk: {s_chunks:?}");
    assert!(
        pg_claims.is_empty(),
        "pg must not receive the claim: {pg_claims:?}"
    );
    assert_eq!(s_claims.len(), 1, "Surreal holds the claim: {s_claims:?}");
    let pg_docs: i64 =
        f.db.query_one("SELECT count(*) FROM document", &[])
            .await
            .unwrap()
            .get(0);
    assert_eq!(pg_docs, 0, "pg must not receive the document row");
    let (_, surreal_docs) = f.document_rows().await;
    assert_eq!(surreal_docs.len(), 1, "Surreal holds the document row");
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

/// C-SURALL2 A2, C0b1 bundle 2: old contract "node and edge follow every pg write by copy" ->
/// new contract "the engine writes Surreal's node and edge directly, and the rows are the ones the
/// pg path writes". Every step is done once on the pg-route store (`pg_store`, pg SQL) and once
/// on the Surreal-route store (`store`, Surreal only); the two graphs must be equal by value.
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
    f.doc_fm(&fm, "sha").await;
    f.assert_graph_same("after a tagged document", 3, 3).await;

    // a re-written document with one tag fewer: the dropped tagged edge leaves Surreal too
    fm.tags = vec!["t1".to_string()];
    f.doc_fm(&fm, "sha2").await;
    f.assert_graph_same("after a tag was dropped", 3, 2).await;

    // semantic entities
    for s in [&f.pg_store, &f.store] {
        s.upsert_tool("tl", "Tool").await.unwrap();
        s.upsert_concept("cn", "Concept").await.unwrap();
        s.relate_doc_tool(&a, "tl").await.unwrap();
        s.relate_doc_concept(&a, "cn").await.unwrap();
    }
    f.assert_graph_same("after uses/about", 5, 4).await;
    for s in [&f.pg_store, &f.store] {
        s.clear_semantic_edges(&a).await.unwrap();
    }
    f.assert_graph_same("after clear_semantic_edges", 5, 2)
        .await;

    // a non-fact claim: +3 nodes (claim, typed, owner speaker) and +5 edges (is_a, claims,
    // claim_of_project, said speaker→doc, said speaker→claim) on top of 5 nodes / 2 edges
    f.claim(&a, &format!("g-{}", f.nanos), "decision", 1).await;
    for s in [&f.pg_store, &f.store] {
        s.upsert_claim_node(
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
    }
    f.assert_graph_same("after a decision claim", 8, 7).await;

    // consumption doors: session, gap, supersedes edge with a judge
    f.doc(&b).await;
    for s in [&f.pg_store, &f.store] {
        s.handover_in_tx(
            &format!("s-{}", f.nanos),
            "2026-10-30",
            std::slice::from_ref(&a),
        )
        .await
        .expect("handover");
        s.record_gap_in_tx(
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
        s.supersedes_in_tx(&[[b.clone(), a.clone()]], Some("owner"))
            .await
            .expect("supersedes");
    }
    let [(_, s_nodes), (_, s_edges)] = f.graph_rows().await;
    assert!(
        s_edges.iter().any(|e| e.ends_with("|supersedes|owner")),
        "judge must reach Surreal: {s_edges:?}"
    );
    assert!(s_nodes.iter().any(|n| n.starts_with("gap:")), "{s_nodes:?}");

    // delete + GC: the note's edges leave, then the orphans nobody points at leave
    f.delete(&a).await;
    f.delete(&b).await;
    f.pg_store.gc_orphans().await.expect("pg gc");
    f.store.gc_orphans().await.expect("surreal gc");
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

    // The old stored sha: the whole file's hash, put into the Surreal document (pg has no row).
    let legacy = hex::encode(sha2::Sha256::digest(text.as_bytes()));
    f.set_legacy_sha(&path_s, &legacy).await;
    assert_eq!(f.surreal_sha(&path_s).await, legacy);

    let (second, _) = ingest().await.expect("second ingest");
    assert_eq!(second, FileOutcome::ShaMoved);
    let moved = f.surreal_sha(&path_s).await;
    assert_ne!(
        moved, legacy,
        "the Surreal sha must have moved off the whole-file hash"
    );
}

/// Graph rows written straight to pg are not copied by the sync's catch-up, and a row only Surreal
/// holds is not deleted by it (the door refuses to start with a pg-writing graph path on surreal).
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn node_and_edge_rows_written_straight_to_pg_are_not_copied_by_catch_up() {
    let f = fixture("w_catchup").await;
    f.db.batch_execute("DELETE FROM edge; DELETE FROM node;")
        .await
        .unwrap();
    f.client
        .fill_from(&f.store)
        .await
        .expect("fill of an empty graph");
    let path = format!("/vault/wiki/py-{}.md", f.nanos);
    // C0b1 bundle 2: old contract "node/edge written straight to pg reach Surreal on catch_up" ->
    // new contract "catch_up copies no node/edge (the engine writes them into Surreal)".
    f.db.batch_execute(&format!(
        "INSERT INTO node (id, kind, label, outcome) VALUES ('gap:py-{n}', 'gap', 'q', 'missing'),
                                                            ('session:py-{n}', 'session', 't', NULL);
         INSERT INTO edge (src, dst, kind, judge) VALUES ('session:py-{n}', 'gap:py-{n}', 'gap', NULL),
                                                         ('gap:py-{n}', 'doc:{path}', 'gap_handed', 'owner');",
        n = f.nanos
    ))
    .await
    .unwrap();
    let (written, deleted) = f.store.catch_up_surreal().await.expect("catch up");
    assert_eq!((written, deleted), (0, 0));
    let [(_, s_nodes), (_, s_edges)] = f.graph_rows().await;
    assert!(
        s_nodes.is_empty() && s_edges.is_empty(),
        "pg-only graph rows must not be copied: {s_nodes:?} {s_edges:?}"
    );

    // a row only Surreal holds stays on the next catch-up
    f.client
        .run(
            "UPSERT type::record('node', 'tool:only-s') CONTENT {cid: 'tool:only-s', kind: 'tool', label: 'S'};",
        )
        .await
        .unwrap();
    f.db.batch_execute("DELETE FROM edge; DELETE FROM node;")
        .await
        .unwrap();
    let (written, deleted) = f.store.catch_up_surreal().await.expect("catch up again");
    assert_eq!((written, deleted), (0, 0));
    let [(_, s_nodes), _] = f.graph_rows().await;
    assert_eq!(s_nodes, vec!["tool:only-s|tool|S|-".to_owned()]);
}

#[tokio::test]
#[ignore = "needs throwaway pg: see the run command in this file's header"]
async fn a_dead_surreal_is_an_error_that_does_not_say_pg_has_it() {
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
    // Graph rows exist only in Surreal (bundle 2), so the document's failure is the graph one:
    // it must not claim "pg has it, only Surreal is missing" (a fill could not restore the graph).
    assert!(
        err.to_string().contains(GRAPH_FAILED),
        "document error must carry the graph-write sentence, got: {err:#}"
    );
    assert!(
        !err.to_string().contains(BEHIND),
        "graph rows are not in pg, so the fill-again sentence is false: {err:#}"
    );
    let doc_in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM document WHERE source_path = $1",
            &[&path],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(
        doc_in_pg, 1,
        "the pg document row was committed before the graph write"
    );

    let session = format!("s-dead-{nanos}");
    let err = store
        .handover_in_tx(&session, "2026-10-30", &[])
        .await
        .expect_err("a graph-only write must not swallow a dead Surreal either");
    assert!(
        err.to_string().contains(GRAPH_FAILED) && !err.to_string().contains(BEHIND),
        "graph error must carry the graph-write sentence only, got: {err:#}"
    );
    let node_in_pg: i64 = db
        .query_one(
            "SELECT count(*) FROM node WHERE id = $1",
            &[&format!("session:{session}")],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(
        node_in_pg, 0,
        "node rows are Surreal-only, pg must hold none"
    );

    // A claim goes to Surreal only (bundle 3), so a dead Surreal fails it and pg holds no row.
    let slot = format!("dead-{nanos}");
    let err = store
        .upsert_claim(
            &slot,
            "observed",
            "a long enough value",
            &path,
            SystemTime::now(),
            &vec_at(7),
            "fact",
            "certain",
        )
        .await
        .expect_err("a dead Surreal must be an error on the claim write");
    assert!(
        !err.to_string().contains(BEHIND),
        "pg holds no claim, so the fill-again sentence is false: {err:#}"
    );
    let claim_in_pg: i64 = db
        .query_one("SELECT count(*) FROM claim WHERE subject = $1", &[&slot])
        .await
        .unwrap()
        .get(0);
    assert_eq!(
        claim_in_pg, 0,
        "claim rows are Surreal-only, pg must hold none"
    );

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
    // New contract (C0b1): the document and chunks are written to Surreal before the pg
    // transaction commits, so a dead Surreal fails the note with nothing committed in pg.
    assert!(
        !err.to_string().contains(BEHIND),
        "pg holds nothing, so the sentence must not say it does: {err:#}"
    );
    let note_rows: i64 = db
        .query_one(
            "SELECT count(*) FROM document WHERE source_path = $1",
            &[&note_path],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(note_rows, 0, "pg must hold no document row of the note");
}
