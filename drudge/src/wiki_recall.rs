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
    /// Title equals the query after case-fold and whitespace collapse (Python `wiki.py` `exact_title`).
    pub exact_title: bool,
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
    let (yaml, body) = crate::vault::split_frontmatter(content).unwrap_or(("", content));
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
    crate::vault::split_frontmatter(content)
        .map(|(yaml, _)| yaml)
        .and_then(|yaml| {
            yaml.lines()
                .find(|l| l.trim_start().starts_with("project:"))
                .and_then(|l| l.split_once(':'))
                .map(|(_, v)| v.trim().trim_matches('"').to_owned())
        })
        .unwrap_or_default()
}

/// One parsed wiki note, cached in lowercased form for scoring. `mtime` keys incremental refresh.
#[derive(Debug)]
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
#[derive(Debug, Default)]
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
        let mut hits = self.search_ranked(query, project, since_hours);
        hits.truncate(k);
        hits
    }

    /// Every matching note in Python `wiki.py` `search()` order — callers cut, this does not.
    #[must_use]
    pub fn search_ranked(
        &self,
        query: &str,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Vec<WikiHit> {
        let asked = normalized(query);
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
                    exact_title: normalized(&d.title) == asked,
                })
            })
            .collect();
        // Python key: exact titles first; then score desc; then newer `wiki-N` first; then path.
        hits.sort_by(|a, b| {
            b.exact_title
                .cmp(&a.exact_title)
                .then_with(|| {
                    if a.exact_title {
                        std::cmp::Ordering::Equal
                    } else {
                        b.score.total_cmp(&a.score)
                    }
                })
                .then_with(|| creation_order(&b.id).cmp(&creation_order(&a.id)))
                .then_with(|| a.source_path.cmp(&b.source_path))
        });
        hits
    }
}

/// `wiki-N` -> N (engine and Python both number notes max+1); unnumbered names are -1 (last).
fn creation_order(id: &str) -> i64 {
    id.strip_prefix("wiki-")
        .unwrap_or(id)
        .parse::<u32>()
        .map_or(-1, i64::from)
}

/// Python `answer._wiki_selected`: from the full ranked list take `size = min(k, len)`, the pool is
/// every exact-title note plus every note scoring at least the size-th hit (ties at the cut stay),
/// and the first `size` pool members go on to ordering with exact-title notes carrying
/// `max(pool score) + 1`. Pure.
#[must_use]
pub fn select_for_order(hits: &[WikiHit], k: usize) -> Vec<WikiHit> {
    let pool = pool(hits, k);
    let lifted = pool.iter().map(|h| h.score).fold(f32::MIN, f32::max) + 1.0;
    pool.into_iter()
        .take(k.min(hits.len()))
        .map(|h| {
            let mut h = h.clone();
            if h.exact_title {
                h.score = lifted;
            }
            h
        })
        .collect()
}

/// Python `_Selected.titles`: (path -> title) of the whole pool `select_for_order` cuts from.
#[must_use]
pub fn pool_titles(hits: &[WikiHit], k: usize) -> HashMap<String, String> {
    pool(hits, k)
        .into_iter()
        .map(|h| (h.source_path.clone(), h.title.clone()))
        .collect()
}

/// Python `pool_hits`: every exact-title note plus every note scoring at least the
/// `min(k, len)`-th hit (ties at the cut stay).
fn pool(hits: &[WikiHit], k: usize) -> Vec<&WikiHit> {
    let Some(threshold) = k.min(hits.len()).checked_sub(1).map(|i| hits[i].score) else {
        return Vec::new();
    };
    hits.iter()
        .filter(|h| h.exact_title || h.score >= threshold)
        .collect()
}

