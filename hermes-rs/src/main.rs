//! Olympus 개인 RAG — Rust (pgvector: vector + node/edge graph + 재귀 CTE + audit).
//! 1차 마일스톤: embed → store → vector search 왕복 증명(selftest).
mod ask;
mod audit;
mod distill;
mod extract;
mod frontmatter;
mod graph;
mod ingest;
mod ollama;
mod retrieve;
mod serve;
mod store;
mod vault;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "hermes",
    about = "Olympus 개인 RAG (Rust, pgvector + 그래프 CTE)"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 스택 자가테스트: Ollama 임베딩 → pgvector 저장 → 벡터 검색 왕복
    Selftest,
    /// 보유 문서 수
    Stats,
    /// 소스 → frontmatter → 청킹 → 임베딩 → 적재 (멱등, example-company 제외)
    Ingest,
    /// 적재 감사 — origin/kind/project 분포 + 품질 경고
    Audit,
    /// 회수 테스트 (vector + BM25 RRF)
    Search { query: String },
    /// 질의 1회 — 회수 + LLM 합성 + 출처
    Ask { question: String },
    /// 최신우선 브리핑 — 최근성(updated_at) 회수 + supersede 합성 (아침 브리핑)
    Brief,
    /// 그래프 확장회수 — 벡터히트 → 그래프(edge) 1-hop 이웃
    Graph { query: String },
    /// LLM 추출 — 문서별 problem/solution/tool/concept 노드 + 엣지 생성
    Extract,
    /// 자가증강 루프: ingest → extract 순차 실행 (크론 호출용)
    Sync,
    /// 고아 시맨틱 노드 삭제 (엣지 미참조 legacy 잔재 제거)
    Gc,
    /// 그래프 투영 — Postgres doc↔doc 관계를 wiki relates_to 위키링크로 기입(Obsidian)
    Link {
        /// vault 루트 (기본: $HERMES_VAULT_DIR 또는 $HOME/oh-my-boring/vault)
        #[arg(long)]
        vault: Option<String>,
    },
    /// HTTP 상주 데몬 — ingest/ask/graph/audit API + 백그라운드 스케줄러
    Serve,
    /// 개인 vault lint/audit
    Vault {
        #[command(subcommand)]
        sub: VaultCmd,
    },
}

#[derive(Subcommand)]
enum VaultCmd {
    /// vault/wiki/*.md 정합성 검사 (schema · frontmatter · wikilink · sources)
    Lint {
        /// vault 루트 경로 (기본: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// 경고도 오류로 처리 (exit 2)
        #[arg(long)]
        strict: bool,
    },
    /// vault 그래프 감사 (orphan · 연결성분 · superseded)
    Audit {
        /// vault 루트 경로 (기본: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// 경고도 오류로 처리 (exit 2)
        #[arg(long)]
        strict: bool,
    },
    /// raw/*.md → curated wiki-NNNN.md 컴파일 (LLM 큐레이션, 멱등)
    Compile {
        /// vault 루트 경로 (기본: $PWD/vault)
        #[arg(long)]
        vault: Option<String>,
        /// raw 노트 디렉터리 (기본: <vault>/raw)
        #[arg(long)]
        raw: Option<String>,
        /// 날짜 오버라이드 (YYYY-MM-DD, 기본: 오늘)
        #[arg(long)]
        date: Option<String>,
    },
}

