//! wiki_recall — retrieve by reading `vault/wiki/*.md` directly (no pgvector·embeddings needed).
//!
//! Cross-reference: design decision D1 (wiki-first, pgvector optional).
//!
//! Karpathy-wiki first-class path: for a personal, small corpus (hundreds of documents), reading markdown directly is simpler, more
//! trustworthy, and easier to debug than RAG (2026 trend + repo `CLAUDE.md` "simplest thing that works"). pgvector (vector+graph) is
//! an optional accelerator turned on when the scale/accuracy trigger is crossed.
//!
//! Scoring: not token *equality* but **substring-match frequency**. Tolerates Korean attached endings (임베딩→"임베딩은") and English
//! partial words (decent without morphological analysis). Title matches are weighted. Separates pure logic (`score_doc`) from I/O (`recall`) (SRP).
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

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

/// title+body score + snippet. None if zero matches. Lowercases then delegates to `score_lower`.
/// Test-only convenience now — the production path caches lowercased forms and calls `score_lower`.
/// score = Σ(body occurrences) + 3·Σ(title occurrences) + coverage (count of distinct matched terms).
#[cfg(test)]
fn score_doc(title: &str, body: &str, terms: &[String]) -> Option<(f32, String)> {
    score_lower(&title.to_lowercase(), &body.to_lowercase(), terms)
}

