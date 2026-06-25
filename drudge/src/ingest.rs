//! Ingest pipeline — source walk → frontmatter parse → chunking → embedding → upsert (idempotent, prune).
//!
//! Cross-reference: ENFORCEMENT.md §B (one-way flow) · design decision D2 (deterministic graph).
//!   - Excludes tool-output dump (noise) paths.
//!   - sha tracking re-embeds only changed files. Chunks of vanished files are pruned.
//!   - **Graph is deterministic** (kernel A): semantic nodes/edges (tool·concept·claim) come from the
//!     note's frontmatter — agent-curated — NOT from an LLM extraction pass. drudge only embeds (bge-m3)
//!     and links. `ingest_file` is the SSOT per-file pipeline shared by `run` (walk) and `remember` (one file).
use anyhow::Result;
use futures_util::stream::{self, StreamExt, TryStreamExt};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::path::Path;
use std::time::SystemTime;
use walkdir::WalkDir;

use crate::config;
use crate::frontmatter::{self, FrontMatter};
use crate::llm::Llm;
use crate::store::{Doc, Store};

/// Max embedding requests in flight per file. Caps load on a local single-GPU LLM server
/// (Ollama/LM Studio) — going wider stops helping once the server serializes internally, and
/// risks OOM/queueing. Small constant > env knob (Karpathy: simplest thing that works).
const EMBED_CONCURRENCY: usize = 8;
const NOISE_SUBSTR: [&str; 1] = ["tool-results"]; // exclude general noise (tool-output dumps)
const EXTS: [&str; 3] = ["md", "markdown", "txt"];

// ─────────────────────────────────────────────────────────────
// Chunker trait + default character-based chunker
// ─────────────────────────────────────────────────────────────

/// Strategy for splitting a document body into embedding-sized pieces.
pub trait Chunker: Send + Sync + Default {
    fn chunk(&self, text: &str) -> Vec<String>;
}

/// Character-based chunking (with overlap). One chunk if short.
#[derive(Debug, Clone, Copy)]
pub struct DefaultChunker {
    size: usize,
    overlap: usize,
}

impl Default for DefaultChunker {
    /// Default chunker used by the pipeline. `DefaultChunker::new()` is the SSOT for the values.
    fn default() -> Self {
        Self::new()
    }
}

impl DefaultChunker {
    /// Create a chunker with the default size/overlap used by the project.
    #[must_use]
    pub fn new() -> Self {
        Self {
            size: 1500,
            overlap: 200,
        }
    }

    /// Create a chunker with custom size and overlap.
    #[must_use]
    pub fn with_size(size: usize, overlap: usize) -> Self {
        Self { size, overlap }
    }
}

impl Chunker for DefaultChunker {
    fn chunk(&self, text: &str) -> Vec<String> {
        // Defensive guard: a degenerate config (size == 0 or overlap >= size) would make step 0
        // and loop forever. Treat the whole text as one chunk rather than OOM.
        if self.size == 0 || self.overlap >= self.size {
            return vec![text.to_owned()];
        }
        let chars: Vec<char> = text.chars().collect();
        if chars.len() <= self.size {
            return vec![text.to_owned()];
        }
        let step = self.size - self.overlap;
        let mut out = Vec::new();
        let mut start = 0;
        while start < chars.len() {
            let end = (start + self.size).min(chars.len());
            out.push(chars[start..end].iter().collect());
            if end == chars.len() {
                break;
            }
            start += step;
        }
        out
    }
}

// ─────────────────────────────────────────────────────────────
// GraphExtractor trait + default frontmatter extractor
// ─────────────────────────────────────────────────────────────

/// Strategy for extracting the semantic graph (tools/concepts/claims) from a parsed note.
pub trait GraphExtractor: Send + Sync + Default {
    fn extract<'a>(
        &'a self,
        store: &'a Store,
        llm: &'a Llm,
        front: &'a FrontMatter,
        body: &'a str,
        stats: &'a mut Stats,
    ) -> impl std::future::Future<Output = Result<()>> + Send + 'a;
}

/// Deterministic semantic graph from the agent-curated frontmatter.
///
/// tool/concept nodes + edges, and temporal-fact claims. Idempotent: clears the doc's prior semantic
/// edges first. Embedding (bge-m3) is the ONLY LLM call — no generation.
#[derive(Debug, Clone, Copy)]
pub struct FrontmatterGraphExtractor {
    /// Cap on graph items per note (matches the agent-curation guidance: short canonical lists, no hairball).
    cap: usize,
}

