//! wiki_recall — retrieve by reading `vault/wiki/*.md` directly (no pgvector·embeddings needed).
//!
//! Karpathy-wiki first-class path: for a personal, small corpus (hundreds of documents), reading markdown directly is simpler, more
//! trustworthy, and easier to debug than RAG (2026 trend + repo `CLAUDE.md` "simplest thing that works"). pgvector (vector+graph) is
//! an optional accelerator turned on when the scale/accuracy trigger is crossed.
//!
//! Scoring: not token *equality* but **substring-match frequency**. Tolerates Korean attached endings (임베딩→"임베딩은") and English
//! partial words (decent without morphological analysis). Title matches are weighted. Separates pure logic (`score_doc`) from I/O (`recall`) (SRP).
use std::path::Path;

use anyhow::{Context, Result};

/// A single retrieval result (minimal fields compatible with the vector path's hit).
#[derive(Debug, Clone)]
pub struct WikiHit {
    pub id: String,
    pub title: String,
    pub source_path: String,
    pub snippet: String,
    pub score: f32,
}

/// Split the query into search terms — whitespace split + 2+ chars + lowercase. Pure.
fn query_terms(query: &str) -> Vec<String> {
    query
        .split_whitespace()
        .map(|w| {
            w.trim_matches(|c: char| !c.is_alphanumeric())
                .to_lowercase()
        })
        .filter(|w| w.chars().count() >= 2)
        .collect()
}

/// Non-overlapping occurrence count of `needle` within `haystack`. Pure.
fn count_occurrences(haystack: &str, needle: &str) -> usize {
    if needle.is_empty() {
        return 0;
    }
    let mut n = 0;
    let mut rest = haystack;
    while let Some(pos) = rest.find(needle) {
        n += 1;
        rest = &rest[pos + needle.len()..];
    }
    n
}

/// title+body score + snippet. None if zero matches. Pure.
/// score = Σ(body occurrences) + 3·Σ(title occurrences) + coverage (count of distinct matched terms).
fn score_doc(title: &str, body: &str, terms: &[String]) -> Option<(f32, String)> {
    let tl = title.to_lowercase();
    let bl = body.to_lowercase();
    let mut score = 0_usize;
    let mut coverage = 0_usize;
    let mut first_hit: Option<usize> = None;
    for t in terms {
        let bc = count_occurrences(&bl, t);
        let tc = count_occurrences(&tl, t);
        if bc + tc > 0 {
            coverage += 1;
        }
        score += bc + 3 * tc;
        if first_hit.is_none()
            && let Some(p) = bl.find(t)
        {
            first_hit = Some(p);
        }
    }
    if score == 0 {
        return None;
    }
    score += coverage; // bonus for documents that match a broader set of search terms
    // Snippet is taken from the same lowercased body `bl` that `first_hit` indexes into,
    // so the byte offset and the slicing share one coordinate system (no case-fold drift).
    Some((
        precise_cast(score),
        snippet_around(&bl, first_hit.unwrap_or(0)),
    ))
}

/// usize → f32 (lossless range at score magnitudes). Helper to avoid the clippy cast gate.
#[allow(clippy::cast_precision_loss)]
fn precise_cast(n: usize) -> f32 {
    n as f32
}

/// ~200-char snippet around `pos`, a byte offset INTO `text`. Caller must pass the same
/// string `pos` was found in (we pass the lowercased body) so byte→char conversion is exact.
/// Char-boundary safe. Pure.
fn snippet_around(text: &str, pos: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    // pos indexes `text` itself → char count of the prefix is exact (no cross-string drift).
    let char_pos = text.get(..pos).map_or(0, |s| s.chars().count());
    let start = char_pos.saturating_sub(40);
    let end = (start + 200).min(chars.len());
    let s: String = chars[start..end].iter().collect();
    let s = s.replace('\n', " ");
    if start > 0 {
        format!("…{}", s.trim())
    } else {
        s.trim().to_owned()
    }
}

