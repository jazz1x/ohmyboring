//! ohmyboring personal RAG — Rust (pgvector: vector + node/edge graph + recursive CTE + audit).
//! First milestone: embed → store → vector search round-trip proof (selftest).
use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use drudge::{
    ask, audit, config, frontmatter, graph, ingest, llm, renumber, retrieve, serve, store, vault,
};

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
    /// source → frontmatter → chunking → embedding → ingest (idempotent, excludes example-company)
    Ingest,
    /// Ingestion audit — origin/kind/project distribution + quality warnings
    Audit,
    /// Retrieval test (vector + BM25 RRF)
    Search { query: String },
    /// Single query — retrieval + LLM synthesis + sources
    Ask { question: String },
    /// Recency-first briefing — recency (updated_at) retrieval + supersede synthesis (morning briefing)
    Brief,
    /// Graph-expanded retrieval — vector hits → graph (edge) 1-hop neighbors
    Graph { query: String },
    /// Deterministic re-ingest: walk → embed → upsert → graph (from frontmatter) → relations
    Sync,
    /// Delete orphan semantic nodes (remove edge-unreferenced legacy remnants)
    Gc,
    /// Show loaded boring.json config (for debugging / migration verification)
    Config,
    /// Graph projection — write Postgres doc↔doc relations as wiki relates_to wikilinks (Obsidian)
    Link {
        /// vault root (default: $DRUDGE_VAULT_DIR or $HOME/oh-my-boring/vault)
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
    /// Compact wiki ids — renumber vault/wiki files to remove gaps (dry-run by default)
    Renumber {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// Actually apply the plan (default is dry-run)
        #[arg(long)]
        apply: bool,
        /// After applying, run a vector-mode sync to rebuild the DB
        #[arg(long)]
        sync: bool,
    },
}

#[tokio::main]
#[allow(clippy::too_many_lines)]
async fn main() -> Result<()> {
    // Rejection message vector-only CLI commands return when off (not silent, ROP). The daemon (serve) runs in wiki mode when off.
    const VEC_OFF: &str = "DRUDGE_VECTOR=off — this command requires the vector backend (pgvector). The daemon (serve) runs in wiki-recall mode when off.";

    let cli = Cli::parse();
    // DRUDGE_VECTOR: default off = wiki first-class (no Postgres connection, simple). Turn on to enable pgvector (vector+graph).
    // unset/off → don't open Store → start engine/CLI without Postgres. (aligned with the wiki-primary trend)
    let cfg = config::BoringConfig::load(None)?;

    let vector_on = std::env::var("DRUDGE_VECTOR")
        .is_ok_and(|v| matches!(v.to_lowercase().as_str(), "on" | "1" | "true" | "yes"));
    let store: Option<store::Store> = if vector_on {
        let dsn = std::env::var("PG_DSN")
            .unwrap_or_else(|_| "postgresql://boring:boring@localhost:5432/boring".to_owned());
        // embed_dim (boring.json) sizes the vector columns — the kernel's only model knob.
        Some(store::Store::open(&dsn, cfg.embed_dim as usize).await?)
    } else {
        None
    };

    match cli.cmd {
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
            let source_dirs = cfg.source_dirs();
            println!("sources: {source_dirs:?}");
            let s = ingest::run(store, &ol, &cfg, &source_dirs).await?;
            println!(
                "scanned={} new={} updated={} unchanged={} deleted={} skipped={} chunks={}",
                s.scanned, s.new, s.updated, s.unchanged, s.deleted, s.skipped, s.chunks
            );
        }
        Cmd::Audit => {
            let store = store.as_ref().context(VEC_OFF)?;
            audit::run(store, cfg.allow_company_origin).await?;
        }
        Cmd::Search { query } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let hits = retrieve::retrieve(store, &ol, &query, 5, &[]).await?;
            println!("'{query}' → {} hits", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(50).collect();
                println!("  [{}/{}] {} — {snip}", h.origin, h.project, h.id);
            }
        }
        Cmd::Ask { question } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            ask::run(store, &ol, &question, &[]).await?;
        }
        Cmd::Brief => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_config(&cfg);
            let out = ask::brief(store, &ol, &[], cfg.note_lang.as_str()).await?;
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
            let vault_wiki = std::env::var("DRUDGE_VAULT_DIR").map_or_else(
                |_| {
                    format!(
                        "{}/oh-my-boring/vault/wiki",
                        std::env::var("HOME").unwrap_or_default()
                    )
                },
                |v| format!("{v}/wiki"),
            );
            let is = ingest::run(store, &ol, &cfg, &[vault_wiki]).await?;
            println!(
                "sync: ingest(new={} updated={} deleted={} chunks={}) graph(tools={} concepts={} claims={} edges={})",
                is.new,
                is.updated,
                is.deleted,
                is.chunks,
                is.tools,
                is.concepts,
                is.claims,
                is.edges,
            );
            let ss = store.semantic_stats().await?;
            println!(
                "semantic audit: tool {} · concept {} · uses {} · about {}",
                ss.tools, ss.concepts, ss.uses, ss.about
            );
        }
        Cmd::Link { vault } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let vault_root = vault
                .or_else(|| std::env::var("DRUDGE_VAULT_DIR").ok())
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
                "gc orphans — tool: {} · concept: {} · total: {}",
                gc.tool,
                gc.concept,
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
                "compact done — vacuum {}ms, reindex {}ms, prune_query_log {}, gc(tool {} concept {}), total {}ms",
                summary.report.vacuum_ms,
                summary.report.reindex_ms,
                summary.report.prune_query_log,
                summary.report.gc_tool,
                summary.report.gc_concept,
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
                VaultCmd::Renumber { vault, apply, sync } => {
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or_else(|| {
                        std::env::var("DRUDGE_VAULT_DIR").unwrap_or(default_vault)
                    }));
                    let wiki_dir = vault_root.join("wiki");
                    let plan = renumber::plan(&wiki_dir)?;
                    if plan.is_noop() {
                        println!("vault renumber: no gaps — nothing to do");
                        return Ok(());
                    }
                    println!("vault renumber plan ({} moves):", plan.moves.len());
                    for m in &plan.moves {
                        let action = if m.old_id == m.new_id {
                            format!("{}  (content references rewritten)", m.old_path.display())
                        } else {
                            format!(
                                "{} → {}",
                                m.old_path.file_name().unwrap_or_default().to_string_lossy(),
                                m.new_path.file_name().unwrap_or_default().to_string_lossy()
                            )
                        };
                        println!("  {action}");
                    }
                    if !apply {
                        println!("\nThis was a dry-run. Pass --apply to execute.");
                        return Ok(());
                    }
                    renumber::apply(&plan)?;
                    println!("renumber applied — {} files rewritten", plan.moves.len());
                    if sync {
                        let store = store.as_ref().context(VEC_OFF)?;
                        let ol = llm::Llm::from_config(&cfg);
                        let stats = ingest::run(
                            store,
                            &ol,
                            &cfg,
                            &[wiki_dir.to_string_lossy().into_owned()],
                        )
                        .await?;
                        println!(
                            "sync: new={} updated={} deleted={} chunks={}",
                            stats.new, stats.updated, stats.deleted, stats.chunks
                        );
                    }
                }
            }
        }
    }
    Ok(())
}