/// Python `answer._fused`: wiki and vector lines merged by RRF (k=60). Leading exact-title wiki
/// lines stay pinned; the rest sort by summed RRF, ties by wiki rank then vector rank; a note in
/// both lists shows its wiki body, a repeated vector path counts at its first rank. Membership is
/// decided by RRF first; then `sunk` (superseded) notes move behind the rest inside the cut, never
/// out of it, and pinned lines stay put. Pure.
pub fn fuse(
    wiki: &[WikiHit],
    vector: &[(String, String)],
    k: usize,
    sunk: &HashSet<String>,
) -> Result<Vec<(String, String)>> {
    let pinned: Vec<&WikiHit> = wiki.iter().take_while(|h| h.exact_title).collect();
    let pinned_paths: HashSet<&str> = pinned.iter().map(|h| h.source_path.as_str()).collect();
    let mut wiki_at: HashMap<&str, usize> = HashMap::new();
    let mut body: HashMap<&str, &str> = HashMap::new();
    for (i, h) in wiki.iter().enumerate() {
        wiki_at.insert(h.source_path.as_str(), i);
    }
    let mut vector_at: HashMap<&str, usize> = HashMap::new();
    for (i, (path, text)) in vector.iter().enumerate() {
        vector_at.entry(path.as_str()).or_insert(i);
        body.entry(path.as_str()).or_insert(text.as_str());
    }
    for h in wiki {
        body.insert(h.source_path.as_str(), h.snippet.as_str());
    }
    let absent = wiki.len() + vector.len();
    let mut scored: Vec<(&str, f64, usize, usize)> = body
        .keys()
        .filter(|path| !pinned_paths.contains(**path))
        .map(|path| {
            let mut sum = 0.0;
            for rank in [wiki_at.get(path), vector_at.get(path)]
                .into_iter()
                .flatten()
            {
                sum += crate::retrieve::rrf_term(rank + 1)?;
            }
            Ok((
                *path,
                -sum,
                wiki_at.get(path).copied().unwrap_or(absent),
                vector_at.get(path).copied().unwrap_or(absent),
            ))
        })
        .collect::<Result<_>>()?;
    scored.sort_by(|a, b| {
        a.1.total_cmp(&b.1)
            .then_with(|| a.2.cmp(&b.2))
            .then_with(|| a.3.cmp(&b.3))
    });
    let room = k.saturating_sub(pinned.len());
    let mut admitted: Vec<&str> = scored.iter().take(room).map(|(path, ..)| *path).collect();
    admitted.sort_by_key(|path| sunk.contains(*path));
    Ok(pinned
        .iter()
        .map(|h| (h.source_path.clone(), h.snippet.clone()))
        .chain(
            admitted
                .iter()
                .map(|path| ((*path).to_owned(), body[path].to_owned())),
        )
        .take(k)
        .collect())
}