/// Same scoring on already-lowercased title/body — the `WikiIndex` caches the lowercased forms so the
/// hot recall path skips re-lowercasing every document on every query. Pure.
fn score_lower(tl: &str, bl: &str, terms: &[String]) -> Option<(f32, String)> {
    let mut score = 0_usize;
    let mut coverage = 0_usize;
    let mut first_hit: Option<usize> = None;
    for t in terms {
        let bc = count_occurrences(bl, t);
        let tc = count_occurrences(tl, t);
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
        snippet_around(bl, first_hit.unwrap_or(0)),
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

/// Extract `project:` from the YAML frontmatter, if present. Pure.
fn extract_project(content: &str) -> String {
    content
        .strip_prefix("---\n")
        .and_then(|rest| rest.find("\n---\n").map(|e| &rest[..e]))
        .and_then(|yaml| {
            yaml.lines()
                .find(|l| l.trim_start().starts_with("project:"))
                .and_then(|l| l.split_once(':'))
                .map(|(_, v)| v.trim().trim_matches('"').to_owned())
        })
        .unwrap_or_default()
}

/// One parsed wiki note, cached in lowercased form for scoring. `mtime` keys incremental refresh.
struct CachedDoc {
    id: String,
    title: String, // raw (for display); lowercased forms below are what scoring reads
    source_path: String,
    project: String,
    title_lower: String,
    body_lower: String,
    mtime: SystemTime,
}

/// In-memory wiki index — parses `vault/wiki/*.md` once and keeps the lowercased title/body cached so
/// the per-query recall path scores in memory instead of re-reading + re-lowercasing every file. The
/// index is **honest, not stale**: `refresh()` re-stats the dir each call and re-reads only files whose
/// mtime changed (catching out-of-band edits, e.g. Obsidian), and drops vanished files. Reading bodies
/// — the expensive part — happens only on first sight or change. (Layer 1 honesty kept; Layer 3 cost cut.)
#[derive(Default)]
pub struct WikiIndex {
    docs: HashMap<PathBuf, CachedDoc>,
}

impl WikiIndex {
    /// Reconcile the cache with `vault/wiki/`: re-read changed/new `.md` (by mtime), drop removed ones.
    /// Missing wiki dir is graceful (cache cleared → empty recall). I/O shell; scoring stays pure.
    pub fn refresh(&mut self, wiki_dir: &Path) -> Result<()> {
        let Ok(read_dir) = std::fs::read_dir(wiki_dir) else {
            self.docs.clear(); // wiki doesn't exist yet — normal (empty recall), not a stale lie
            return Ok(());
        };
        let mut seen: HashSet<PathBuf> = HashSet::new();
        for entry in read_dir {
            let path = entry.context("reading wiki dir entry")?.path();
            if path.extension().and_then(|e| e.to_str()) != Some("md") {
                continue;
            }
            seen.insert(path.clone());
            let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
            // Up-to-date cache entry → skip the expensive read+parse.
            if let Some(mt) = mtime
                && self.docs.get(&path).is_some_and(|c| c.mtime == mt)
            {
                continue;
            }
            let Ok(content) = std::fs::read_to_string(&path) else {
                continue; // skip on read failure (graceful)
            };
            let stem = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_owned();
            let (title, body) = extract_title_body(&content, &stem);
            let project = extract_project(&content);
            self.docs.insert(
                path.clone(),
                CachedDoc {
                    id: stem,
                    title_lower: title.to_lowercase(),
                    body_lower: body.to_lowercase(),
                    title,
                    source_path: path.to_string_lossy().into_owned(),
                    project,
                    mtime: mtime.unwrap_or_else(SystemTime::now),
                },
            );
        }
        self.docs.retain(|p, _| seen.contains(p)); // drop vanished notes
        Ok(())
    }

    /// Top-K notes closest to `query`, scored over the cached (lowercased) corpus. Pure — no I/O.
    /// Optional `project`/`since_hours` filters are applied before scoring.
    #[must_use]
    pub fn search(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Vec<WikiHit> {
        let terms = query_terms(query);
        if terms.is_empty() {
            return Vec::new();
        }
        let cutoff = since_hours.map(|h| {
            let hours = i64::from(h).max(0);
            let secs = u64::try_from(hours.saturating_mul(3600)).unwrap_or(0);
            SystemTime::now() - std::time::Duration::from_secs(secs)
        });
        let mut hits: Vec<WikiHit> = self
            .docs
            .values()
            .filter(|d| {
                if let Some(p) = project {
                    return d.project == p;
                }
                if let Some(cutoff) = cutoff {
                    return d.mtime >= cutoff;
                }
                true
            })
            .filter_map(|d| {
                score_lower(&d.title_lower, &d.body_lower, &terms).map(|(score, snippet)| WikiHit {
                    id: d.id.clone(),
                    title: d.title.clone(),
                    source_path: d.source_path.clone(),
                    snippet,
                    score,
                })
            })
            .collect();
        hits.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        hits.truncate(k);
        hits
    }
}

/// One-shot recall (CLI / non-cached callers): build a fresh index, refresh, search. The resident
/// daemon instead holds a persistent `WikiIndex` (in `AppState`) so repeated `/search` skips re-reads.
/// Missing wiki directory · read failure are graceful: empty result/skip (recall never panics).
pub fn recall(
    wiki_dir: &Path,
    query: &str,
    k: usize,
    project: Option<&str>,
    since_hours: Option<i32>,
) -> Result<Vec<WikiHit>> {
    let mut index = WikiIndex::default();
    index.refresh(wiki_dir)?;
    Ok(index.search(query, k, project, since_hours))
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::float_cmp
    )]
    use super::{WikiIndex, count_occurrences, extract_title_body, query_terms, score_doc};

    #[test]
    fn wiki_index_refresh_is_incremental_and_honest() {
        use std::time::{Duration, SystemTime};
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: docker cache\n---\nlayer caching tips",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: pg pool\n---\ntoo many clients fix",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        // search hits the right note by content
        let hits = idx.search("docker layer", 5, None, None);
        assert_eq!(hits.first().map(|h| h.id.as_str()), Some("wiki-0001"));
        assert!(
            idx.search("clients", 5, None, None)
                .iter()
                .any(|h| h.id == "wiki-0002")
        );

        // OUT-OF-BAND edit: rewrite 0001's body + push mtime forward → refresh must pick it up (honest, not stale).
        let future = SystemTime::now() + Duration::from_secs(5);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: docker cache\n---\nkubernetes oomkilled memory",
        )
        .unwrap();
        filetime_set(&p("wiki-0001.md"), future);
        idx.refresh(dir.path()).unwrap();
        assert!(
            idx.search("kubernetes oomkilled", 5, None, None)
                .iter()
                .any(|h| h.id == "wiki-0001")
        );
        assert!(
            idx.search("layer caching", 5, None, None).is_empty(),
            "stale body must be gone"
        );

        // VANISHED file drops out of the index.
        std::fs::remove_file(p("wiki-0002.md")).unwrap();
        idx.refresh(dir.path()).unwrap();
        assert!(
            idx.search("clients", 5, None, None).is_empty(),
            "removed note must not be recalled"
        );
    }

    // Set mtime without an extra dep: write via std then bump using a second write is unreliable, so
    // we just touch through OpenOptions + set_modified (stable since 1.75).
    fn filetime_set(path: &std::path::Path, t: std::time::SystemTime) {
        let f = std::fs::OpenOptions::new().write(true).open(path).unwrap();
        f.set_modified(t).unwrap();
    }

    #[test]
    fn search_filters_by_project() {
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: docker cache\nproject: omb\n---\nlayer caching tips",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: pg pool\nproject: kb-rag-bot\n---\ntoo many clients fix",
        )
        .unwrap();

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("tips", 5, Some("omb"), None);
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
        assert!(idx.search("tips", 5, Some("kb-rag-bot"), None).is_empty());
    }

    #[test]
    fn search_filters_by_since_hours() {
        use std::time::{Duration, SystemTime};
        let dir = tempfile::tempdir().unwrap();
        let p = |name: &str| dir.path().join(name);
        std::fs::write(
            p("wiki-0001.md"),
            "---\ntitle: recent\nproject: omb\n---\nrecent content",
        )
        .unwrap();
        std::fs::write(
            p("wiki-0002.md"),
            "---\ntitle: old\nproject: omb\n---\nold content",
        )
        .unwrap();

        // Make the second file 2 days old so a 24-hour window excludes it.
        let old = SystemTime::now() - Duration::from_hours(48);
        filetime_set(&p("wiki-0002.md"), old);

        let mut idx = WikiIndex::default();
        idx.refresh(dir.path()).unwrap();
        let hits = idx.search("content", 5, None, Some(24));
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "wiki-0001");
    }

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
