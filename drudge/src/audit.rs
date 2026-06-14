//! Audit — check ingestion state: origin/kind/project distribution + quality warnings.
//! Answers "is the ingestion a mess?" with actual DB aggregation rather than guesswork. Aggregation is done in Rust (avoiding SQL GROUP).
//!
//! SRP: `stats()` returns data (shared by HTTP·CLI), `run()` is the CLI stdout shell.
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

/// `stats()` return value — used by both the HTTP handler and the CLI.
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

/// Pure logic: DB aggregation → returns `AuditStats`. No I/O.
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

/// CLI shell: call `stats()` then print to stdout.
pub async fn run(store: &Store) -> Result<()> {
    let metas = store.all_meta().await?;
    let total = metas.len();
    let files: HashSet<&str> = metas.iter().map(|m| m.source_path.as_str()).collect();
    println!(
        "📊 Ingest audit — chunks {total} · source files {}\n",
        files.len()
    );

    print_group("origin", &tally(metas.iter().map(|m| m.origin.as_str())));
    print_group("kind", &tally(metas.iter().map(|m| m.kind.as_str())));
    print_group("project", &tally(metas.iter().map(|m| m.project.as_str())));

    let company = metas
        .iter()
        .filter(|m| crate::frontmatter::is_company_path(&m.source_path))
        .count();
    let no_origin = metas.iter().filter(|m| m.origin.is_empty()).count();
    let no_project = metas.iter().filter(|m| m.project.is_empty()).count();
    println!("\n  ⚠️ Quality check");
    println!("    company pollution : {company}  (expected 0)");
    println!("    origin missing    : {no_origin}");
    println!("    project missing   : {no_project}");
    let clean = company == 0 && no_origin == 0;
    println!(
        "    → {}",
        if clean {
            "✅ clean"
        } else {
            "❌ needs review"
        }
    );

    let gs = store.graph_stats().await?;
    println!(
        "\n  [graph] documents {} · chunks {} · project {} · topic {} · edges {}",
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
