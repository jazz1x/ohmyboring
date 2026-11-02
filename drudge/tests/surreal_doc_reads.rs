//! C-SURALL2 C0a: on `backend=surreal` the document / graph / consumption reads come out of
//! Surreal. Each test fills a throwaway pg, copies it into a throwaway Surreal, asks the pg-backed
//! store and the Surreal-backed store the same questions and prints `C0a <read>: same n/N`. The
//! second test then removes every row from pg: a read that still goes to pg answers nothing there.
//!
//! Needs a throwaway pgvector and a throwaway SurrealDB. Run:
//!   BORING_TEST_DATABASE_URL=postgresql://boring:boring@127.0.0.1:<pg-port>/boring \
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<surreal-port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test surreal_doc_reads -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::too_many_lines)]

use std::collections::BTreeMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::{SecondsFormat, Utc};
use drudge::config::SurrealConfig;
use drudge::frontmatter::{Author, FrontMatter};
use drudge::store::{Doc, Store, SurrealClient, VectorRoute};
use serde_json::json;
use tokio_postgres::NoTls;

const DIM: usize = 1024;

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

async fn pg_client() -> tokio_postgres::Client {
    let (db, conn) = tokio_postgres::connect(&env("BORING_TEST_DATABASE_URL"), NoTls)
        .await
        .expect("connect");
    tokio::spawn(conn);
    db
}

/// Prints `C0a <read>: same n/N` and fails on any difference.
fn same<T: std::fmt::Debug>(read: &str, pg: &[T], surreal: &[T]) {
    let (p, s): (Vec<String>, Vec<String>) = (
        pg.iter().map(|x| format!("{x:?}")).collect(),
        surreal.iter().map(|x| format!("{x:?}")).collect(),
    );
    let equal = p.iter().zip(&s).filter(|(a, b)| a == b).count();
    println!("C0a {read}: same {equal}/{}", p.len());
    assert_eq!(p, s, "{read}: pg answer vs Surreal answer");
}

/// One value (or a map flattened to sorted pairs) as a one-element list for `same`.
fn one<T: std::fmt::Debug>(v: &T) -> Vec<String> {
    vec![format!("{v:?}")]
}

fn sorted_map<V: std::fmt::Debug>(m: impl IntoIterator<Item = (String, V)>) -> Vec<String> {
    let sorted: BTreeMap<String, String> =
        m.into_iter().map(|(k, v)| (k, format!("{v:?}"))).collect();
    sorted
        .into_iter()
        .map(|(k, v)| format!("{k} => {v}"))
        .collect()
}

fn sorted_vals(m: impl IntoIterator<Item = (String, Vec<String>)>) -> Vec<String> {
    sorted_map(m.into_iter().map(|(k, mut v)| {
        v.sort();
        (k, v)
    }))
}

fn emb(i: usize) -> Vec<f32> {
    let mut v = vec![0.0_f32; DIM];
    v[0] = 0.9;
    v[1 + i % 3] = 0.43;
    v
}

fn path(i: usize) -> String {
    if i == 6 {
        "/vault/wiki/eval-c0a-6.md".to_owned()
    } else {
        format!("/vault/wiki/c0a-{i}.md")
    }
}

fn doc_id(i: usize) -> String {
    format!("doc:{}", path(i))
}

