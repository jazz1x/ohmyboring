//! Extract — 문서별 LLM 추출: problem/solution/tool/concept/attempt 노드 + 엣지 생성.
//! LLM(qwen2.5:7b)에서 strict JSON 한 번 파싱 후 그대로 신뢰(parse-don't-validate).
//! 문서 파싱 실패 → Err 로그 + skipped 카운트 + 계속(graceful boundary, ROP).
use anyhow::Result;
use serde::Deserialize;

use crate::ollama::Ollama;
use crate::store::Store;

const SYSTEM: &str = "You are a precise JSON extractor. Output ONLY a single JSON object — no prose, no markdown fences. /no_think";

const PROMPT_TMPL: &str = r#"Extract from the document below. Return EXACTLY this JSON shape (no extra keys):
{"problems":["string"],"solution":"string","tools":["string"],"concepts":["string"],"attempts":[{"what":"string","outcome":"worked"}],"claims":[{"subject":"string","predicate":"string","value":"string"}]}

Rules:
- problems: technical problems or pain-points addressed (short phrases, max 5)
- solution: one-line summary of how it was solved (empty string if N/A)
- tools: software tools, libraries, databases, services used
  * canonical SHORT names only (e.g. "postgres" not "PostgreSQL 15", "surrealdb" not "SurrealDB v3")
  * lowercase preferred, no version numbers
  * deduplicate: list each tool only once
  * MAX 6 tools total
- concepts: key technical concepts or patterns
  * short canonical names, lowercase preferred, deduplicate
  * MAX 6 concepts total
- attempts: things tried during problem-solving (may be empty array [])
  * what: short description of what was tried
  * outcome: EXACTLY one of "worked", "failed", or "abandoned"
- claims: durable FACTS or DECISIONS stated as (subject, predicate, value) triples (may be []).
  * ONLY stable facts/decisions that could change over time, NOT transient events.
    Good: {"subject":"olympus database","predicate":"is","value":"pgvector"}
          {"subject":"olympus llm","predicate":"is","value":"gemma4:12b"}
    Bad (transient): "fixed a bug", "ran tests"
  * subject = the thing the fact is about (short noun). predicate = relation (is/uses/decided).
  * value = current value. MAX 4 claims. Skip if none.
- IMPORTANT LANGUAGE RULE: ALL string values MUST be in Korean or English ONLY.
  Do NOT use Chinese characters (漢字/汉字). Do NOT use Japanese kanji.
  Korean technical terms use Hangul (가-힣) or Latin letters — never Han ideographs (一二三 etc.).
  If you are tempted to write Chinese/Japanese characters, write the equivalent in English instead.
- Use empty arrays/string if not applicable.

Document:
---
{BODY}
---
/no_think"#;

/// LLM 추출 결과 (타입 파싱 1회 — parse-don't-validate).
#[derive(Debug, Deserialize)]
struct Extracted {
    problems: Vec<String>,
    solution: String,
    tools: Vec<String>,
    concepts: Vec<String>,
    #[serde(default)]
    attempts: Vec<AttemptRaw>,
    #[serde(default)]
    claims: Vec<ClaimRaw>,
}

/// 시간축 사실 1개 — (subject, predicate, value). 새 value 가 옛 value 를 supersede.
#[derive(Debug, Deserialize)]
struct ClaimRaw {
    subject: String,
    predicate: String,
    value: String,
}

/// claim subject/predicate 정규화 키 — 소문자·trim·공백단일화(매칭 일관성 + 가독성).
fn canon(s: &str) -> String {
    s.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// LLM이 뱉은 attempt 원본 — outcome은 경계에서 Outcome 으로 변환된다.
#[derive(Debug, Deserialize)]
struct AttemptRaw {
    what: String,
    outcome: String,
}

/// LLM outcome 의 허용 값 집합 (ADT — 불가능한 상태를 표현 불가능하게).
#[derive(Debug, Clone, Copy)]
enum Outcome {
    Worked,
    Failed,
    Abandoned,
}

impl Outcome {
    /// 경계 파싱 — 대소문자·공백 허용. 알 수 없는 값 → `None`(호출측이 skip).
    fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "worked" => Some(Self::Worked),
            "failed" => Some(Self::Failed),
            "abandoned" => Some(Self::Abandoned),
            _ => None,
        }
    }

    /// DB 저장 시 canonical lowercase str.
    const fn as_str(self) -> &'static str {
        match self {
            Self::Worked => "worked",
            Self::Failed => "failed",
            Self::Abandoned => "abandoned",
        }
    }
}

