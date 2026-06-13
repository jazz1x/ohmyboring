//! wiki_recall — `vault/wiki/*.md` 를 직접 읽어 회수한다 (pgvector·임베딩 불필요).
//!
//! Karpathy-wiki 1급 경로: 개인·소규모 코퍼스(수백 문서)에선 마크다운 직독이 RAG 보다 단순·신뢰·
//! 디버그 쉬움(2026 트렌드 + repo `CLAUDE.md` "simplest thing that works"). pgvector(vector+graph)는
//! 규모/정확도 trigger 넘을 때 켜는 옵셔널 가속기.
//!
//! 스코어링: 토큰 *동등*이 아니라 **부분일치 빈도**. 한국어 가산어미(임베딩→"임베딩은")·영어 부분어를
//! 포용한다(형태소 분석 없이 무난). title 일치는 가중. 순수 로직(`score_doc`)과 I/O(`recall`) 분리(SRP).
use std::path::Path;

use anyhow::{Context, Result};

/// 회수 결과 1건 (벡터 경로의 hit 와 호환되는 최소 필드).
#[derive(Debug, Clone)]
pub struct WikiHit {
    pub id: String,
    pub title: String,
    pub source_path: String,
    pub snippet: String,
    pub score: f32,
}

/// 쿼리를 검색어로 쪼갬 — 공백 분리 + 2자 이상 + 소문자. 순수.
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

/// `haystack` 안의 `needle` 비중첩 출현 횟수. 순수.
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

/// title+body 점수 + 스니펫. 매칭 0이면 None. 순수.
/// 점수 = Σ(body 출현) + 3·Σ(title 출현) + 커버리지(맞은 distinct 검색어 수).
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
    score += coverage; // 여러 검색어를 두루 맞춘 문서를 가산
    Some((
        precise_cast(score),
        snippet_around(body, first_hit.unwrap_or(0)),
    ))
}

/// usize → f32 (점수 규모상 무손실 범위). clippy cast 게이트 회피용 헬퍼.
#[allow(clippy::cast_precision_loss)]
fn precise_cast(n: usize) -> f32 {
    n as f32
}

/// `pos`(소문자 인덱스) 주변 ~200자 스니펫. 원문 body 에서 잘라냄(문자 경계 안전). 순수.
fn snippet_around(body: &str, pos: usize) -> String {
    let chars: Vec<char> = body.chars().collect();
    // pos 는 바이트 인덱스(소문자 기준) — 대략 문자 인덱스로 환산(ASCII 가정 깨질 수 있어 clamp).
    let approx = body.get(..pos).map_or(0, |s| s.chars().count());
    let start = approx.saturating_sub(40);
    let end = (start + 200).min(chars.len());
    let s: String = chars[start..end].iter().collect();
    let s = s.replace('\n', " ");
    if start > 0 {
        format!("…{}", s.trim())
    } else {
        s.trim().to_owned()
    }
}

/// `--- yaml ---\nbody` 분리 + frontmatter 의 title 추출. 없으면 첫 `# ` 헤딩, 그것도 없으면 stem. 순수.
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

/// `vault/wiki/*.md` 를 직접 읽어 query 와 가장 가까운 top-K (I/O 쉘 — 순수 `score_doc` 위임).
/// wiki 디렉터리 부재·읽기 실패는 graceful: 빈 결과/스킵(회수는 절대 패닉 안 함).
pub fn recall(wiki_dir: &Path, query: &str, k: usize) -> Result<Vec<WikiHit>> {
    let terms = query_terms(query);
    if terms.is_empty() {
        return Ok(Vec::new());
    }
    let mut hits: Vec<WikiHit> = Vec::new();
    let Ok(read_dir) = std::fs::read_dir(wiki_dir) else {
        return Ok(Vec::new()); // wiki 아직 없음 — 정상(빈 회수)
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
            continue; // 읽기 실패 스킵(graceful)
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
        assert_eq!(query_terms("bge-m3 임베딩 a"), vec!["bge-m3", "임베딩"]); // 1자 'a' 제외
    }

    #[test]
    fn count_occurrences_non_overlapping() {
        assert_eq!(count_occurrences("aaaa", "aa"), 2);
        assert_eq!(count_occurrences("임베딩은 임베딩", "임베딩"), 2);
        assert_eq!(count_occurrences("none", "x"), 0);
    }

    #[test]
    fn score_doc_substring_handles_korean_josa() {
        // 검색어 "임베딩" 이 body "임베딩은"(조사 붙음) 을 부분일치로 잡아야 함
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