fn rfc3339_ago(hours: i64) -> String {
    (Utc::now() - chrono::Duration::hours(hours)).to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn naive_ago(hours: i64) -> String {
    (Utc::now() - chrono::Duration::hours(hours))
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string()
}

/// Seven notes (one without chunks, one an `eval-` note, one in another project), three chunks each
/// (keys `#0`, `#2`, `#10`: key order is not text order), the concept / tool / claim / project /
/// topic nodes and edges between them, `supersedes` between two notes, and sessions with `used` /
/// `contested` / `handed` edges whose labels are RFC 3339, naive, old and not a time at all.
async fn seed(pg: &Store, db: &tokio_postgres::Client) {
    db.batch_execute(
        "DELETE FROM claim; DELETE FROM edge; DELETE FROM node; DELETE FROM document;",
    )
    .await
    .unwrap();
    let now = SystemTime::now();
    // (project, origin, author, age in hours)
    let notes: [(&str, &str, Author, u64); 7] = [
        ("c0a", "personal", Author::Owner, 500),
        ("c0a", "personal", Author::Owner, 100),
        ("c0a", "personal", Author::Unknown, 50),
        ("c0a", "company", Author::Inferred, 10),
        ("c0a-b", "personal", Author::Owner, 5),
        ("c0a", "personal", Author::Unknown, 1),
        ("c0a", "personal", Author::Unknown, 2),
    ];
    for (i, (project, origin, author, age)) in notes.into_iter().enumerate() {
        let front = FrontMatter {
            origin: origin.to_owned(),
            project: project.to_owned(),
            kind: if i % 2 == 0 { "note" } else { "decision" }.to_owned(),
            source_path: path(i),
            title: Some(format!("c0a {i}")),
            tags: vec![format!("t{}", i % 2), "common".to_owned()],
            author,
            ..Default::default()
        };
        pg.upsert_document(&front, &format!("sha-{i}"), now - Duration::from_hours(age))
            .await
            .unwrap();
        if i == 5 {
            continue;
        }
        for idx in [10_usize, 0, 2] {
            pg.upsert_chunk(&Doc {
                id: format!("{}#{idx}", path(i)),
                content: format!("doc{i} part{idx} alpha \"quoted\" 한글"),
                embedding: emb(i),
                front: front.clone(),
                chunk_idx: idx,
            })
            .await
            .unwrap();
        }
    }
    let labels = [rfc3339_ago(2), rfc3339_ago(400), naive_ago(3)];
    let graph_nodes = [
        ("concept:alpha", "concept", "Alpha"),
        ("concept:beta", "concept", "beta"),
        ("concept:gamma", "concept", "Gamma"),
        ("tool:rg", "tool", "rg"),
        ("tool:git", "tool", "git"),
        ("project:c0a", "project", "c0a"),
        ("topic:t1", "topic", "t1"),
        ("claim:s1:p1", "claim", "s1 p1"),
        ("claim:s2:p2", "claim", "s2 p2"),
        ("decision:d1", "decision", "d1"),
        ("risk:r1", "risk", "r1"),
        ("session:slack:new", "session", labels[0].as_str()),
        ("session:slack:old", "session", labels[1].as_str()),
        ("session:naive", "session", labels[2].as_str()),
        ("session:notatime", "session", "not-a-time"),
    ];
    for (id, kind, label) in graph_nodes {
        db.execute(
            "INSERT INTO node (id, kind, label) VALUES ($1, $2, $3)",
            &[&id, &kind, &label],
        )
        .await
        .unwrap();
    }
    let about = [
        (0, "alpha"),
        (0, "beta"),
        (1, "alpha"),
        (1, "beta"),
        (1, "gamma"),
        (2, "alpha"),
        (2, "beta"),
        (3, "alpha"),
        (4, "beta"),
        (4, "gamma"),
        (6, "alpha"),
    ];
    let mut edges: Vec<(String, String, &str, Option<&str>)> = Vec::new();
    for (i, c) in about {
        edges.push((doc_id(i), format!("concept:{c}"), "about", None));
    }
    for (i, t) in [(0, "rg"), (0, "git"), (1, "rg"), (1, "git"), (2, "rg")] {
        edges.push((doc_id(i), format!("tool:{t}"), "uses", None));
    }
    // the same node reached by two edge kinds: pg's join counts it twice
    edges.push((doc_id(0), "concept:alpha".to_owned(), "uses", None));
    for (i, c) in [(0, "s1:p1"), (1, "s1:p1"), (2, "s2:p2"), (3, "s1:p1")] {
        edges.push((doc_id(i), format!("claim:{c}"), "claims", None));
    }
    for i in [0, 1, 2, 3, 4] {
        edges.push((doc_id(i), "project:c0a".to_owned(), "in_project", None));
        edges.push((doc_id(i), "topic:t1".to_owned(), "tagged", None));
    }
    edges.push((doc_id(1), doc_id(0), "supersedes", None));
    edges.push((doc_id(3), doc_id(2), "supersedes", Some("inferred")));
    for (s, i, kind, judge) in [
        ("session:slack:new", 0, "used", Some("owner")),
        ("session:slack:new", 1, "used", Some("owner")),
        ("session:slack:new", 2, "contested", Some("inferred")),
        ("session:slack:new", 0, "contested", None),
        ("session:slack:new", 3, "used", Some("agent:x")),
        ("session:slack:new", 4, "contested", Some("inferred")),
        ("session:slack:new", 0, "handed", None),
        ("session:slack:old", 0, "used", Some("owner")),
        ("session:naive", 1, "used", None),
        ("session:naive", 4, "contested", Some("owner")),
        ("session:notatime", 4, "used", None),
    ] {
        edges.push((s.to_owned(), doc_id(i), kind, judge));
    }
    for (src, dst, kind, judge) in edges {
        db.execute(
            "INSERT INTO edge (src, dst, kind, judge) VALUES ($1, $2, $3, $4)",
            &[&src, &dst, &kind, &judge],
        )
        .await
        .unwrap();
    }
}

async fn pair(namespace: &str) -> (Store, Store, tokio_postgres::Client, SurrealClient) {
    let dsn = env("BORING_TEST_DATABASE_URL");
    let pg = Store::open(&dsn, DIM).await.expect("open store");
    let db = pg_client().await;
    seed(&pg, &db).await;
    let client = client_at(&format!("{namespace}_{}", nanos()));
    let tables = client.fill_from(&pg).await.expect("fill");
    assert!(tables.iter().all(drudge::store::FillTable::same), "fill");
    let routed = Store::open(&dsn, DIM)
        .await
        .unwrap()
        .with_vector_route(VectorRoute::Surreal(client.clone()));
    (pg, routed, db, client)
}

#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn document_and_graph_reads_equal_pg_over_the_same_rows() {
    let (pg, sur, _db, _client) = pair("c0a_parity").await;
    let all: Vec<String> = (0..7).map(path).collect();
    let none: Vec<String> = Vec::new();
    let company = vec!["company".to_owned()];

    let mut a = pg.all_doc_paths().await.unwrap();
    let mut b = sur.all_doc_paths().await.unwrap();
    a.sort();
    b.sort();
    same("all_doc_paths", &a, &b);

    let a: Vec<_> = futures_sha(&pg, &all).await;
    let b: Vec<_> = futures_sha(&sur, &all).await;
    same("get_doc_sha", &a, &b);

    for (name, limit, excl, hours, project) in [
        ("recent_docs plain", 10_i64, &none, None, None),
        ("recent_docs exclude+window", 10, &company, Some(48), None),
        ("recent_docs project", 10, &none, None, Some("c0a-b")),
        (
            "recent_docs limit+window+project",
            2,
            &none,
            Some(168),
            Some("c0a"),
        ),
    ] {
        let a = pg.recent_docs(limit, excl, hours, project).await.unwrap();
        let b = sur.recent_docs(limit, excl, hours, project).await.unwrap();
        same(name, &a, &b);
    }

    let mut a = Vec::new();
    let mut b = Vec::new();
    for i in [0, 1, 2, 3, 4, 5] {
        for limit in [1, 5] {
            a.push(pg.related_docs(&path(i), limit).await.unwrap());
            b.push(sur.related_docs(&path(i), limit).await.unwrap());
        }
    }
    same("related_docs", &a, &b);

    let mut a = Vec::new();
    let mut b = Vec::new();
    for i in [0, 1, 2, 4] {
        a.push(pg.related_doc_content(&path(i), 5).await.unwrap());
        b.push(sur.related_doc_content(&path(i), 5).await.unwrap());
    }
    same("related_doc_content", &a, &b);

    let mut a = Vec::new();
    let mut b = Vec::new();
    for i in [1, 2, 3, 4, 5] {
        for (limit, min) in [(5, 1), (5, 2), (1, 1), (5, 3)] {
            a.push(
                pg.related_by_shared_ground(&path(i), limit, min)
                    .await
                    .unwrap(),
            );
            b.push(
                sur.related_by_shared_ground(&path(i), limit, min)
                    .await
                    .unwrap(),
            );
        }
    }
    same("related_by_shared_ground", &a, &b);

    let mut a = Vec::new();
    let mut b = Vec::new();
    for i in [0, 4, 5] {
        for limit in [2, 10] {
            a.push(pg.recent_project_docs(&path(i), limit).await.unwrap());
            b.push(sur.recent_project_docs(&path(i), limit).await.unwrap());
        }
    }
    same("recent_project_docs", &a, &b);

    let mut a: Vec<String> = pg
        .text_search("alpha", 50)
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    let mut b: Vec<String> = sur
        .text_search("alpha", 50)
        .await
        .unwrap()
        .into_iter()
        .map(|h| h.id)
        .collect();
    a.sort();
    b.sort();
    same("text_search (chunk ids)", &a, &b);

    same(
        "count",
        &[pg.count().await.unwrap()],
        &[sur.count().await.unwrap()],
    );

    let mut a: Vec<String> = pg
        .all_meta()
        .await
        .unwrap()
        .iter()
        .map(|m| format!("{m:?}"))
        .collect();
    let mut b: Vec<String> = sur
        .all_meta()
        .await
        .unwrap()
        .iter()
        .map(|m| format!("{m:?}"))
        .collect();
    a.sort();
    b.sort();
    same("all_meta", &a, &b);

    same(
        "project_names",
        &pg.project_names().await.unwrap(),
        &sur.project_names().await.unwrap(),
    );
    for days in [1, 3, 30] {
        let a = pg.active_project_counts(days).await.unwrap();
        let b = sur.active_project_counts(days).await.unwrap();
        same(&format!("active_project_counts {days}d"), &a, &b);
    }

    let mut a: Vec<String> = pg
        .approved_edges()
        .await
        .unwrap()
        .iter()
        .map(|e| format!("{e:?}"))
        .collect();
    let mut b: Vec<String> = sur
        .approved_edges()
        .await
        .unwrap()
        .iter()
        .map(|e| format!("{e:?}"))
        .collect();
    a.sort();
    b.sort();
    same("approved_edges", &a, &b);

    same(
        "consumption_counts",
        &sorted_map(pg.consumption_counts(&all).await.unwrap()),
        &sorted_map(sur.consumption_counts(&all).await.unwrap()),
    );
    same(
        "ranking_feedback_counts",
        &sorted_map(pg.ranking_feedback_counts(&all).await.unwrap()),
        &sorted_map(sur.ranking_feedback_counts(&all).await.unwrap()),
    );
    same(
        "concepts_of",
        &sorted_vals(pg.concepts_of(&all).await.unwrap()),
        &sorted_vals(sur.concepts_of(&all).await.unwrap()),
    );
    same(
        "superseded_by",
        &sorted_vals(pg.superseded_by(&all).await.unwrap()),
        &sorted_vals(sur.superseded_by(&all).await.unwrap()),
    );
    same(
        "rank_facts",
        &sorted_map(pg.rank_facts(&all).await.unwrap()),
        &sorted_map(sur.rank_facts(&all).await.unwrap()),
    );

    for (days, limit) in [(7_i64, 5_i64), (7, 1), (30, 10)] {
        let a = pg.reused_recently(days, limit).await.unwrap();
        let b = sur.reused_recently(days, limit).await.unwrap();
        same(&format!("reused_recently {days}d/{limit}"), &a, &b);
    }

    let mut a = Vec::new();
    let mut b = Vec::new();
    for i in [0, 3, 5] {
        let chunk = format!("{}#0", path(i));
        let (mut g1, mut s1) = (
            pg.graph_neighbors(&chunk).await.unwrap(),
            pg.semantic_neighbors(&chunk).await.unwrap(),
        );
        let (mut g2, mut s2) = (
            sur.graph_neighbors(&chunk).await.unwrap(),
            sur.semantic_neighbors(&chunk).await.unwrap(),
        );
        g1.sort();
        s1.sort();
        g2.sort();
        s2.sort();
        a.push((g1, s1));
        b.push((g2, s2));
    }
    same("graph_neighbors+semantic_neighbors", &a, &b);

    same(
        "graph_stats",
        &one(&pg.graph_stats().await.unwrap()),
        &one(&sur.graph_stats().await.unwrap()),
    );
    same(
        "semantic_stats",
        &one(&pg.semantic_stats().await.unwrap()),
        &one(&sur.semantic_stats().await.unwrap()),
    );
}