impl Default for FrontmatterGraphExtractor {
    /// Default extractor used by the pipeline. `FrontmatterGraphExtractor::new()` is the SSOT.
    fn default() -> Self {
        Self::new()
    }
}

impl FrontmatterGraphExtractor {
    /// Create the extractor with the default project cap.
    #[must_use]
    pub fn new() -> Self {
        Self { cap: 6 }
    }

    /// Create the extractor with a custom item cap.
    #[must_use]
    pub fn with_cap(cap: usize) -> Self {
        Self { cap }
    }
}

impl GraphExtractor for FrontmatterGraphExtractor {
    async fn extract(
        &self,
        store: &Store,
        llm: &Llm,
        front: &FrontMatter,
        _body: &str,
        stats: &mut Stats,
    ) -> Result<()> {
        let path = &front.source_path;
        store.clear_semantic_edges(path).await?;

        // tool nodes + uses edges (Han drift filter + slug dedup + cap)
        let mut seen_tool = HashSet::new();
        for t in front.tools.iter().take(self.cap) {
            let t = t.trim();
            if t.is_empty() || has_han(t) {
                continue;
            }
            let slug = slugify(t);
            if slug.is_empty() || !seen_tool.insert(slug.clone()) {
                continue;
            }
            store.upsert_tool(&slug, t).await?;
            store.relate_doc_tool(path, &slug).await?;
            stats.tools += 1;
            stats.edges += 1;
        }

        // concept nodes + about edges
        let mut seen_concept = HashSet::new();
        for c in front.concepts.iter().take(self.cap) {
            let c = c.trim();
            if c.is_empty() || has_han(c) {
                continue;
            }
            let slug = slugify(c);
            if slug.is_empty() || !seen_concept.insert(slug.clone()) {
                continue;
            }
            store.upsert_concept(&slug, c).await?;
            store.relate_doc_concept(path, &slug).await?;
            stats.concepts += 1;
            stats.edges += 1;
        }

        // temporal-fact claims — (subject,predicate)→value, a new value supersedes the old.
        // valid_from = document mtime (chronological ordering). value embedding via bge-m3 (no generation).
        if !front.claims.is_empty() {
            let valid_from = store.doc_updated_at(path).await?;
            for cl in &front.claims {
                let subject = canon(&cl.subject);
                let predicate = canon(&cl.predicate);
                let value = cl.value.trim();
                if subject.is_empty()
                    || predicate.is_empty()
                    || value.is_empty()
                    || has_han(&subject)
                    || has_han(value)
                {
                    continue;
                }
                let emb = llm.embed(&format!("{subject} {predicate} {value}")).await?;
                store
                    .upsert_claim(&subject, &predicate, value, path, valid_from, &emb)
                    .await?;
                stats.claims += 1;
            }
        }
        Ok(())
    }
}

#[derive(Debug, Default)]
pub struct Stats {
    pub scanned: usize,
    pub new: usize,
    pub updated: usize,
    pub unchanged: usize,
    pub deleted: usize,
    pub skipped: usize,
    pub failed: usize, // notes that errored on parse/ingest and were skipped (resilient sync, not aborted)
    pub repaired: usize, // notes auto-repaired (unsafe frontmatter re-quoted) then re-ingested
    pub chunks: usize,
    // deterministic graph (from frontmatter)
    pub tools: usize,
    pub concepts: usize,
    pub claims: usize,
    pub edges: usize,
}

/// Per-file ingest verdict — what `ingest_file` did with one note.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileOutcome {
    New,
    Updated,
    Unchanged,
    Skipped,
}

fn sha256(data: &str) -> String {
    hex::encode(Sha256::digest(data.as_bytes()))
}

/// Detect whether the string contains CJK Unified Ideographs (U+4E00..=U+9FFF) — filters model drift
/// into Chinese in agent-curated tags/tools/concepts. Korean uses Hangul, not Han ideographs.
fn has_han(s: &str) -> bool {
    s.chars().any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c))
}

