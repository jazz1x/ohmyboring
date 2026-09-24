//! ohmyboring personal RAG — Rust (pgvector: vector + node/edge graph + recursive CTE + audit).
//! First milestone: embed → store → vector search round-trip proof (selftest).
use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use clap::{Parser, Subcommand};
use drudge::{
    ask, audit, code_index, config, frontmatter, graph, ingest, llm, retrieve, serve, store, vault,
};
use std::path::PathBuf;

fn vault_wiki_dir(vault_dir: Option<&str>, home_dir: Option<&str>) -> Result<String> {
    let vault_root = match vault_dir {
        Some(path) => PathBuf::from(path),
        None => PathBuf::from(
            home_dir.context("BORING_VAULT_DIR and HOME are unset; cannot locate vault/wiki")?,
        )
        .join("oh-my-boring/vault"),
    };
    Ok(vault_root.join("wiki").to_string_lossy().into_owned())
}

#[derive(Parser)]
#[command(
    name = "drudge",
    about = "ohmyboring personal RAG (Rust, pgvector + graph CTE)"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Stack self-test: Llm embed → pgvector store → vector search round-trip
    Selftest,
    /// Number of stored documents
    Stats,
    /// Compatibility re-ingest of curated vault/wiki notes (not session sources or raw code)
    Ingest,
    /// Ingestion audit — origin/kind/project distribution + quality warnings
    Audit,
    /// Retrieval test (vector + BM25 RRF)
    Search { query: String },
    /// Print the commit this binary was built from — the host answer to /health.build_sha
    Version,
    /// Single query — retrieval + LLM synthesis + sources
    Ask { question: String },
    /// Recency-first briefing — recency (updated_at) retrieval + supersede synthesis (morning briefing)
    Brief,
    /// Graph-expanded retrieval — vector hits → graph (edge) 1-hop neighbors
    Graph { query: String },
    /// Deterministic re-ingest: walk → embed → upsert → graph (from frontmatter) → relations
    Sync,
    /// Synchronize explicitly enabled source-code repositories into the isolated code_index corpus
    CodeSync {
        /// Stable repository id (default: every enabled code_index source)
        #[arg(long)]
        repository: Option<String>,
    },
    /// Inspect the isolated source-code corpus
    CodeStatus {
        /// Stable repository id (default: every indexed repository)
        #[arg(long)]
        repository: Option<String>,
    },
    /// Delete orphan semantic nodes (remove edge-unreferenced legacy remnants)
    Gc,
    /// Show loaded boring.json config (for debugging / migration verification)
    Config,
    /// Graph projection — write Postgres doc↔doc relations as wiki relates_to wikilinks (Obsidian)
    Link {
        /// vault root (default: $BORING_VAULT_DIR or $HOME/oh-my-boring/vault)
        #[arg(long)]
        vault: Option<String>,
    },
    /// HTTP resident daemon — ingest/ask/graph/audit API + background scheduler
    Serve,
    /// Maintenance compact — VACUUM/ANALYZE + REINDEX + prune old query_log + GC orphans
    Compact,
    /// Recent query/retrieval log (memory usage analytics)
    QueryLog {
        #[arg(short, long, default_value = "50")]
        limit: i64,
    },
    /// Personal vault lint/audit/maintenance
    Vault {
        #[command(subcommand)]
        sub: VaultCmd,
    },
}

#[derive(Subcommand)]
enum VaultCmd {
    /// vault/wiki/*.md consistency check (schema · frontmatter · wikilink · sources)
    Lint {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// Treat warnings as errors too (exit 2)
        #[arg(long)]
        strict: bool,
    },
    /// vault graph audit (orphan · connected components · superseded)
    Audit {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// Treat warnings as errors too (exit 2)
        #[arg(long)]
        strict: bool,
    },
}

