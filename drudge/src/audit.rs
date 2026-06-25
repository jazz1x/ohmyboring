//! Audit — check ingestion state: origin/kind/project distribution + quality warnings.
//!
//! Cross-reference: ENFORCEMENT.md §A (ADT) · design decision D7 (vault/wiki SSOT).
//!
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

fn print_group(label: &str, rows: &[(String, usize)]) {
    println!("  [{label}]");
    for (k, c) in rows {
        let key = if k.is_empty() { "(empty)" } else { k.as_str() };
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
    /// Whether company-origin notes are accepted by policy (`allow_company_origin`). When true,
    /// `company_contamination` is reported for visibility but does not flip `clean`.
    pub company_allowed: bool,
    pub missing_origin: usize,
    pub missing_project: usize,
    pub clean: bool,
    pub graph_documents: usize,
    pub graph_chunks: usize,
    pub graph_projects: usize,
    pub graph_topics: usize,
    pub graph_edges: usize,
    pub semantic_tools: usize,
    pub semantic_concepts: usize,
    pub semantic_uses: usize,
    pub semantic_about: usize,
}

/// Pure logic: DB aggregation → returns `AuditStats`. No I/O.
/// `allow_company` reflects the `allow_company_origin` policy: when true, company-origin notes do not
/// flip the `clean` flag (they are accepted session experience, not contamination).
pub async fn stats(store: &Store, allow_company: bool) -> Result<AuditStats> {
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

    let company_contamination = metas.iter().filter(|m| m.origin == "company").count();
    let missing_origin = metas.iter().filter(|m| m.origin.is_empty()).count();
    let missing_project = metas.iter().filter(|m| m.project.is_empty()).count();
    let clean = (allow_company || company_contamination == 0) && missing_origin == 0;

    let gs = store.graph_stats().await?;
    let ss = store.semantic_stats().await?;

    Ok(AuditStats {
        total_chunks,
        total_files,
        by_origin,
        by_kind,
        by_project,
        company_contamination,
        company_allowed: allow_company,
        missing_origin,
        missing_project,
        clean,
        graph_documents: gs.documents,
        graph_chunks: gs.chunks,
        graph_projects: gs.projects,
        graph_topics: gs.topics,
        graph_edges: gs.edges,
        semantic_tools: ss.tools,
        semantic_concepts: ss.concepts,
        semantic_uses: ss.uses,
        semantic_about: ss.about,
    })
}

/// CLI shell: consume `stats()` and print to stdout. No re-aggregation (composition over duplication).
pub async fn run(store: &Store, allow_company: bool) -> Result<()> {
    let s = stats(store, allow_company).await?;
    println!(
        "📊 Ingest audit — chunks {} · source files {}\n",
        s.total_chunks, s.total_files
    );

    print_group("origin", &s.by_origin);
    print_group("kind", &s.by_kind);
    print_group("project", &s.by_project);

    println!("\n  ⚠️ Quality check");
    println!(
        "    company origin    : {}  ({})",
        s.company_contamination,
        if s.company_allowed {
            "allowed by policy"
        } else {
            "expected 0"
        }
    );
    println!("    origin missing    : {}", s.missing_origin);
    println!("    project missing   : {}", s.missing_project);
    println!(
        "    → {}",
        if s.clean {
            "✅ clean"
        } else {
            "❌ needs review"
        }
    );

    println!(
        "\n  [graph] documents {} · chunks {} · project {} · topic {} · edges {}",
        s.graph_documents, s.graph_chunks, s.graph_projects, s.graph_topics, s.graph_edges
    );
    println!(
        "  [semantic] tool {} · concept {}",
        s.semantic_tools, s.semantic_concepts
    );
    println!(
        "  [semantic edges] uses {} · about {}",
        s.semantic_uses, s.semantic_about
    );
    Ok(())
}