fn normalized(text: &str) -> String {
    text.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
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
    use std::collections::HashSet;

    use super::{
        WikiIndex, count_occurrences, extract_title_body, query_terms, score_doc, select_for_order,
    };

    fn note(dir: &std::path::Path, id: &str, title: &str, repeat: usize) {
        let body = "zebra ".repeat(repeat);
        std::fs::write(
            dir.join(format!("{id}.md")),
            format!("---\ntitle: {title}\n---\n{body}"),
        )
        .unwrap();
    }

    fn picked(dir: &std::path::Path, k: usize) -> Vec<(String, f32)> {
        let mut idx = WikiIndex::default();
        idx.refresh(dir).unwrap();
        select_for_order(&idx.search_ranked("zebra", None, None), k)
            .into_iter()
            .map(|h| (h.id, h.score))
            .collect()
    }

    // Expected values: Python wiki.search + answer._wiki_selected on the same notes.
    #[test]
    fn recall_picks_like_python_ties_creation_order_and_exact_lift() {
        let dir = tempfile::tempdir().unwrap();
        note(dir.path(), "wiki-0001", "zebra", 0);
        note(dir.path(), "wiki-0006", "Zebra", 0);
        note(dir.path(), "wiki-0004", "t4", 9);
        for id in ["wiki-0005", "wiki-0003", "wiki-0002", "zz-b", "zz-a"] {
            note(dir.path(), id, "tt", 5);
        }
        let want: Vec<(String, f32)> = [
            ("wiki-0006", 11.0),
            ("wiki-0001", 11.0),
            ("wiki-0004", 10.0),
            ("wiki-0005", 6.0),
        ]
        .map(|(i, s)| (i.to_owned(), s))
        .to_vec();
        assert_eq!(picked(dir.path(), 4), want);
        assert_eq!(picked(dir.path(), 1), vec![("wiki-0006".to_owned(), 11.0)]);
    }

    fn wiki_hit(path: &str, exact: bool) -> super::WikiHit {
        super::WikiHit {
            id: path.to_owned(),
            title: path.to_owned(),
            source_path: path.to_owned(),
            snippet: format!("W:{path}"),
            score: 1.0,
            exact_title: exact,
        }
    }

    // Python answer._wiki_selected: titles ride the whole pool (ties at the cut + exact titles),
    // not just the first `size` that go on to ordering.
    #[test]
    fn pool_titles_cover_the_pool_beyond_size() {
        let hit = |p: &str, score: f32, exact: bool| super::WikiHit {
            score,
            exact_title: exact,
            ..wiki_hit(p, exact)
        };
        let ranked = vec![
            hit("e.md", 0.0, true),
            hit("w1.md", 5.0, false),
            hit("w2.md", 3.0, false),
            hit("w3.md", 3.0, false),
            hit("w4.md", 1.0, false),
        ];
        let titles = super::pool_titles(&ranked, 2);
        let mut got: Vec<&str> = titles.keys().map(String::as_str).collect();
        got.sort_unstable();
        // size 2 -> threshold = 2nd hit (w1, 5.0): pool = exact e + w1 only; w3 ties w2 at 3.0 < 5.0.
        assert_eq!(got, vec!["e.md", "w1.md"]);
        let titles = super::pool_titles(&ranked[1..], 2);
        let mut got: Vec<&str> = titles.keys().map(String::as_str).collect();
        got.sort_unstable();
        // threshold = 2nd hit (w2, 3.0): w3 is in the pool but outside select_for_order's take(2).
        assert_eq!(got, vec!["w1.md", "w2.md", "w3.md"]);
        assert_eq!(select_for_order(&ranked[1..], 2).len(), 2);
    }

    fn vector_lines(paths: &[&str]) -> Vec<(String, String)> {
        paths
            .iter()
            .enumerate()
            .map(|(i, p)| ((*p).to_owned(), format!("V:{p}{i}")))
            .collect()
    }

    fn fused(wiki: &[(&str, bool)], vector: &[&str], k: usize) -> Vec<(String, String)> {
        let wiki: Vec<_> = wiki.iter().map(|(p, e)| wiki_hit(p, *e)).collect();
        super::fuse(&wiki, &vector_lines(vector), k, &HashSet::new()).unwrap()
    }

    fn fused_sunk(
        wiki: &[(&str, bool)],
        vector: &[&str],
        k: usize,
        sunk: &[&str],
    ) -> Vec<(String, String)> {
        let wiki: Vec<_> = wiki.iter().map(|(p, e)| wiki_hit(p, *e)).collect();
        let sunk: HashSet<String> = sunk.iter().map(|p| (*p).to_owned()).collect();
        super::fuse(&wiki, &vector_lines(vector), k, &sunk).unwrap()
    }

    // Expected values: Python answer._fused with rank_facts marking `sunk` superseded.
    #[test]
    fn fuse_sinks_superseded_inside_the_cut_without_cutting_more() {
        let w = [("a", false), ("b", false), ("c", false)];
        let lines = fused_sunk(&w, &["v1", "v2"], 4, &["a"]);
        assert_eq!(
            shown(&lines),
            [("v1", "V:v10"), ("b", "W:b"), ("v2", "V:v21"), ("a", "W:a")]
        );
        let lines = fused_sunk(&w, &["v1", "v2"], 3, &["v2"]);
        assert_eq!(shown(&lines), [("a", "W:a"), ("v1", "V:v10"), ("b", "W:b")]);
        let lines = fused_sunk(&w, &["v1", "v2"], 4, &["v1"]);
        assert_eq!(
            shown(&lines),
            [("a", "W:a"), ("b", "W:b"), ("v2", "V:v21"), ("v1", "V:v10")]
        );
        let pinned = [("e", true), ("a", false), ("b", false), ("c", false)];
        let lines = fused_sunk(&pinned, &["v1"], 3, &["a", "e"]);
        assert_eq!(shown(&lines), [("e", "W:e"), ("v1", "V:v10"), ("a", "W:a")]);
        let lines = fused_sunk(&w, &["v1", "v2"], 2, &["c"]);
        assert_eq!(shown(&lines), [("a", "W:a"), ("v1", "V:v10")]);
    }

    fn shown(lines: &[(String, String)]) -> Vec<(&str, &str)> {
        lines
            .iter()
            .map(|(p, b)| (p.as_str(), b.as_str()))
            .collect()
    }

    // Expected values: Python answer._fused on the same inputs (Seams.rank_facts -> {}).
    #[test]
    fn fuse_matches_python_rrf_pin_ties_and_duplicates() {
        let wiki = [("e", true), ("a", false), ("b", false)];
        let lines = fused(&wiki, &["v1", "b", "a", "v2", "v2"], 5);
        assert_eq!(
            shown(&lines),
            [
                ("e", "W:e"),
                ("a", "W:a"),
                ("b", "W:b"),
                ("v1", "V:v10"),
                ("v2", "V:v23")
            ]
        );

        let lines = fused(&wiki, &["v1", "a", "v2"], 3);
        assert_eq!(shown(&lines), [("e", "W:e"), ("a", "W:a"), ("v1", "V:v10")]);

        let lines = fused(&[("a", false)], &["b"], 2);
        assert_eq!(shown(&lines), [("a", "W:a"), ("b", "V:b0")]);

        let lines = fused(&[("a", false)], &["b"], 1);
        assert_eq!(shown(&lines), [("a", "W:a")]);

        let lines = fused(&[("a", false)], &["v1", "v2", "v3"], 4);
        assert_eq!(
            shown(&lines),
            [
                ("a", "W:a"),
                ("v1", "V:v10"),
                ("v2", "V:v21"),
                ("v3", "V:v32")
            ]
        );

        let lines = fused(&[("x", false), ("e", true)], &["v1"], 3);
        assert_eq!(shown(&lines), [("x", "W:x"), ("v1", "V:v10"), ("e", "W:e")]);

        let lines = fused(&[("a", false), ("b", false)], &["v1"], 1);
        assert_eq!(shown(&lines), [("a", "W:a")]);
    }

    #[test]
    fn exact_title_beyond_k_still_lifts_the_pool_ceiling() {
        let dir = tempfile::tempdir().unwrap();
        note(dir.path(), "wiki-0009", "zebra", 0);
        note(dir.path(), "wiki-0001", "zebra", 16);
        note(dir.path(), "wiki-0004", "t4", 9);
        assert_eq!(picked(dir.path(), 1), vec![("wiki-0009".to_owned(), 21.0)]);
    }

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
