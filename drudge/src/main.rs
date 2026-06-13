//! oh-my-boring personal RAG — Rust (pgvector: vector + node/edge graph + recursive CTE + audit).
//! First milestone: embed → store → vector search round-trip proof (selftest).
mod ask;
mod audit;
mod distill;
mod extract;
mod frontmatter;
mod graph;
mod ingest;
mod llm;
mod retrieve;
mod serve;
mod store;
mod vault;
mod wiki_recall;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "drudge",
    about = "oh-my-boring 개인 RAG (Rust, pgvector + 그래프 CTE)"
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
    /// LLM extraction — generate per-document problem/solution/tool/concept nodes + edges
    Extract,
    /// Self-augmentation loop: run ingest → extract sequentially (for cron invocation)
    Sync,
    /// Delete orphan semantic nodes (remove edge-unreferenced legacy remnants)
    Gc,
    /// Graph projection — write Postgres doc↔doc relations as wiki relates_to wikilinks (Obsidian)
    Link {
        /// vault root (default: $DRUDGE_VAULT_DIR or $HOME/oh-my-boring/vault)
        #[arg(long)]
        vault: Option<String>,
    },
    /// HTTP resident daemon — ingest/ask/graph/audit API + background scheduler
    Serve,
    /// Personal vault lint/audit
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
    /// Compile raw/*.md → curated wiki-NNNN.md (LLM curation, idempotent)
    Compile {
        /// vault root path (default: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// raw note directory (default: <vault>/raw)
        #[arg(long)]
        raw: Option<String>,
        /// Date override (YYYY-MM-DD, default: today)
        #[arg(long)]
        date: Option<String>,
    },
}