#[tokio::main]
#[allow(clippy::too_many_lines)]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let dsn = std::env::var("PG_DSN")
        .unwrap_or_else(|_| "postgresql://olympus:olympus@localhost:5432/olympus".to_owned());
    let store = store::Store::open(&dsn).await?;

    match cli.cmd {
        Cmd::Selftest => {
            let ol = ollama::Ollama::from_env();
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
                    project: "olympus".to_owned(),
                    source_path: (*id).to_owned(),
                    ..Default::default()
                };
                // chunk.source_path REFERENCES document(source_path) — FK 충족을 위해
                // upsert_document 를 먼저 호출해 부모 레코드를 보장한다.
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
            // GOAL 검증: 'db' 문서가 1위여야 (쿼리와 의미상 최근접)
            match hits.first() {
                Some(h) if h.id == "doc:db" => {
                    println!("✅ 랭킹 정확 (doc:db 1위) — 벡터검색 정상");
                }
                Some(h) => println!("⚠️ 1위가 doc:db 아님: {} — 임베딩/거리 점검 필요", h.id),
                None => println!("❌ 0건 — 벡터검색 여전히 실패"),
            }
        }
        Cmd::Stats => {
            println!("knowledge docs: {}", store.count().await?);
        }
        Cmd::Ingest => {
            let ol = ollama::Ollama::from_env();
            let home = std::env::var("HOME").unwrap_or_default();
            let dirs = std::env::var("HERMES_SOURCE_DIRS").unwrap_or_else(|_| {
                format!(
                    "{home}/.claude/projects:{home}/oh-my-boring/data/notes:{home}/oh-my-boring/vault/wiki"
                )
            });
            let source_dirs: Vec<String> = dirs.split(':').map(str::to_owned).collect();
            println!("sources: {source_dirs:?}");
            let s = ingest::run(&store, &ol, &source_dirs).await?;
            println!(
                "scanned={} new={} updated={} unchanged={} deleted={} skipped={} chunks={}",
                s.scanned, s.new, s.updated, s.unchanged, s.deleted, s.skipped, s.chunks
            );
        }
        Cmd::Audit => {
            audit::run(&store).await?;
        }
        Cmd::Search { query } => {
            let ol = ollama::Ollama::from_env();
            let hits = retrieve::retrieve(&store, &ol, &query, 5, &[]).await?;
            println!("'{query}' → {} hits", hits.len());
            for h in &hits {
                let snip: String = h.content.chars().take(50).collect();
                println!("  [{}/{}] {} — {snip}", h.origin, h.project, h.id);
            }
        }
        Cmd::Ask { question } => {
            let ol = ollama::Ollama::from_env();
            ask::run(&store, &ol, &question, &[]).await?;
        }
        Cmd::Brief => {
            let ol = ollama::Ollama::from_env();
            let out = ask::brief(&store, &ol, &[]).await?;
            println!("{}\n", out.answer);
            if !out.sources.is_empty() {
                println!("출처:");
                for src in &out.sources {
                    println!("  - {src}");
                }
            }
        }
        Cmd::Graph { query } => {
            let ol = ollama::Ollama::from_env();
            graph::run(&store, &ol, &query).await?;
        }
        Cmd::Extract => {
            let ol = ollama::Ollama::from_env();
            let s = extract::run(&store, &ol).await?;
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
            // 시맨틱 통계 감사 출력 (멱등 검증용)
            let ss = store.semantic_stats().await?;
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
            let ol = ollama::Ollama::from_env();
            let home = std::env::var("HOME").unwrap_or_default();
            let dirs = std::env::var("HERMES_SOURCE_DIRS").unwrap_or_else(|_| {
                format!(
                    "{home}/.claude/projects:{home}/oh-my-boring/data/notes:{home}/oh-my-boring/vault/wiki"
                )
            });
            let source_dirs: Vec<String> = dirs.split(':').map(str::to_owned).collect();
            let is = ingest::run(&store, &ol, &source_dirs).await?;
            let es = extract::run(&store, &ol).await?;
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
            let vault_root = vault
                .or_else(|| std::env::var("HERMES_VAULT_DIR").ok())
                .unwrap_or_else(|| {
                    format!(
                        "{}/oh-my-boring/vault",
                        std::env::var("HOME").unwrap_or_default()
                    )
                });
            let n = vault::project_links(&store, std::path::Path::new(&vault_root), 6).await?;
            println!("graph→obsidian: {n} wiki 노트의 relates_to 갱신");
        }
        Cmd::Gc => {
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
            let ol = ollama::Ollama::from_env();
            // store 소유권을 serve::run 으로 이동 — 단일 프로세스 DB 소유자 패턴.
            serve::run(store, ol).await?;
        }
        Cmd::Vault { sub } => {
            // vault 명령은 store(Postgres) 불필요 — drop 해 커넥션 해제.
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
                    let ol = ollama::Ollama::from_env();
                    let vault_root =
                        std::path::PathBuf::from(vault.unwrap_or_else(|| default_vault.clone()));
                    let raw_dir =
                        raw.map_or_else(|| vault_root.join("raw"), std::path::PathBuf::from);
                    // today: --date 인자, 없으면 시스템 날짜(I/O 경계는 vault::today_utc 1곳)
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