/// Split `--- yaml ---\nbody` + extract the title from frontmatter. If absent, the first `# ` heading, and failing that the stem. Pure.
fn extract_title_body<'a>(content: &'a str, stem: &str) -> (String, &'a str) {
    let (yaml, body) = content
        .strip_prefix("---\n")
        .and_then(|rest| rest.find("\n---\n").map(|e| (&rest[..e], &rest[e + 5..])))
        .unwrap_or(("", content));
    if let Some(line) = yaml.lines().find(|l| l.trim_start().starts_with("title:")) {
        let t = line
            .split_once(':')
            .map_or("", |(_, v)| v)
            .trim()
            .trim_matches('"');
        if !t.is_empty() {
            return (t.to_owned(), body);
        }
    }
    if let Some(h) = body.lines().find(|l| l.starts_with("# ")) {
        return (h[2..].trim().to_owned(), body);
    }
    (stem.to_owned(), body)
}

/// Read `vault/wiki/*.md` directly for the top-K closest to query (I/O shell — delegates to pure `score_doc`).
/// Missing wiki directory · read failure are graceful: empty result/skip (recall never panics).
pub fn recall(wiki_dir: &Path, query: &str, k: usize) -> Result<Vec<WikiHit>> {
    let terms = query_terms(query);
    if terms.is_empty() {
        return Ok(Vec::new());
    }
    let mut hits: Vec<WikiHit> = Vec::new();
    let Ok(read_dir) = std::fs::read_dir(wiki_dir) else {
        return Ok(Vec::new()); // wiki doesn't exist yet — normal (empty recall)
    };
    for entry in read_dir {
        let path = entry.context("wiki 디렉터리 항목 읽기")?.path();
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();
        let Ok(content) = std::fs::read_to_string(&path) else {
            continue; // skip on read failure (graceful)
        };
        let (title, body) = extract_title_body(&content, &stem);
        if let Some((score, snippet)) = score_doc(&title, body, &terms) {
            hits.push(WikiHit {
                id: stem,
                title,
                source_path: path.to_string_lossy().into_owned(),
                snippet,
                score,
            });
        }
    }
    hits.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    hits.truncate(k);
    Ok(hits)
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::float_cmp
    )]
    use super::{count_occurrences, extract_title_body, query_terms, score_doc};

    #[test]
    fn query_terms_splits_and_filters() {
        assert_eq!(query_terms("bge-m3 임베딩 a"), vec!["bge-m3", "임베딩"]); // 1-char 'a' excluded
    }

    #[test]
    fn count_occurrences_non_overlapping() {
        assert_eq!(count_occurrences("aaaa", "aa"), 2);
        assert_eq!(count_occurrences("임베딩은 임베딩", "임베딩"), 2);
        assert_eq!(count_occurrences("none", "x"), 0);
    }

    #[test]
    fn score_doc_substring_handles_korean_josa() {
        // the term "임베딩" must catch the body "임베딩은" (with attached particle) via substring match
        let terms = query_terms("임베딩 차원");
        let (score, snip) = score_doc("벡터 노트", "bge-m3 임베딩은 1024차원이다", &terms)
            .expect("부분일치로 점수 나야 함");
        assert!(score > 0.0);
        assert!(snip.contains("임베딩"));
    }

    #[test]
    fn score_doc_title_weighted_and_zero_is_none() {
        let terms = query_terms("docker");
        let in_title = score_doc("docker 캐시", "본문 무관", &terms).unwrap().0;
        let in_body = score_doc("무관", "docker 한 번", &terms).unwrap().0;
        assert!(in_title > in_body, "title 일치가 더 높아야");
        assert!(score_doc("무관", "전혀 다른 내용", &terms).is_none());
    }

    #[test]
    fn extract_title_from_frontmatter_then_heading_then_stem() {
        let fm = "---\nid: wiki-0001\ntitle: 제목A\n---\n본문";
        assert_eq!(extract_title_body(fm, "wiki-0001").0, "제목A");
        let h = "# 헤딩B\n본문";
        assert_eq!(extract_title_body(h, "wiki-0002").0, "헤딩B");
        assert_eq!(
            extract_title_body("프런트매터 없음", "wiki-0003").0,
            "wiki-0003"
        );
    }
}