/// CJK 통합 한자(U+4E00..=U+9FFF) 포함 여부 판별.
/// 주의: 한국어 한자 표기(음차)를 극히 드물게 사용할 수도 있으나,
/// qwen 모델이 중국어로 drift 한 토큰을 걸러내는 게 목적이므로 이 범위를 차단한다.
fn has_han(s: &str) -> bool {
    s.chars().any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c))
}

/// 슬러그: 소문자 + `[a-z0-9]` 만 유지(구분자 완전 제거) → 변형 표기 충돌 방지.
/// 예: `macos keychain` / `macos_keychain` / `macoskeychain` → 모두 `macoskeychain`.
/// 별칭 맵: `c++`→`cpp`, `c#`→`csharp` (그대로 두면 `c`로 축약돼 충돌).
fn slugify(s: &str) -> String {
    // 별칭 정규화 (소문자 이후 적용)
    let lower = s.to_lowercase();
    let normalized = lower
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace(".net", "dotnet");
    // [a-z0-9] 만 남김 — 구분자(공백·언더스코어·하이픈 등) 완전 제거
    normalized
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

/// JSON 펜스 및 앞선 산문 제거.
/// `{` 첫 위치를 찾고, 그 이후 범위에서만 `}` 를 역탐색한다.
/// `}` 가 `{` 앞에만 있거나 `{` 가 없으면 `""` 반환 (호출측: parse 실패 → skip).
fn strip_to_json(raw: &str) -> &str {
    let Some(start) = raw.find('{') else {
        return "";
    };
    // start 이후 부분에서 마지막 `}` 탐색
    let suffix = &raw[start..];
    let Some(rel_end) = suffix.rfind('}') else {
        return "";
    };
    let end = start + rel_end + 1;
    // 방어: start >= end 이면 빈 슬라이스 → parse 실패로 처리
    if start >= end {
        return "";
    }
    &raw[start..end]
}

pub struct ExtractStats {
    pub processed: usize,
    pub skipped: usize,
    pub problems: usize,
    pub solutions: usize,
    pub tools: usize,
    pub concepts: usize,
    pub attempts: usize,
    pub edges: usize,
}

#[allow(clippy::too_many_lines)]
pub async fn run(store: &Store, ollama: &Ollama) -> Result<ExtractStats> {
    // 증분: sha 가 바뀐(또는 신규) 문서만 — gemma4 추출 비용 절감(타입 온톨로지는 보존).
    let docs = store.docs_needing_extract().await?;
    let mut stats = ExtractStats {
        processed: 0,
        skipped: 0,
        problems: 0,
        solutions: 0,
        tools: 0,
        concepts: 0,
        attempts: 0,
        edges: 0,
    };

    for (path, body) in &docs {
        let body_snip: String = body.chars().take(3000).collect();
        let prompt = PROMPT_TMPL.replace("{BODY}", &body_snip);
        let raw = match ollama.generate(SYSTEM, &prompt).await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("⚠ extract LLM error [{path}]: {e}");
                stats.skipped += 1;
                continue;
            }
        };

        let json_str = strip_to_json(raw.trim());
        let extracted: Extracted = match serde_json::from_str(json_str) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("⚠ extract parse error [{path}]: {e} — raw: {json_str:.120}");
                stats.skipped += 1;
                continue;
            }
        };

        // 멱등화: 이 문서의 기존 시맨틱 엣지 + attempt 노드를 먼저 삭제
        store.clear_semantic_edges(path).await?;

        // problem 노드 + addresses 엣지 (Han drift 필터)
        for text in &extracted.problems {
            let t = text.trim();
            if t.is_empty() || has_han(t) {
                continue;
            }
            let slug = slugify(t);
            if slug.is_empty() {
                continue;
            }
            store.upsert_problem(&slug, t).await?;
            store.relate_doc_problem(path, &slug).await?;
            stats.problems += 1;
            stats.edges += 1;
        }

        // solution 노드 + resolved_by 엣지 (Han drift 필터)
        {
            let t = extracted.solution.trim();
            if !t.is_empty() && !has_han(t) {
                let slug = slugify(t);
                if !slug.is_empty() {
                    store.upsert_solution(&slug, t).await?;
                    store.relate_doc_solution(path, &slug).await?;
                    stats.solutions += 1;
                    stats.edges += 1;
                }
            }
        }

        // tool 노드 + uses 엣지 (Han drift 필터 + doc-level slug dedup)
        let mut seen_tool_slugs = std::collections::HashSet::new();
        for text in &extracted.tools {
            let t = text.trim();
            if t.is_empty() || has_han(t) {
                continue;
            }
            let slug = slugify(t);
            if slug.is_empty() || !seen_tool_slugs.insert(slug.clone()) {
                continue;
            }
            store.upsert_tool(&slug, t).await?;
            store.relate_doc_tool(path, &slug).await?;
            stats.tools += 1;
            stats.edges += 1;
        }

        // concept 노드 + about 엣지 (Han drift 필터 + doc-level slug dedup)
        let mut seen_concept_slugs = std::collections::HashSet::new();
        for text in &extracted.concepts {
            let t = text.trim();
            if t.is_empty() || has_han(t) {
                continue;
            }
            let slug = slugify(t);
            if slug.is_empty() || !seen_concept_slugs.insert(slug.clone()) {
                continue;
            }
            store.upsert_concept(&slug, t).await?;
            store.relate_doc_concept(path, &slug).await?;
            stats.concepts += 1;
            stats.edges += 1;
        }

        // attempt 노드 + tried 엣지 (per-doc, Han drift 필터)
        let mut created_idxs: Vec<usize> = Vec::new();
        for (idx, attempt) in extracted.attempts.iter().enumerate() {
            let what = attempt.what.trim();
            if what.is_empty() || has_han(what) {
                continue;
            }
            // 경계 파싱: LLM outcome → Outcome enum. 알 수 없는 값은 skip (불법 상태 저장 금지).
            let Some(outcome) = Outcome::parse(&attempt.outcome) else {
                continue;
            };
            store
                .upsert_attempt(path, idx, what, outcome.as_str())
                .await?;
            store.relate_doc_attempt(path, idx).await?;
            created_idxs.push(idx);
            stats.attempts += 1;
            stats.edges += 1;
        }
        // lineage: 연속 시도를 leads_to 로 연결 (문제해결 서사 순서 = entity↔entity 엣지)
        for pair in created_idxs.windows(2) {
            if let [from, to] = pair {
                store.relate_leads_to(path, *from, *to).await?;
                stats.edges += 1;
            }
        }

        // claim: 시간축 사실 권위 — (subject,predicate)→value, 새 value 가 옛것 supersede.
        // valid_from = 문서 mtime(시간순 정렬). value 임베딩(bge-m3, gemma 추가호출 아님).
        if !extracted.claims.is_empty() {
            let valid_from = store.doc_updated_at(path).await?;
            for cl in &extracted.claims {
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
                let emb = ollama
                    .embed(&format!("{subject} {predicate} {value}"))
                    .await?;
                store
                    .upsert_claim(&subject, &predicate, value, path, valid_from, &emb)
                    .await?;
            }
        }

        store.mark_extracted(path).await?; // 증분: 이 sha 는 추출 완료 표시
        stats.processed += 1;
    }

    Ok(stats)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::Outcome;

    #[test]
    fn outcome_parse_valid() {
        assert!(matches!(Outcome::parse("worked"), Some(Outcome::Worked)));
        assert!(matches!(Outcome::parse("WORKED"), Some(Outcome::Worked)));
        assert!(matches!(
            Outcome::parse("  Failed  "),
            Some(Outcome::Failed)
        ));
        assert!(matches!(
            Outcome::parse("ABANDONED"),
            Some(Outcome::Abandoned)
        ));
    }

    #[test]
    fn outcome_parse_invalid_is_none() {
        // 불법 값은 None → 호출측이 skip (불법 상태 저장 금지)
        assert!(Outcome::parse("").is_none());
        assert!(Outcome::parse("partial").is_none());
        assert!(Outcome::parse("success").is_none());
    }

    #[test]
    fn outcome_as_str_is_canonical_lowercase() {
        assert_eq!(Outcome::Worked.as_str(), "worked");
        assert_eq!(Outcome::Failed.as_str(), "failed");
        assert_eq!(Outcome::Abandoned.as_str(), "abandoned");
    }
}