async fn futures_sha(store: &Store, paths: &[String]) -> Vec<Option<String>> {
    let mut out = Vec::new();
    for p in paths {
        out.push(store.get_doc_sha(p).await.unwrap());
    }
    out.push(store.get_doc_sha("/vault/wiki/absent.md").await.unwrap());
    out
}

/// After the copy, pg loses every document, chunk, node and edge. The reads must still answer,
/// from Surreal alone.
#[tokio::test]
#[ignore = "needs throwaway pg+surreal: see the run command in this file's header"]
async fn reads_see_documents_and_edges_only_surreal_holds() {
    let (_pg, sur, db, _client) = pair("c0a_only").await;
    db.batch_execute("DELETE FROM edge; DELETE FROM node; DELETE FROM document;")
        .await
        .unwrap();
    let in_pg: i64 = db
        .query_one("SELECT count(*) FROM document", &[])
        .await
        .unwrap()
        .get(0);
    assert_eq!(in_pg, 0, "pg holds no document");

    let sha = sur.get_doc_sha(&path(0)).await.unwrap();
    println!("C0a only-in-Surreal get_doc_sha: {sha:?}");
    assert_eq!(sha.as_deref(), Some("sha-0"));
    assert_eq!(sur.all_doc_paths().await.unwrap().len(), 7);
    let related = sur.related_docs(&path(0), 2).await.unwrap();
    println!("C0a only-in-Surreal related_docs: {related:?}");
    assert_eq!(related, vec![path(1), path(2)]);
    let content = sur.related_doc_content(&path(0), 1).await.unwrap();
    assert_eq!(content[0].source_path, path(1));
    assert_eq!(
        content[0].content,
        "doc1 part0 alpha \"quoted\" 한글\ndoc1 part2 alpha \"quoted\" 한글\ndoc1 part10 alpha \"quoted\" 한글"
    );
    assert_eq!(
        sur.superseded_by(&[path(0)]).await.unwrap().get(&path(0)),
        Some(&vec![path(1)])
    );
    let reused = sur.reused_recently(7, 5).await.unwrap();
    assert!(!reused.is_empty(), "sessions of the window are in Surreal");
    assert_eq!(sur.project_names().await.unwrap(), vec!["c0a", "c0a-b"]);
    assert_eq!(sur.graph_stats().await.unwrap().documents, 7);
}