#[tokio::main]
#[allow(clippy::too_many_lines)]
async fn main() -> Result<()> {
    // Rejection message vector-only CLI commands return when off (not silent, ROP). The daemon (serve) runs in wiki mode when off.
    const VEC_OFF: &str = "DRUDGE_VECTOR=off — 이 명령은 벡터 백엔드(pgvector)가 필요합니다. 데몬(serve)은 off 면 wiki 회수로 동작합니다.";

    let cli = Cli::parse();
    // DRUDGE_VECTOR: default off = wiki first-class (no Postgres connection, simple). Turn on to enable pgvector (vector+graph).
    // unset/off → don't open Store → start engine/CLI without Postgres. (aligned with the wiki-primary trend)
    let vector_on = std::env::var("DRUDGE_VECTOR")
        .is_ok_and(|v| matches!(v.to_lowercase().as_str(), "on" | "1" | "true" | "yes"));
    let store: Option<store::Store> = if vector_on {
        let dsn = std::env::var("PG_DSN")
            .unwrap_or_else(|_| "postgresql://boring:boring@localhost:5432/boring".to_owned());
        Some(store::Store::open(&dsn).await?)
    } else {
        None
    };

    match cli.cmd {
        Cmd::Selftest => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            let docs = [
                (
                    "doc:rust",
                    "Rust는 메모리 안전성과 성능을 동시에 주는 시스템 프로그래밍 언어다.",
                ),
                (
                    "doc:coffee",
                    "에스프레소는 곱게 간 원두에 뜨거운 물을 높은 압력으로 통과시켜 추출한다.",
                ),
                (
                    "doc:db",
                    "Postgres는 pgvector로 벡터검색을, node/edge 테이블과 재귀 CTE로 그래프를 제공한다.",
                ),
            ];
            println!("1) 임베딩 + 저장 ({}개 문서)", docs.len());
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

            let query = "데이터베이스에서 벡터와 그래프를 쓰는 법";
            println!("2) 쿼리: {query:?}");
            let qe = ol.embed(query).await?;
            let hits = store.vector_search(&qe, 3).await?;
            println!("3) 벡터 검색 결과 (top-{}):", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(34).collect();
                println!("   [dist={:.4}] {} ({}) — {}", h.dist, h.id, h.origin, snip);
            }
            // GOAL check: the 'db' document must rank first (semantically closest to the query)
            match hits.first() {
                Some(h) if h.id == "doc:db" => {
                    println!("✅ 랭킹 정확 (doc:db 1위) — 벡터검색 정상");
                }
                Some(h) => println!("⚠️ 1위가 doc:db 아님: {} — 임베딩/거리 점검 필요", h.id),
                None => println!("❌ 0건 — 벡터검색 여전히 실패"),
            }
        }
        Cmd::Stats => {
            let store = store.as_ref().context(VEC_OFF)?;
            println!("knowledge docs: {}", store.count().await?);
        }
        Cmd::Ingest => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            let home = std::env::var("HOME").unwrap_or_default();
            let dirs = std::env::var("DRUDGE_SOURCE_DIRS").unwrap_or_else(|_| {
                format!(
                    "{home}/.claude/projects:{home}/oh-my-boring/data/notes:{home}/oh-my-boring/vault/wiki"
                )
            });
            let source_dirs: Vec<String> = dirs.split(':').map(str::to_owned).collect();
            println!("sources: {source_dirs:?}");
            let s = ingest::run(store, &ol, &source_dirs).await?;
            println!(
                "scanned={} new={} updated={} unchanged={} deleted={} skipped={} chunks={}",
                s.scanned, s.new, s.updated, s.unchanged, s.deleted, s.skipped, s.chunks
            );
        }
        Cmd::Audit => {
            let store = store.as_ref().context(VEC_OFF)?;
            audit::run(store).await?;
        }
        Cmd::Search { query } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            let hits = retrieve::retrieve(store, &ol, &query, 5, &[]).await?;
            println!("'{query}' → {} hits", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(50).collect();
                println!("  [{}/{}] {} — {snip}", h.origin, h.project, h.id);
            }
        }
        Cmd::Ask { question } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            ask::run(store, &ol, &question, &[]).await?;
        }
        Cmd::Brief => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            let out = ask::brief(store, &ol, &[]).await?;
            println!("{}\n", out.answer);
            if !out.sources.is_empty() {
                println!("출처:");
                for src in &out.sources {
                    println!("  - {src}");
                }
            }
        }
        Cmd::Graph { query } => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            graph::run(store, &ol, &query).await?;
        }
        Cmd::Extract => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            let s = extract::run(store, &ol).await?;
            println!(
                "extract: processed={} skipped={} problems={} solutions={} tools={} concepts={} attempts={} edges={}",
                s.processed,
                s.skipped,
                s.problems,
                s.solutions,
                s.tools,
                s.concepts,
                s.attempts,
                s.edges
            );
            // Semantic stats audit output (for idempotency verification)
            let ss = store.semantic_stats().await?; // store: &Store (passed the guard)
            println!(
                "semantic audit: problem {} · solution {} · tool {} · concept {} · attempt {}",
                ss.problems, ss.solutions, ss.tools, ss.concepts, ss.attempts
            );
            println!(
                "semantic edges: addresses {} · resolved_by {} · uses {} · about {} · tried {}",
                ss.addresses, ss.resolved_by, ss.uses, ss.about, ss.tried
            );
        }
        Cmd::Sync => {
            let store = store.as_ref().context(VEC_OFF)?;
            let ol = llm::Llm::from_env();
            let home = std::env::var("HOME").unwrap_or_default();
            let dirs = std::env::var("DRUDGE_SOURCE_DIRS").unwrap_or_else(|_| {
                format!(
                    "{home}/.claude/projects:{home}/oh-my-boring/data/notes:{home}/oh-my-boring/vault/wiki"
                )
            });
            let source_dirs: Vec<String> = dirs.split(':').map(str::to_owned).collect();
            let is = ingest::run(store, &ol, &source_dirs).await?;
            let es = extract::run(store, &ol).await?;
            println!(
                "sync: ingest(new={} updated={} deleted={} chunks={}) + extract(processed={} skipped={} nodes={} edges={})",
                is.new,
                is.updated,
                is.deleted,
                is.chunks,
                es.processed,
                es.skipped,
                es.problems + es.solutions + es.tools + es.concepts + es.attempts,
                es.edges,
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
            println!("graph→obsidian: {n} wiki 노트의 relates_to 갱신");
        }
        Cmd::Gc => {
            let store = store.as_ref().context(VEC_OFF)?;
            let gc = store.gc_orphans().await?;
            println!(
                "gc orphans — tool: {} · concept: {} · problem: {} · solution: {} · attempt: {} · total: {}",
                gc.tool,
                gc.concept,
                gc.problem,
                gc.solution,
                gc.attempt,
                gc.total()
            );
        }
        Cmd::Serve => {
            let ol = llm::Llm::from_env();
            // Move store ownership into serve::run — single-process DB owner pattern.
            serve::run(store, ol).await?;
        }
        Cmd::Vault { sub } => {
            // vault commands don't need store (Postgres) — drop it to release the connection.
            drop(store);
            let default_vault = format!(
                "{}/oh-my-boring/vault",
                std::env::var("HOME").unwrap_or_default()
            );
            match sub {
                VaultCmd::Lint { vault, strict } => {
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let code = vault::run_lint(&vault_root, strict)?;
                    std::process::exit(code);
                }
                VaultCmd::Audit { vault, strict } => {
                    let vault_root = std::path::PathBuf::from(vault.unwrap_or(default_vault));
                    let code = vault::run_audit(&vault_root, strict)?;
                    std::process::exit(code);
                }
                VaultCmd::Compile { vault, raw, date } => {
                    let ol = llm::Llm::from_env();
                    let vault_root =
                        std::path::PathBuf::from(vault.unwrap_or_else(|| default_vault.clone()));
                    let raw_dir =
                        raw.map_or_else(|| vault_root.join("raw"), std::path::PathBuf::from);
                    // today: the --date argument, or the system date if absent (the I/O boundary is the single vault::today_utc)
                    let today = date.unwrap_or_else(vault::today_utc);
                    let s = vault::run_compile(&vault_root, &raw_dir, &today, &ol).await?;
                    println!(
                        "compile: total_raw={} compiled={} recompiled={} skipped={}",
                        s.total_raw, s.compiled, s.recompiled, s.skipped
                    );
                }
            }
        }
    }
    Ok(())
}
