//! Ingest pipeline — source walk → frontmatter parse → chunking → embedding → upsert (idempotent, prune).
//!   - Excludes tool-output dump (noise) + isolation-token (`DRUDGE_COMPANY_SUBSTR`) paths.
//!   - sha tracking re-embeds only changed files. Chunks of vanished files are pruned.
use anyhow::Result;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::path::Path;
use std::time::SystemTime;
use walkdir::WalkDir;

use crate::frontmatter;
use crate::llm::Llm;
use crate::store::{Doc, Store};

const NOISE_SUBSTR: [&str; 1] = ["tool-results"]; // exclude general noise (tool-output dumps)
const EXTS: [&str; 3] = ["md", "markdown", "txt"];
const CHUNK_SIZE: usize = 1500;
const CHUNK_OVERLAP: usize = 200;

#[derive(Debug, Default)]
pub struct Stats {
    pub scanned: usize,
    pub new: usize,
    pub updated: usize,
    pub unchanged: usize,
    pub deleted: usize,
    pub skipped: usize,
    pub chunks: usize,
}

fn sha256(data: &str) -> String {
    hex::encode(Sha256::digest(data.as_bytes()))
}

/// Character-based chunking (with overlap). One chunk if short.
/// Strip NUL (0x00) — Postgres `text` cannot store NUL (by spec). A NUL that
/// rarely sneaks into a Claude Code transcript would abort the whole sync at
/// upsert with `invalid byte sequence for encoding "UTF8": 0x00`. Strip once at
/// the IO boundary — lossless (preserves text meaning), not symptom-masking but
/// parse-don't-validate input normalization (root cause = source NUL, outside our
/// control → the boundary is the SSOT). Pure.
fn strip_nul(s: &str) -> String {
    if s.contains('\u{0}') {
        s.replace('\u{0}', "")
    } else {
        s.to_owned()
    }
}

fn chunk(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= CHUNK_SIZE {
        return vec![text.to_owned()];
    }
    let step = CHUNK_SIZE - CHUNK_OVERLAP;
    let mut out = Vec::new();
    let mut start = 0;
    while start < chars.len() {
        let end = (start + CHUNK_SIZE).min(chars.len());
        out.push(chars[start..end].iter().collect());
        if end == chars.len() {
            break;
        }
        start += step;
    }
    out
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

pub async fn run(store: &Store, llm: &Llm, source_dirs: &[String]) -> Result<Stats> {
    let mut stats = Stats::default();
    let mut seen: HashSet<String> = HashSet::new();

    // Exclude tokens = general noise + (if configured) company isolation token. env evaluated once.
    let exclude: Vec<String> = NOISE_SUBSTR
        .iter()
        .map(|s| (*s).to_owned())
        .chain(frontmatter::company_substrs())
        .collect();

    for dir in source_dirs {
        for entry in WalkDir::new(dir).into_iter().filter_map(Result::ok) {
            if !entry.file_type().is_file() || !is_target(entry.path(), &exclude) {
                continue;
            }
            let pstr = entry.path().to_string_lossy().into_owned();
            stats.scanned += 1;

            // IO boundary: non-UTF8 etc. are gracefully skipped (not a domain fallback).
            let Ok(data) = std::fs::read_to_string(entry.path()) else {
                stats.skipped += 1;
                continue;
            };
            // Normalize NUL at the same IO boundary — PG text cannot store 0x00 (protects sha/parse/embed/upsert below).
            let data = strip_nul(&data);
            seen.insert(pstr.clone());

            // Recency signal = file mtime. IO boundary: if unreadable, now() (treated as just-seen, graceful).
            let mtime = entry
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .unwrap_or_else(SystemTime::now);

            let sha = sha256(&data);
            let prev = store.get_doc_sha(&pstr).await?;
            if prev.as_deref() == Some(sha.as_str()) {
                // Content identical — backfill only recency without re-embedding (refresh sort key of existing doc).
                store.set_updated_at(&pstr, mtime).await?;
                stats.unchanged += 1;
                continue;
            }

            let (front, body) = frontmatter::parse(&data, &pstr)?;
            let pieces = chunk(body.trim());
            if pieces.iter().all(|p| p.trim().is_empty()) {
                stats.skipped += 1;
                continue;
            }

            // Graph-shaped load: document node + project/topic edges → chunk nodes + part_of
            store.delete_doc_chunks(&pstr).await?;
            store.upsert_document(&front, &sha, mtime).await?;
            for (i, piece) in pieces.iter().enumerate() {
                let embedding = llm.embed(piece).await?;
                let id = format!("{pstr}#{i}");
                store
                    .upsert_chunk(&Doc {
                        id: id.clone(),
                        content: piece.clone(),
                        embedding,
                        front: front.clone(),
                        chunk_idx: i,
                    })
                    .await?;
            }
            if prev.is_some() {
                stats.updated += 1;
            } else {
                stats.new += 1;
            }
            stats.chunks += pieces.len();
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
    use super::strip_nul;

    #[test]
    fn strip_nul_removes_null_bytes() {
        // Guardrail: PG text cannot store 0x00 → it must be stripped at the boundary so sync does not break.
        assert_eq!(strip_nul("a\u{0}b\u{0}c"), "abc");
        assert_eq!(strip_nul("\u{0}\u{0}"), "");
    }

    #[test]
    fn strip_nul_preserves_clean_text() {
        assert_eq!(strip_nul("정상 텍스트\n탭\t유지"), "정상 텍스트\n탭\t유지");
    }
}
