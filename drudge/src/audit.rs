//! Audit — 적재 상태 점검: origin/kind/project 분포 + 품질 경고.
//! "적재가 엉망인지"를 추측 아니라 DB 실제 집계로 답한다. 집계는 Rust 에서(SQL GROUP 회피).
//!
//! SRP: `stats()` 는 데이터 반환(HTTP·CLI 공용), `run()` 은 CLI stdout 껍질.
use std::collections::{HashMap, HashSet};

use anyhow::Result;
use serde::Serialize;

use crate::store::Store;

fn tally<'a>(it: impl Iterator<Item = &'a str>) -> Vec<(&'a str, usize)> {
    let mut m: HashMap<&str, usize> = HashMap::new();
    for k in it {
        *m.entry(k).or_insert(0) += 1;
    }
    let mut v: Vec<(&str, usize)> = m.into_iter().collect();
    v.sort_unstable_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    v
}

fn print_group(label: &str, rows: &[(&str, usize)]) {
    println!("  [{label}]");
    for (k, c) in rows {
        let key = if k.is_empty() { "(empty)" } else { k };
        println!("    {key:<28} {c}");
    }
}

/// `stats()` 반환값 — HTTP 핸들러와 CLI 모두 사용.
#[derive(Serialize)]
pub struct AuditStats {
    pub total_chunks: usize,
    pub total_files: usize,
    pub by_origin: Vec<(String, usize)>,
    pub by_kind: Vec<(String, usize)>,
    pub by_project: Vec<(String, usize)>,
    pub company_contamination: usize,
    pub missing_origin: usize,
    pub missing_project: usize,
    pub clean: bool,
    pub graph_documents: usize,
    pub graph_chunks: usize,
    pub graph_projects: usize,
    pub graph_topics: usize,
    pub graph_edges: usize,
    pub semantic_problems: usize,
    pub semantic_solutions: usize,
    pub semantic_tools: usize,
    pub semantic_concepts: usize,
    pub semantic_attempts: usize,
    pub semantic_addresses: usize,
    pub semantic_resolved_by: usize,
    pub semantic_uses: usize,
    pub semantic_about: usize,
    pub semantic_tried: usize,
}

/// 순수 로직: DB 집계 → `AuditStats` 반환. I/O 없음.
pub async fn stats(store: &Store) -> Result<AuditStats> {
    let metas = store.all_meta().await?;
    let total_chunks = metas.len();
    let files: HashSet<&str> = metas.iter().map(|m| m.source_path.as_str()).collect();
    let total_files = files.len();

    let by_origin: Vec<(String, usize)> = tally(metas.iter().map(|m| m.origin.as_str()))
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    let by_kind: Vec<(String, usize)> = tally(metas.iter().map(|m| m.kind.as_str()))
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();
    let by_project: Vec<(String, usize)> = tally(metas.iter().map(|m| m.project.as_str()))
        .into_iter()
        .map(|(k, v)| (k.to_owned(), v))
        .collect();

    let company_contamination = metas
        .iter()
        .filter(|m| crate::frontmatter::is_company_path(&m.source_path))
        .count();
    let missing_origin = metas.iter().filter(|m| m.origin.is_empty()).count();
    let missing_project = metas.iter().filter(|m| m.project.is_empty()).count();
    let clean = company_contamination == 0 && missing_origin == 0;

    let gs = store.graph_stats().await?;
    let ss = store.semantic_stats().await?;

    Ok(AuditStats {
        total_chunks,
        total_files,
        by_origin,
        by_kind,
        by_project,
        company_contamination,
        missing_origin,
        missing_project,
        clean,
        graph_documents: gs.documents,
        graph_chunks: gs.chunks,
        graph_projects: gs.projects,
        graph_topics: gs.topics,
        graph_edges: gs.edges,
        semantic_problems: ss.problems,
        semantic_solutions: ss.solutions,
        semantic_tools: ss.tools,
        semantic_concepts: ss.concepts,
        semantic_attempts: ss.attempts,
        semantic_addresses: ss.addresses,
        semantic_resolved_by: ss.resolved_by,
        semantic_uses: ss.uses,
        semantic_about: ss.about,
        semantic_tried: ss.tried,
    })
}

/// CLI 껍질: `stats()` 호출 후 stdout 출력.
pub async fn run(store: &Store) -> Result<()> {
    let metas = store.all_meta().await?;
    let total = metas.len();
    let files: HashSet<&str> = metas.iter().map(|m| m.source_path.as_str()).collect();
    println!("📊 적재 감사 — 청크 {total} · 소스파일 {}\n", files.len());

    print_group("origin", &tally(metas.iter().map(|m| m.origin.as_str())));
    print_group("kind", &tally(metas.iter().map(|m| m.kind.as_str())));
    print_group("project", &tally(metas.iter().map(|m| m.project.as_str())));

    let company = metas
        .iter()
        .filter(|m| crate::frontmatter::is_company_path(&m.source_path))
        .count();
    let no_origin = metas.iter().filter(|m| m.origin.is_empty()).count();
    let no_project = metas.iter().filter(|m| m.project.is_empty()).count();
    println!("\n  ⚠️ 품질 점검");
    println!("    회사 오염       : {company}  (기대 0)");
    println!("    origin 누락     : {no_origin}");
    println!("    project 누락    : {no_project}");
    let clean = company == 0 && no_origin == 0;
    println!(
        "    → {}",
        if clean {
            "✅ 깨끗"
        } else {
            "❌ 점검 필요"
        }
    );

    let gs = store.graph_stats().await?;
    println!(
        "\n  [graph] 문서 {} · 청크 {} · project {} · topic {} · 엣지 {}",
        gs.documents, gs.chunks, gs.projects, gs.topics, gs.edges
    );

    let ss = store.semantic_stats().await?;
    println!(
        "  [semantic] problem {} · solution {} · tool {} · concept {} · attempt {}",
        ss.problems, ss.solutions, ss.tools, ss.concepts, ss.attempts
    );
    println!(
        "  [semantic edges] addresses {} · resolved_by {} · uses {} · about {} · tried {}",
        ss.addresses, ss.resolved_by, ss.uses, ss.about, ss.tried
    );
    Ok(())
}