#[tokio::main]
#[allow(clippy::too_many_lines)]
async fn main() -> Result<()> {
    // Rejection message vector-only CLI commands return when off (not silent, ROP). The daemon (serve) runs in wiki mode when off.
    const VEC_OFF: &str = "BORING_VECTOR=off — this command requires the vector backend (pgvector). The daemon (serve) runs in wiki-recall mode when off.";

    let cli = Cli::parse();
    // BORING_VECTOR: default off = wiki first-class (no Postgres connection, simple). Turn on to enable pgvector (vector+graph).
    // unset/off → don't open Store → start engine/CLI without Postgres. (aligned with the wiki-primary trend)
    let cfg = config::BoringConfig::load(None)?;

    let cmd = match cli.cmd {
        Cmd::CodeSync { repository } => {
            let selected: Vec<_> = cfg
                .code_index
                .sources
                .iter()
                .filter(|source| source.enabled())
                .filter(|source| {
                    repository
                        .as_deref()
                        .is_none_or(|requested| requested == source.id())
                })
                .collect();
            anyhow::ensure!(
                !selected.is_empty(),
                "no enabled code_index source matched{}",
                repository
                    .as_deref()
                    .map_or_else(String::new, |id| format!(" repository '{id}'"))
            );
            let dsn = config::pg_dsn();
            let mut code_store = code_index::CodeIndexStore::connect(&dsn)?;
            code_store.initialize().await?;
            for source in selected {
                let report = code_index::sync_repository(&mut code_store, source).await?;
                println!(
                    "code-sync {}: scanned={} changed={} unchanged={} deleted={} parse_errors={}",
                    report.repository_id,
                    report.scanned,
                    report.changed,
                    report.unchanged,
                    report.deleted,
                    report.parse_errors
                );
            }
            return Ok(());
        }
        Cmd::CodeStatus { repository } => {
            let dsn = config::pg_dsn();
            let code_store = code_index::CodeIndexStore::connect(&dsn)?;
            let statuses = code_store.status(repository.as_deref()).await?;
            if let Some(requested) = repository.as_deref() {
                anyhow::ensure!(
                    !statuses.is_empty(),
                    "repository '{requested}' is not present in the code index"
                );
            }
            for status in statuses {
                let synced = DateTime::<Utc>::from(status.last_synced_at);
                println!(
                    "{} ({}) language={} root={} files={} symbols={} relations={} error_files={} parse_errors={} synced={}",
                    status.repository_id,
                    status.name,
                    status.language,
                    status.root_path,
                    status.files,
                    status.symbols,
                    status.relations,
                    status.files_with_errors,
                    status.parse_errors,
                    synced.format("%Y-%m-%dT%H:%M:%SZ")
                );
            }
            return Ok(());
        }
        command => command,
    };

    let vector_on = config::env_set("BORING_VECTOR")
        .is_some_and(|v| matches!(v.to_lowercase().as_str(), "on" | "1" | "true" | "yes"));
    let store: Option<store::Store> = if vector_on {
        let dsn = config::pg_dsn();
        // embed_dim (boring.json) sizes the vector columns — the kernel's only model knob.
        Some(store::Store::open(&dsn, cfg.embed_dim as usize).await?)
    } else {
        None
    };

    match cmd {
        Cmd::Version => {
            // Empty means the build could not resolve a commit (no .git, no BUILD_SHA), which is
            // a different statement from a stale sha and must not be dressed up as one.
            let stamp = env!("BORING_BUILT_FROM");
            if stamp.is_empty() {
                println!("unstamped");
            } else {
                println!("{stamp}");
            }
            return Ok(());
        }
        Cmd::Selftest => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let docs = [
                (
                    "doc:rust",
                    "Rust is a systems programming language that delivers memory safety and performance at once.",
                ),
                (
                    "doc:coffee",
                    "Espresso is extracted by forcing hot water through finely ground beans at high pressure.",
                ),
                (
                    "doc:db",
                    "Postgres provides vector search via pgvector and graphs via node/edge tables with recursive CTEs.",
                ),
            ];
            println!("1) embed + store ({} docs)", docs.len());
            for (id, text) in docs {
                let emb = ol.embed(text).await?;
                let front = frontmatter::FrontMatter {
                    origin: "personal".to_owned(),
                    project: "oh-my-boring".to_owned(),
                    source_path: (*id).to_owned(),
                    ..Default::default()
                };
                // chunk.source_path REFERENCES document(source_path) — call upsert_document
                // first to guarantee the parent record so the FK is satisfied.
                store
                    .upsert_document(&front, "selftest", std::time::SystemTime::now())
                    .await?;
                store
                    .upsert_chunk(&store::Doc {
                        id: (*id).into(),
                        content: (*text).into(),
                        embedding: emb,
                        front,
                        chunk_idx: 0,
                    })
                    .await?;
            }

            let query = "how to use vectors and graphs in a database";
            println!("2) query: {query:?}");
            let qe = ol.embed(query).await?;
            let hits = store.vector_search(&qe, 3).await?;
            println!("3) vector search results (top-{}):", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(34).collect();
                println!("   [dist={:.4}] {} ({}) — {}", h.dist, h.id, h.origin, snip);
            }
            // GOAL check: the 'db' document must rank first (semantically closest to the query)
            match hits.first() {
                Some(h) if h.id == "doc:db" => {
                    println!("✅ ranking correct (doc:db #1) — vector search OK");
                }
                Some(h) => println!("⚠️ #1 is not doc:db: {} — check embedding/distance", h.id),
                None => println!("❌ 0 hits — vector search still failing"),
            }
        }
        Cmd::Stats => {
            let store = store.as_ref().context(VEC_OFF)?;
            println!("knowledge docs: {}", store.count().await?);
        }
        Cmd::Ingest => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let vault_dir = config::env_set("BORING_VAULT_DIR");
            let home_dir = config::env_set("HOME");
            let corpus = [vault_wiki_dir(vault_dir.as_deref(), home_dir.as_deref())?];
            println!("sources: {corpus:?}");
            let s = ingest::run(store, &ol, &cfg, &corpus).await?;
            println!(
                "scanned={} new={} updated={} unchanged={} deleted={} kept_vanished={} skipped={} chunks={}",
                s.scanned,
                s.new,
                s.updated,
                s.unchanged,
                s.deleted,
                s.kept_vanished,
                s.skipped,
                s.chunks
            );
        }
        Cmd::Audit => {
            let store = store.as_ref().context(VEC_OFF)?;
            audit::run(store, cfg.allow_company_origin).await?;
        }
        Cmd::Search { query } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let hits = retrieve::retrieve(store, &ol, &query, 5, &[], None, None).await?;
            println!("'{query}' → {} hits", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(50).collect();
                let kind_label = h.dist_kind.as_str();
                println!(
                    "  [dist={:.4} {kind_label}] [{}/{}] {} — {snip}",
                    h.dist, h.origin, h.project, h.id
                );
            }
        }
        Cmd::Ask { question } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            ask::run(store, &ol, &question, &[], None, None).await?;
        }
        Cmd::Brief => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let (out, _injected) = ask::brief(store, &ol, &[], cfg.note_lang.as_str()).await?;
            println!("{}\n", out.answer);
            if !out.sources.is_empty() {
                println!("sources:");
                for src in &out.sources {
                    println!("  - {src}");
                }
            }
        }
        Cmd::Graph { query } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            graph::run(store, &ol, &query).await?;
        }
        Cmd::Sync => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            // Kernel A corpus = the vault's wiki dir (agent-written notes), not raw transcripts.
            let vault_dir = config::env_set("BORING_VAULT_DIR");
            let home_dir = config::env_set("HOME");
            let corpus = [vault_wiki_dir(vault_dir.as_deref(), home_dir.as_deref())?];
            let is = ingest::run(store, &ol, &cfg, &corpus).await?;
            println!(
                "sync: ingest(new={} updated={} deleted={} kept_vanished={} chunks={}) graph(tools={} concepts={} claims={} claims_unchanged={} edges={})",
                is.new,
                is.updated,
                is.deleted,
                is.kept_vanished,
                is.chunks,
                is.tools,
                is.concepts,
                is.claims,
                is.claims_unchanged,
                is.edges,
            );
            let ss = store.semantic_stats().await?;
            println!(
                "semantic audit: tool {} · concept {} · uses {} · about {}",
                ss.tools, ss.concepts, ss.uses, ss.about
            );
        }
        Cmd::CodeSync { .. } | Cmd::CodeStatus { .. } => {}
        Cmd::Link { vault } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let vault_root = vault
                .or_else(|| config::env_set("BORING_VAULT_DIR"))
                .unwrap_or_else(|| {
                    format!(
                        "{}/oh-my-boring/vault",
                        std::env::var("HOME").unwrap_or_default()
                    )
                });
            let n = vault::project_links(store, std::path::Path::new(&vault_root), 6).await?;
            println!("graph→obsidian: updated relates_to of {n} wiki notes");
        }
        Cmd::Gc => {
            let store = store.as_ref().context(VEC_OFF)?;
            let gc = store.gc_orphans().await?;
            println!(
                "gc orphans — tool: {} · concept: {} · claim nodes: {} · claim edges: {} · total: {}",
                gc.tool,
                gc.concept,
                gc.claim_nodes,
                gc.claim_edges,
                gc.total()
            );
        }
        Cmd::Config => {
            println!("{}", serde_json::to_string_pretty(&cfg)?);
        }
        Cmd::Serve => {
            let ol = llm::Llm::from_config(&cfg);
            // Move store ownership into serve::run — single-process DB owner pattern.
            serve::run(store, ol, cfg).await?;
        }
        Cmd::Compact => {
            let store = store.as_ref().context(VEC_OFF)?;
            let summary = store.compact().await?;
            println!(
                "compact done — vacuum {}ms, reindex {}ms, prune_query_log {}, gc(tool {} concept {} claim nodes {} claim edges {}), total {}ms",
                summary.report.vacuum_ms,
                summary.report.reindex_ms,
                summary.report.prune_query_log,
                summary.report.gc_tool,
                summary.report.gc_concept,
                summary.report.gc_claim_nodes,
                summary.report.gc_claim_edges,
                summary.total_ms,
            );
        }
        Cmd::QueryLog { limit } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let rows = store.recent_queries(limit.clamp(1, 1000)).await?;
            for r in rows {
                let ts = format!("{:?}", r.created_at);
                println!(
                    "[{}] {:<10} {:>5}ms  q={:?}  hits={:?}",
                    ts,
                    r.endpoint,
                    r.latency_ms
                        .map_or_else(|| "?".to_string(), |n| n.to_string()),
                    r.query,
                    if r.hit_paths.is_empty() {
                        r.sources
                    } else {
                        r.hit_paths
                    }
                );
            }
        }
        Cmd::Vault { sub } => {
            let default_vault = format!(
                "{}/oh-my-boring/vault",
                std::env::var("HOME").unwrap_or_default()
            );
            match sub {
                VaultCmd::Lint { vault, strict } => {
                    // Lint does not need Postgres — release the connection.
                    drop(store);
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let code = vault::run_lint(&vault_root, strict)?;
                    std::process::exit(code);
                }
                VaultCmd::Audit { vault, strict } => {
                    // Audit does not need Postgres either.
                    drop(store);
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let code = vault::run_audit(&vault_root, strict)?;
                    std::process::exit(code);
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use clap::Parser;

    use super::{Cli, Cmd, vault_wiki_dir};

    #[test]
    fn code_index_subcommands_parse_repository_selection() {
        let sync = Cli::try_parse_from(["drudge", "code-sync", "--repository", "widget"]).unwrap();
        assert!(matches!(
            sync.cmd,
            Cmd::CodeSync { repository } if repository.as_deref() == Some("widget")
        ));

        let status = Cli::try_parse_from(["drudge", "code-status"]).unwrap();
        assert!(matches!(status.cmd, Cmd::CodeStatus { repository: None }));
    }

    #[test]
    fn vault_wiki_dir_prefers_explicit_vault() {
        let path = vault_wiki_dir(Some("/srv/boring-vault"), Some("/home/user")).unwrap();
        assert_eq!(path, "/srv/boring-vault/wiki");
    }

    #[test]
    fn vault_wiki_dir_uses_home_default() {
        let path = vault_wiki_dir(None, Some("/home/user")).unwrap();
        assert_eq!(path, "/home/user/oh-my-boring/vault/wiki");
    }

    #[test]
    fn vault_wiki_dir_requires_vault_or_home() {
        let error = vault_wiki_dir(None, None).unwrap_err();
        assert_eq!(
            error.to_string(),
            "BORING_VAULT_DIR and HOME are unset; cannot locate vault/wiki"
        );
    }
}
