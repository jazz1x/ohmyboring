//! Ingest 파이프 — 소스 walk → frontmatter parse → 청킹 → 임베딩 → upsert(멱등, prune).
//!   - 툴출력 덤프(노이즈) + 격리 토큰(`DRUDGE_COMPANY_SUBSTR`) 경로는 제외.
//!   - sha 추적으로 변경된 파일만 재임베딩. 사라진 파일은 청크 prune.
use anyhow::Result;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::path::Path;
use std::time::SystemTime;
use walkdir::WalkDir;

use crate::frontmatter;
use crate::llm::Llm;
use crate::store::{Doc, Store};

const NOISE_SUBSTR: [&str; 1] = ["tool-results"]; // 일반 노이즈(툴출력 덤프) 제외
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

/// 문자 기준 청킹 (overlap 포함). 짧으면 1청크.
/// NUL(0x00) 제거 — Postgres `text` 는 NUL 저장 불가(스펙). Claude Code 트랜스크립트에
/// 드물게 섞이는 NUL 이 upsert 에서 `invalid byte sequence for encoding "UTF8": 0x00` 로
/// sync 전체를 중단시킨다. IO 경계에서 1회 strip — 무손실(텍스트 의미 보존), 증상무마 아닌
/// parse-don't-validate 입력 정규화(근본원인=소스 NUL, 우리 통제 밖 → 경계가 SSOT). 순수.
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

/// 흡수 대상 = 확장자 OK + 제외 토큰 미포함.
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

    // 제외 토큰 = 일반 노이즈 + (설정 시) 회사 격리 토큰. env 1회 평가.
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

            // IO 경계: 비-UTF8 등은 graceful skip (도메인 fallback 아님).
            let Ok(data) = std::fs::read_to_string(entry.path()) else {
                stats.skipped += 1;
                continue;
            };
            // 같은 IO 경계에서 NUL 정규화 — PG text 가 0x00 저장 불가(아래 sha/parse/embed/upsert 보호).
            let data = strip_nul(&data);
            seen.insert(pstr.clone());

            // 최근성 신호 = 파일 mtime. IO 경계: 못 읽으면 now()(방금 본 것으로 간주, graceful).
            let mtime = entry
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .unwrap_or_else(SystemTime::now);

            let sha = sha256(&data);
            let prev = store.get_doc_sha(&pstr).await?;
            if prev.as_deref() == Some(sha.as_str()) {
                // 내용 동일 — 재임베딩 없이 최근성만 backfill(기존 문서 정렬키 보강).
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

            // 그래프 형태 적재: document 노드 + project/topic 엣지 → chunk 노드 + part_of
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

    // prune: 사라진 파일 → 문서 노드 + 엣지 + 청크 제거
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
        // 가드레일: PG text 는 0x00 저장 불가 → 경계에서 제거되어야 sync 가 안 깨진다.
        assert_eq!(strip_nul("a\u{0}b\u{0}c"), "abc");
        assert_eq!(strip_nul("\u{0}\u{0}"), "");
    }

    #[test]
    fn strip_nul_preserves_clean_text() {
        assert_eq!(strip_nul("정상 텍스트\n탭\t유지"), "정상 텍스트\n탭\t유지");
    }
}
