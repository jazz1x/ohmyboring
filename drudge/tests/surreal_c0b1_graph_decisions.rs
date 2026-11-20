//! C-SURALL2 C0b1 bundle 2 (node + edge). On `backend=surreal` the engine writes `node` and `edge`
//! straight into Surreal and reads them back from there; pg holds none.
//!
//! (b) each moved read has one fixture that exists in Surreal only, so pg-reading code and
//!     Surreal-reading code answer differently: `handed_paths`, the `supersedes` edge read that
//!     `compact` seals under, and the node / edge side of `gc_orphans`.
//! (m) the same graph in pg and in Surreal gives the same answer on the pg path and the Surreal
//!     path, both sides of each branch, printed as `C0b1-b2 (m) <read>: same n/N`.
//! (h) every engine graph write on a Surreal-routed store leaves pg's `node` / `edge` untouched
//!     and changes Surreal's: `C0b1-b2 (h) writes only to Surreal: n/N`.
//!
//! `handed_paths` takes the `DocLookup` on head. On trunk df2eba4 the same test reads
//! `Store::handed_paths(&tx, session)`; replace that one call to see it red there. The `compact`
//! and `gc_orphans` tests compile on both.
//!
//! Needs a throwaway pgvector and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test surreal_c0b1_graph_decisions -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::config::SurrealConfig;
use drudge::frontmatter::{Author, Claim, FrontMatter, SaidBy};
use drudge::store::{DocLookup, Store, SurrealClient, VectorRoute};
use serde_json::{Value, json};
use tokio_postgres::NoTls;

const DIM: usize = 1024;
const SURREAL_ONLY_US: i64 = 1_700_000_000_000_000;

fn env(name: &str) -> String {
    std::env::var(name).expect(name)
}

fn nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

fn client_at(namespace: &str) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": env("BORING_TEST_SURREAL_URL"), "namespace": namespace, "database": namespace}),
    )
    .unwrap();
    SurrealClient::new(config.settings(&|n| std::env::var(n).ok()).unwrap()).unwrap()
}

fn front(path: &str, tags: &[&str]) -> FrontMatter {
    FrontMatter {
        origin: "personal".to_owned(),
        project: "c0b1-graph".to_owned(),
        kind: "note".to_owned(),
        source_path: path.to_owned(),
        title: Some("c0b1".to_owned()),
        author: Author::Unknown,
        tags: tags.iter().map(|t| (*t).to_owned()).collect(),
        ..Default::default()
    }
}

fn embedding() -> Vec<f32> {
    let mut v = vec![0.0_f32; DIM];
    v[0] = 0.9;
    v
}

/// An empty pg, Surreal tables laid down by a fill of it, and a store routed to that Surreal.
async fn setup(namespace: &str) -> (Store, Store, SurrealClient, tokio_postgres::Client) {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    db.batch_execute(
        "DELETE FROM claim; DELETE FROM edge; DELETE FROM node; DELETE FROM document;",
    )
    .await
    .unwrap();
    let client = client_at(&format!("{namespace}_{}", nanos()));
    client.fill_from(&pg).await.expect("fill of an empty pg");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client.clone()));
    (pg, routed, client, db)
}

/// A document row written into Surreal only, the way the engine's write puts it.
async fn surreal_only_note(client: &SurrealClient, path: &str) {
    let p = serde_json::to_string(path).unwrap();
    client
        .run(&format!(
            "UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, \
             origin: 'personal', project: 'c0b1-graph', kind: 'note', tags: [], sha: 'sha-s', \
             extracted_sha: '', updated_us: {SURREAL_ONLY_US}, author: 'unknown', via: 'surreal'}};"
        ))
        .await
        .expect("surreal-only document");
}

async fn run_ok(client: &SurrealClient, sql: &str) {
    client.run(sql).await.expect("surreal statement");
}

async fn surreal_count(client: &SurrealClient, table: &str) -> usize {
    let reply = client
        .run(&format!("SELECT count() AS n FROM {table} GROUP ALL;"))
        .await
        .unwrap();
    usize::try_from(reply[0][0]["n"].as_u64().unwrap_or(0)).unwrap()
}