/// Slug: lowercase + keep only `[a-z0-9]` (remove all separators) → prevents variant-spelling collisions.
/// Alias map: `c++`→`cpp`, `c#`→`csharp`, `.net`→`dotnet` (left as-is they would collapse and collide).
fn slugify(s: &str) -> String {
    let lower = s.to_lowercase();
    let normalized = lower
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace(".net", "dotnet");
    normalized
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

/// claim subject/predicate normalization — lowercase, trim, collapse whitespace (matching consistency).
fn canon(s: &str) -> String {
    s.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Strip NUL (0x00) — Postgres `text` cannot store NUL. Strip once at the IO boundary (lossless,
/// parse-don't-validate input normalization; root cause = source NUL, outside our control). Pure.
fn strip_nul(s: &str) -> String {
    if s.contains('\u{0}') {
        s.replace('\u{0}', "")
    } else {
        s.to_owned()
    }
}

/// Ingest target = extension OK + does not contain an exclude token.
fn is_target(path: &Path, exclude: &[String]) -> bool {
    let ext_ok = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| EXTS.contains(&e.to_lowercase().as_str()));
    let pstr = path.to_string_lossy();
    ext_ok && !exclude.iter().any(|s| pstr.contains(s))
}

/// Ingest one note file into the vector store + deterministic graph. The SSOT per-file pipeline.
/// Reads `path` from disk, parses frontmatter, chunks, embeds, upserts, and (on new/changed content)
/// rebuilds the semantic graph from the frontmatter. Accumulates into `stats`. Returns the verdict.
///
/// Uses the default chunker and frontmatter graph extractor. For custom strategies see
/// [`ingest_file_with`].
pub async fn ingest_file(
    store: &Store,
    llm: &Llm,
    cfg: &config::BoringConfig,
    path: &str,
    stats: &mut Stats,
) -> Result<FileOutcome> {
    ingest_file_with::<DefaultChunker, FrontmatterGraphExtractor>(store, llm, cfg, path, stats)
        .await
}

/// Same as [`ingest_file`], but with injectable `Chunker` and `GraphExtractor` strategies.
pub async fn ingest_file_with<C: Chunker, G: GraphExtractor>(
    store: &Store,
    llm: &Llm,
    cfg: &config::BoringConfig,
    path: &str,
    stats: &mut Stats,
) -> Result<FileOutcome> {
    // IO boundary: non-UTF8 etc. are gracefully skipped (not a domain fallback).
    let Ok(data) = std::fs::read_to_string(path) else {
        stats.skipped += 1;
        return Ok(FileOutcome::Skipped);
    };
    let data = strip_nul(&data);

    // Recency signal = file mtime. IO boundary: if unreadable, now() (treated as just-seen, graceful).
    let mtime = std::fs::metadata(path)
        .ok()
        .and_then(|m| m.modified().ok())
        .unwrap_or_else(SystemTime::now);

    let sha = sha256(&data);
    let prev = store.get_doc_sha(path).await?;
    if prev.as_deref() == Some(sha.as_str()) {
        // Content identical — backfill only recency without re-embedding (graph already built).
        store.set_updated_at(path, mtime).await?;
        stats.unchanged += 1;
        return Ok(FileOutcome::Unchanged);
    }

    let (front, body) = frontmatter::parse(&data, path, cfg)?;
    let pieces = C::default().chunk(body.trim());
    if pieces.iter().all(|p| p.trim().is_empty()) {
        stats.skipped += 1;
        return Ok(FileOutcome::Skipped);
    }

    // Pre-compute embeddings BEFORE any DB write — keep the slow LLM round-trips out of the
    // update window so the upsert/prune below is a tight back-to-back sequence. Chunks embed
    // independently, so they run with bounded concurrency (`buffered` preserves order, keeping
    // chunk_idx aligned) instead of one blocking await per chunk. First error short-circuits (ROP).
    let embedded: Vec<(String, Vec<f32>)> = stream::iter(pieces.clone())
        .map(|piece| async move {
            let embedding = llm.embed(&piece).await?;
            Ok::<_, anyhow::Error>((piece, embedding))
        })
        .buffered(EMBED_CONCURRENCY)
        .try_collect()
        .await?;

    // Upsert-then-prune (NOT delete-then-insert): chunks are keyed by `path#idx` with ON CONFLICT
    // DO UPDATE, so overwriting them in place keeps the document with a full chunk set throughout —
    // a concurrent ask/recall never sees an empty/half-deleted window during a re-ingest. The stale
    // tail (when the new version has fewer chunks) is pruned only AFTER the new chunks are in place.
    store.upsert_document(&front, &sha, mtime).await?;
    for (i, (content, embedding)) in embedded.into_iter().enumerate() {
        store
            .upsert_chunk(&Doc {
                id: format!("{path}#{i}"),
                content,
                embedding,
                front: front.clone(),
                chunk_idx: i,
            })
            .await?;
    }
    store.prune_chunks_from(path, pieces.len()).await?;
    stats.chunks += pieces.len();

    // deterministic semantic graph from frontmatter (no LLM extraction).
    G::default()
        .extract(store, llm, &front, &body, stats)
        .await?;

    if prev.is_some() {
        stats.updated += 1;
        Ok(FileOutcome::Updated)
    } else {
        stats.new += 1;
        Ok(FileOutcome::New)
    }
}

/// Walk `dirs`, ingesting every target note. Kernel A: the scheduler/sync feeds `<vault>/wiki` (the
/// agent-written corpus) — drudge does NOT bulk-ingest raw transcripts (that is the agent's job).
/// `dirs` is explicit (not read from cfg) so the caller owns the source-of-truth boundary.
///
/// Uses the default chunker and frontmatter graph extractor. For custom strategies see [`run_with`].
pub async fn run(
    store: &Store,
    llm: &Llm,
    cfg: &config::BoringConfig,
    dirs: &[String],
) -> Result<Stats> {
    run_with::<DefaultChunker, FrontmatterGraphExtractor>(store, llm, cfg, dirs).await
}

/// Same as [`run`], but with injectable `Chunker` and `GraphExtractor` strategies.
pub async fn run_with<C: Chunker, G: GraphExtractor>(
    store: &Store,
    llm: &Llm,
    cfg: &config::BoringConfig,
    dirs: &[String],
) -> Result<Stats> {
    let mut stats = Stats::default();
    let mut seen: HashSet<String> = HashSet::new();

    let exclude: Vec<String> = NOISE_SUBSTR.iter().map(|s| (*s).to_owned()).collect();

    for dir in dirs {
        for entry in WalkDir::new(dir).into_iter().filter_map(Result::ok) {
            if !entry.file_type().is_file() || !is_target(entry.path(), &exclude) {
                continue;
            }
            let pstr = entry.path().to_string_lossy().into_owned();
            stats.scanned += 1;
            seen.insert(pstr.clone());
            // Resilient-by-default: a malformed note must NEVER abort the whole re-ingest. First try an
            // autonomous repair (quote unsafe scalar frontmatter, e.g. an unquoted `title: [FEDEV-97] …`
            // that YAML reads as a sequence) and re-ingest; only if THAT still fails do we skip + log.
            // Either way the rest of the corpus keeps syncing.
            if let Err(e) = ingest_file_with::<C, G>(store, llm, cfg, &pstr, &mut stats).await {
                if let Some(fixed) = std::fs::read_to_string(&pstr)
                    .ok()
                    .and_then(|c| crate::vault::repair_note_frontmatter(&c))
                    && frontmatter::parse(&fixed, &pstr, cfg).is_ok()
                    && std::fs::write(&pstr, &fixed).is_ok()
                    && ingest_file_with::<C, G>(store, llm, cfg, &pstr, &mut stats)
                        .await
                        .is_ok()
                {
                    eprintln!("[ingest] auto-repaired malformed frontmatter + re-ingested {pstr}");
                    stats.repaired += 1;
                } else {
                    eprintln!("[ingest] skipped malformed note {pstr}: {e:#}");
                    stats.failed += 1;
                }
            }
        }
    }

    // prune: vanished files → remove document node + edges + chunks
    for p in store.all_doc_paths().await? {
        if !seen.contains(&p) {
            store.delete_document(&p).await?;
            stats.deleted += 1;
        }
    }
    Ok(stats)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{canon, has_han, slugify, strip_nul};

    #[test]
    fn strip_nul_removes_null_bytes() {
        assert_eq!(strip_nul("a\u{0}b\u{0}c"), "abc");
        assert_eq!(strip_nul("\u{0}\u{0}"), "");
    }

    #[test]
    fn strip_nul_preserves_clean_text() {
        assert_eq!(strip_nul("정상 텍스트\n탭\t유지"), "정상 텍스트\n탭\t유지");
    }

    #[test]
    fn slugify_collapses_separators() {
        assert_eq!(slugify("macos keychain"), "macoskeychain");
        assert_eq!(slugify("macos_keychain"), "macoskeychain");
        assert_eq!(slugify("c++"), "cpp");
    }

    #[test]
    fn has_han_detects_cjk() {
        assert!(has_han("漢字"));
        assert!(!has_han("rust 한글"));
    }

    #[test]
    fn canon_normalizes() {
        assert_eq!(canon("  OH-my  Boring  DB "), "oh-my boring db");
    }
}
