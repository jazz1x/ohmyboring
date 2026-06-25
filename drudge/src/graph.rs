//! Graph — project/topic edge + vector-hit → graph 1-hop expansion recall.
//! "find by vector → one hop by graph": pgvector entry + node/edge recursive CTE expansion.
//!
//! Cross-reference: design decision D2 (deterministic graph) · ENFORCEMENT.md §B (one-way flow).
//!
//! SRP: `query()` is pure logic (returns data), `run()` is the CLI I/O shell.
use std::collections::HashSet;

use anyhow::Result;

use crate::llm::Llm;
use crate::store::Store;

/// `query()` return value — used by both the HTTP handler and the CLI.
pub struct GraphOut {
    pub hit: String,
    pub graph_neighbors: Vec<String>,
    pub semantic_neighbors: Vec<String>,
}

/// Pure logic: graph recall → returns `GraphOut`. No I/O.
/// If there is no vector hit, returns a `GraphOut` whose `hit` is an empty string.
pub async fn query(store: &Store, llm: &Llm, q: &str) -> Result<GraphOut> {
    let qe = llm.embed(q).await?;
    let hits = store.vector_search(&qe, 1).await?;
    let Some(top) = hits.into_iter().next() else {
        return Ok(GraphOut {
            hit: String::new(),
            graph_neighbors: vec![],
            semantic_neighbors: vec![],
        });
    };

    let hit = format!("{} ({})", top.source_path, top.project);

    let raw_neighbors = store.graph_neighbors(&top.id).await?;
    let mut seen = HashSet::new();
    let graph_neighbors: Vec<String> = raw_neighbors
        .into_iter()
        .filter(|n| n != &top.source_path && seen.insert(n.clone()))
        .collect();

    let raw_sem = store.semantic_neighbors(&top.id).await?;
    let mut seen2 = HashSet::new();
    let semantic_neighbors: Vec<String> = raw_sem
        .into_iter()
        .filter(|n| seen2.insert(n.clone()))
        .collect();

    Ok(GraphOut {
        hit,
        graph_neighbors,
        semantic_neighbors,
    })
}

/// CLI shell: call `query()` then print to stdout.
pub async fn run(store: &Store, llm: &Llm, q: &str) -> Result<()> {
    let gs = store.graph_stats().await?;
    println!(
        "graph: documents {} · chunks {} · project {} · topic {} · edges {}\n",
        gs.documents, gs.chunks, gs.projects, gs.topics, gs.edges
    );

    let out = query(store, llm, q).await?;
    if out.hit.is_empty() {
        println!("no vector hit");
        return Ok(());
    }
    println!("vector top-1: {}", out.hit);

    println!("\ngraph 1-hop neighbors (same project, single-query traversal):");
    if out.graph_neighbors.is_empty() {
        println!("  (no neighbors)");
    } else {
        for n in &out.graph_neighbors {
            println!("  → {n}");
        }
    }

    println!("\nsemantic neighbors (shared tool/concept, edge-table traversal):");
    if out.semantic_neighbors.is_empty() {
        println!("  (no semantic neighbors — extract not yet run, or no shared tool/concept)");
    } else {
        for n in &out.semantic_neighbors {
            println!("  ~ {n}");
        }
    }
    Ok(())
}