async fn surreal_values(client: &SurrealClient, sql: &str) -> Vec<String> {
    let reply = client.run(sql).await.unwrap();
    let rows: Vec<Vec<Value>> = serde_json::from_value(Value::Array(reply)).unwrap();
    let mut out: Vec<String> = rows[0]
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    out.sort();
    out
}

async fn pg_count(db: &tokio_postgres::Client, table: &str) -> i64 {
    db.query_one(&format!("SELECT count(*) FROM {table}"), &[])
        .await
        .unwrap()
        .get(0)
}

/// Prints `C0b1-b2 (m) <read>: same n/N` and fails on any difference.
fn same<T: std::fmt::Debug>(read: &str, pg: &[T], surreal: &[T]) {
    let (p, s): (Vec<String>, Vec<String>) = (
        pg.iter().map(|x| format!("{x:?}")).collect(),
        surreal.iter().map(|x| format!("{x:?}")).collect(),
    );
    let equal = p.iter().zip(&s).filter(|(a, b)| a == b).count();
    println!("C0b1-b2 (m) {read}: same {equal}/{}", p.len());
    assert_eq!(p, s, "{read}: pg answer vs Surreal answer");
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn handed_paths_reads_the_surreal_edges() {
    let (pg, routed, client, db) = setup("c0b1_b2_handed").await;
    let n = nanos();
    let (p1, p2, p3) = (
        format!("/vault/wiki/h-{n}-1.md"),
        format!("/vault/wiki/h-{n}-2.md"),
        format!("/vault/wiki/h-{n}-3.md"),
    );
    for p in [&p1, &p2] {
        pg.upsert_document(&front(p, &[]), "sha", SystemTime::now())
            .await
            .unwrap();
    }
    let both = format!("h-both-{n}");
    pg.handover_in_tx(&both, "2026-11-02", &[p2.clone(), p1.clone()])
        .await
        .unwrap();
    // A `used` edge of the same session must not count as handed.
    db.execute(
        "INSERT INTO edge (src, dst, kind) VALUES ($1, $2, 'used')",
        &[&format!("session:{both}"), &format!("doc:{p1}")],
    )
    .await
    .unwrap();
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");

    let mut conn = routed.conn().await.unwrap();
    let tx = conn.transaction().await.unwrap();
    // (m) both sides of the branch: a session with handed edges, and a session nobody knows.
    let (mut a, mut b) = (Vec::new(), Vec::new());
    for session in [both.as_str(), "h-unknown-session"] {
        a.push(
            Store::handed_paths(DocLookup::Pg, &tx, session)
                .await
                .unwrap(),
        );
        b.push(
            Store::handed_paths(routed.doc_lookup(), &tx, session)
                .await
                .unwrap(),
        );
    }
    same("handed_paths", &a, &b);

    // (b) a handed edge only Surreal holds: the pg path cannot see it, the Surreal path does.
    surreal_only_note(&client, &p3).await;
    let only = format!("h-only-{n}");
    routed
        .handover_in_tx(&only, "2026-11-02", std::slice::from_ref(&p3))
        .await
        .unwrap();
    let on_pg = Store::handed_paths(DocLookup::Pg, &tx, &only)
        .await
        .unwrap();
    let on_surreal = Store::handed_paths(routed.doc_lookup(), &tx, &only)
        .await
        .unwrap();
    println!("C0b1-b2 (b) handed_paths: pg path {on_pg:?}, surreal path {on_surreal:?}");
    assert_eq!(on_pg.len(), 0);
    assert_eq!(on_surreal, vec![p3]);
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn compact_seals_under_a_supersedes_edge_only_surreal_holds() {
    let (_pg, routed, client, _db) = setup("c0b1_b2_seal").await;
    let n = nanos();
    let (newer, older) = (
        format!("/vault/wiki/s-{n}-new.md"),
        format!("/vault/wiki/s-{n}-old.md"),
    );
    surreal_only_note(&client, &newer).await;
    surreal_only_note(&client, &older).await;
    routed
        .upsert_claim_with_anchor(
            &format!("c0b1-seal-{n}"),
            "goal",
            "a long enough value to count as a statement",
            &older,
            SystemTime::now() - Duration::from_hours(2),
            &embedding(),
            "fact",
            "certain",
            None,
            None,
            None,
        )
        .await
        .expect("claim on surreal");
    // The edge exists in Surreal and nowhere else.
    run_ok(
        &client,
        &format!(
            "UPSERT type::record('edge', 'k-{n}') CONTENT {{cid: 'k-{n}', src: 'doc:{newer}', \
             dst: 'doc:{older}', kind: 'supersedes'}};"
        ),
    )
    .await;

    let summary = routed.compact().await.expect("compact");
    let sealed = surreal_values(
        &client,
        &format!(
            "SELECT VALUE cid FROM claim_row WHERE source_path = '{older}' AND superseded_us != NONE;"
        ),
    )
    .await;
    println!(
        "C0b1-b2 (b) seal edge read: report {} sealed, claim_row sealed {}",
        summary.report.sealed_superseded_claims,
        sealed.len()
    );
    assert_eq!(summary.report.sealed_superseded_claims, 1);
    assert_eq!(sealed.len(), 1);
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn the_supersedes_edge_list_is_the_same_on_the_pg_path_and_the_surreal_path() {
    let (pg, _routed, client, db) = setup("c0b1_b2_seal_m").await;
    let n = nanos();
    let paths: Vec<String> = (0..3)
        .map(|i| format!("/vault/wiki/sm-{n}-{i}.md"))
        .collect();
    for p in &paths {
        pg.upsert_document(&front(p, &[]), "sha", SystemTime::now())
            .await
            .unwrap();
    }
    pg.supersedes_in_tx(
        &[
            [paths[1].clone(), paths[0].clone()],
            [paths[2].clone(), paths[1].clone()],
        ],
        None,
    )
    .await
    .unwrap();
    // Edges of other kinds must stay out of the list on both paths.
    pg.handover_in_tx(&format!("sm-{n}"), "2026-11-02", &paths)
        .await
        .unwrap();
    client.fill_from(&pg).await.expect("fill");

    let mut on_pg: Vec<(String, String)> = db
        .query("SELECT src, dst FROM edge WHERE kind = 'supersedes'", &[])
        .await
        .unwrap()
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    on_pg.sort();
    let mut on_surreal = client.supersedes_edges().await.unwrap();
    on_surreal.sort();
    same(
        "supersedes edges (2 edges, 3 other)",
        &[on_pg],
        &[on_surreal],
    );

    // The empty side of the branch.
    db.batch_execute("DELETE FROM edge WHERE kind = 'supersedes'")
        .await
        .unwrap();
    client.fill_from(&pg).await.expect("fill again");
    let on_pg: Vec<(String, String)> = db
        .query("SELECT src, dst FROM edge WHERE kind = 'supersedes'", &[])
        .await
        .unwrap()
        .iter()
        .map(|r| (r.get(0), r.get(1)))
        .collect();
    let on_surreal = client.supersedes_edges().await.unwrap();
    same("supersedes edges (none)", &[on_pg], &[on_surreal]);
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn gc_orphans_deletes_the_node_and_edge_rows_only_surreal_holds() {
    let (_pg, routed, client, db) = setup("c0b1_b2_gc").await;
    let n = nanos();
    run_ok(
        &client,
        &format!(
            "UPSERT type::record('node', 'tool:b-orphan-{n}') CONTENT {{cid: 'tool:b-orphan-{n}', kind: 'tool', label: 'T'}};
             UPSERT type::record('node', 'concept:b-orphan-{n}') CONTENT {{cid: 'concept:b-orphan-{n}', kind: 'concept', label: 'C'}};
             UPSERT type::record('node', 'tool:b-kept-{n}') CONTENT {{cid: 'tool:b-kept-{n}', kind: 'tool', label: 'K'}};
             UPSERT type::record('node', 'claim:b-stale-{n}:p') CONTENT {{cid: 'claim:b-stale-{n}:p', kind: 'claim', label: 'x', outcome: 'fact'}};
             UPSERT type::record('edge', 'b1-{n}') CONTENT {{cid: 'b1-{n}', src: 'doc:/b/a', dst: 'tool:b-kept-{n}', kind: 'uses'}};
             UPSERT type::record('edge', 'b2-{n}') CONTENT {{cid: 'b2-{n}', src: 'doc:/b/a', dst: 'claim:b-stale-{n}:p', kind: 'claims'}};
             UPSERT type::record('edge', 'b3-{n}') CONTENT {{cid: 'b3-{n}', src: 'claim:b-stale-{n}:p', dst: 'project:b', kind: 'claim_of_project'}};"
        ),
    )
    .await;
    assert_eq!(
        (pg_count(&db, "node").await, pg_count(&db, "edge").await),
        (0, 0)
    );

    let gc = routed.gc_orphans().await.expect("gc");
    let nodes = surreal_values(&client, "SELECT VALUE cid FROM node;").await;
    println!(
        "C0b1-b2 (b) gc_orphans: tool {} concept {} claim_nodes {} claim_edges {}; left {nodes:?}",
        gc.tool, gc.concept, gc.claim_nodes, gc.claim_edges
    );
    assert_eq!(
        (gc.tool, gc.concept, gc.claim_nodes, gc.claim_edges),
        (1, 1, 1, 2)
    );
    assert_eq!(nodes, vec![format!("tool:b-kept-{n}")]);
    assert_eq!(surreal_count(&client, "edge").await, 1);
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn gc_orphans_counts_the_same_on_the_pg_path_and_the_surreal_path() {
    let (pg, routed, client, db) = setup("c0b1_b2_gc_m").await;
    let n = nanos();
    let path = format!("/vault/wiki/gm-{n}.md");
    pg.upsert_claim(
        &format!("m-live-{n}"),
        "p",
        "a long enough value to count as a statement",
        &path,
        SystemTime::now() - Duration::from_hours(1),
        &embedding(),
        "decision",
        "certain",
    )
    .await
    .unwrap();
    db.batch_execute(&format!(
        "INSERT INTO node (id, kind, label, outcome) VALUES
           ('tool:m-orphan-{n}', 'tool', 'T', NULL), ('tool:m-kept-{n}', 'tool', 'K', NULL),
           ('concept:m-orphan-{n}', 'concept', 'C', NULL), ('concept:m-kept-{n}', 'concept', 'K', NULL),
           ('claim:m-live-{n}:p', 'claim', 'x', 'decision'), ('decision:m-live-{n}:p', 'decision', 'x', 'certain'),
           ('claim:m-stale-{n}:p', 'claim', 'x', 'decision'), ('decision:m-stale-{n}:p', 'decision', 'x', 'certain');
         INSERT INTO edge (src, dst, kind) VALUES
           ('doc:{path}', 'tool:m-kept-{n}', 'uses'), ('doc:{path}', 'concept:m-kept-{n}', 'about'),
           ('doc:{path}', 'claim:m-live-{n}:p', 'claims'), ('claim:m-live-{n}:p', 'decision:m-live-{n}:p', 'is_a'),
           ('doc:{path}', 'claim:m-stale-{n}:p', 'claims'), ('claim:m-stale-{n}:p', 'decision:m-stale-{n}:p', 'is_a');"
    ))
    .await
    .unwrap();
    client.fill_from(&pg).await.expect("fill");

    let on_pg = pg.gc_orphans().await.expect("pg gc");
    let on_surreal = routed.gc_orphans().await.expect("surreal gc");
    let fields = |g: &drudge::store::GcStats| [g.tool, g.concept, g.claim_nodes, g.claim_edges];
    println!("C0b1-b2 (m) gc_orphans fixture: pg {on_pg:?} surreal {on_surreal:?}");
    same("gc_orphans", &fields(&on_pg), &fields(&on_surreal));
    assert_eq!(
        fields(&on_pg),
        [1, 1, 2, 2],
        "the fixture covers each branch"
    );

    let left_pg: Vec<String> = db
        .query("SELECT id FROM node ORDER BY id", &[])
        .await
        .unwrap()
        .iter()
        .map(|r| r.get(0))
        .collect();
    let left_surreal = surreal_values(&client, "SELECT VALUE cid FROM node;").await;
    same("gc_orphans (nodes left)", &left_pg, &left_surreal);
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
#[allow(unused_assignments)] // `step!` re-baselines `last`; the final call's write is never read
async fn every_graph_write_goes_only_to_surreal() {
    let (_pg, routed, client, db) = setup("c0b1_b2_h").await;
    let n = nanos();
    let (a, b) = (
        format!("/vault/wiki/w-{n}-a.md"),
        format!("/vault/wiki/w-{n}-b.md"),
    );
    let mut ok = 0_usize;
    let mut steps = 0_usize;
    let mut last = (
        surreal_count(&client, "node").await,
        surreal_count(&client, "edge").await,
    );
    macro_rules! step {
        ($name:expr) => {{
            let now = (
                surreal_count(&client, "node").await,
                surreal_count(&client, "edge").await,
            );
            let pg_empty = pg_count(&db, "node").await == 0 && pg_count(&db, "edge").await == 0;
            let moved = now != last;
            println!(
                "C0b1-b2 (h) {}: pg empty {pg_empty}, surreal {last:?} -> {now:?}",
                $name
            );
            steps += 1;
            ok += usize::from(pg_empty && moved);
            last = now;
        }};
    }

    {
        let mut conn = routed.conn().await.unwrap();
        let tx = conn.transaction().await.unwrap();
        routed
            .write_note_on(
                &tx,
                &front(&a, &["t1", "t2"]),
                "sha",
                SystemTime::now(),
                vec![("chunk text".to_owned(), embedding())],
            )
            .await
            .unwrap();
        tx.commit().await.unwrap();
    }
    step!("write_note_on (project, topics, in_project, tagged)");
    surreal_only_note(&client, &b).await;
    last = (
        surreal_count(&client, "node").await,
        surreal_count(&client, "edge").await,
    );

    routed.upsert_tool("tl", "Tool").await.unwrap();
    step!("upsert_tool");
    routed.upsert_concept("cn", "Concept").await.unwrap();
    step!("upsert_concept");
    routed.relate_doc_tool(&a, "tl").await.unwrap();
    step!("relate_doc_tool");
    routed.relate_doc_concept(&a, "cn").await.unwrap();
    step!("relate_doc_concept");
    routed
        .upsert_claim_node(
            &a,
            "c0b1-graph",
            &format!("w-{n}"),
            "goal",
            &Claim {
                subject: format!("w-{n}"),
                predicate: "goal".to_owned(),
                value: "a long enough value".to_owned(),
                kind: "decision".to_owned(),
                confidence: "high".to_owned(),
                said_by: Some(SaidBy::Owner),
            },
        )
        .await
        .unwrap();
    step!("upsert_claim_node (claim, typed, is_a, claims, claim_of_project, said)");
    routed
        .handover_in_tx(&format!("w-{n}"), "2026-11-02", std::slice::from_ref(&a))
        .await
        .unwrap();
    step!("handover_in_tx (session, handed)");
    routed
        .record_gap_in_tx(
            &drudge::gap::GapArgs {
                session_id: format!("w-{n}"),
                query: "what".to_owned(),
                kind: drudge::gap::Kind::Missing,
                handed: vec![a.clone()],
            },
            "2026-11-02",
        )
        .await
        .unwrap();
    step!("record_gap_in_tx (gap, session, gap, gap_handed)");
    {
        let mut conn = routed.conn().await.unwrap();
        let tx = conn.transaction().await.unwrap();
        Store::record_consumption_with(
            routed.doc_lookup(),
            &tx,
            &format!("w-used-{n}"),
            "2026-11-02",
            std::slice::from_ref(&a),
            std::slice::from_ref(&b),
            &[],
            Some("owner"),
        )
        .await
        .unwrap();
        tx.commit().await.unwrap();
    }
    step!("record_consumption_with (session, used, contested)");
    routed
        .supersedes_in_tx(&[[b.clone(), a.clone()]], Some("owner"))
        .await
        .unwrap();
    step!("supersedes_in_tx (supersedes)");
    routed.clear_semantic_edges(&a).await.unwrap();
    step!("clear_semantic_edges (delete uses, about)");
    routed.gc_orphans().await.unwrap();
    step!("gc_orphans (orphan tool, concept)");
    routed.delete_document(&a).await.unwrap();
    step!("delete_document (delete edges at the note)");

    println!("C0b1-b2 (h) writes only to Surreal: {ok}/{steps}");
    assert_eq!(ok, steps);
}
