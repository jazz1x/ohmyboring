//! Graph — project/topic edge + 벡터히트→그래프 1-hop 확장회수.
//! "벡터로 찾고 → 그래프로 한 홉": pgvector 진입 + node/edge 재귀 CTE 확장.
//!
//! SRP: `query()` 는 순수 로직(데이터 반환), `run()` 은 CLI I/O 껍질.
use std::collections::HashSet;

use anyhow::Result;

use crate::llm::Llm;
use crate::store::Store;

/// `query()` 반환값 — HTTP 핸들러와 CLI 모두 사용.
pub struct GraphOut {
    pub hit: String,
    pub graph_neighbors: Vec<String>,
    pub semantic_neighbors: Vec<String>,
}

/// 순수 로직: 그래프 회수 → `GraphOut` 반환. I/O 없음.
/// 벡터 히트가 없으면 `hit` 이 빈 문자열인 `GraphOut` 반환.
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

/// CLI 껍질: `query()` 호출 후 stdout 출력.
pub async fn run(store: &Store, llm: &Llm, q: &str) -> Result<()> {
    let gs = store.graph_stats().await?;
    println!(
        "그래프: 문서 {} · 청크 {} · project {} · topic {} · 엣지 {}\n",
        gs.documents, gs.chunks, gs.projects, gs.topics, gs.edges
    );

    let out = query(store, llm, q).await?;
    if out.hit.is_empty() {
        println!("벡터 히트 없음");
        return Ok(());
    }
    println!("벡터 top-1: {}", out.hit);

    println!("\n그래프 1-hop 이웃 (같은 project, 한 쿼리 traversal):");
    if out.graph_neighbors.is_empty() {
        println!("  (이웃 없음)");
    } else {
        for n in &out.graph_neighbors {
            println!("  → {n}");
        }
    }

    println!("\n시맨틱 이웃 (공유 tool/concept, edge-table traversal):");
    if out.semantic_neighbors.is_empty() {
        println!("  (시맨틱 이웃 없음 — extract 실행 전이거나 공유 tool/concept 없음)");
    } else {
        for n in &out.semantic_neighbors {
            println!("  ~ {n}");
        }
    }
    Ok(())
}
