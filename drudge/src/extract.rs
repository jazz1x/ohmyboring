//! Extract — per-document LLM extraction: creates problem/solution/tool/concept/attempt nodes + edges.
//! Parse strict JSON once from the LLM (qwen2.5:7b) and trust it as-is (parse-don't-validate).
//! Document parse failure → Err log + skipped count + continue (graceful boundary, ROP).
use anyhow::Result;
use serde::Deserialize;

use crate::llm::Llm;
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
    Good: {"subject":"oh-my-boring database","predicate":"is","value":"pgvector"}
          {"subject":"oh-my-boring llm","predicate":"is","value":"gemma4:12b"}
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

/// LLM extraction result (typed parse once — parse-don't-validate).
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

/// One temporal fact — (subject, predicate, value). A new value supersedes the old value.
#[derive(Debug, Deserialize)]
struct ClaimRaw {
    subject: String,
    predicate: String,
    value: String,
}

/// claim subject/predicate normalization key — lowercase, trim, collapse whitespace (matching consistency + readability).
fn canon(s: &str) -> String {
    s.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Raw attempt emitted by the LLM — outcome is converted to Outcome at the boundary.
#[derive(Debug, Deserialize)]
struct AttemptRaw {
    what: String,
    outcome: String,
}

/// Set of allowed values for the LLM outcome (ADT — make impossible states unrepresentable).
#[derive(Debug, Clone, Copy)]
enum Outcome {
    Worked,
    Failed,
    Abandoned,
}

impl Outcome {
    /// Boundary parse — case-insensitive, whitespace-tolerant. Unknown value → `None` (caller skips).
    fn parse(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "worked" => Some(Self::Worked),
            "failed" => Some(Self::Failed),
            "abandoned" => Some(Self::Abandoned),
            _ => None,
        }
    }

    /// Canonical lowercase str for DB storage.
    const fn as_str(self) -> &'static str {
        match self {
            Self::Worked => "worked",
            Self::Failed => "failed",
            Self::Abandoned => "abandoned",
        }
    }
}

/// Detect whether the string contains CJK Unified Ideographs (U+4E00..=U+9FFF).
/// Note: Korean text may very rarely use Han characters (phonetic borrowing),
/// but the purpose is to filter out tokens where the qwen model drifted into Chinese,
/// so this range is blocked.
fn has_han(s: &str) -> bool {
    s.chars().any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c))
}

/// Slug: lowercase + keep only `[a-z0-9]` (remove all separators) → prevents variant-spelling collisions.
/// e.g. `macos keychain` / `macos_keychain` / `macoskeychain` → all become `macoskeychain`.
/// Alias map: `c++`→`cpp`, `c#`→`csharp` (left as-is they would collapse to `c` and collide).
fn slugify(s: &str) -> String {
    // Alias normalization (applied after lowercasing)
    let lower = s.to_lowercase();
    let normalized = lower
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace(".net", "dotnet");
    // Keep only [a-z0-9] — remove all separators (spaces, underscores, hyphens, etc.)
    normalized
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

/// Strip JSON fences and any leading prose.
/// Find the first `{`, then reverse-search for `}` only within the range after it.
/// If `}` exists only before `{`, or there is no `{`, return `""` (caller: parse failure → skip).
fn strip_to_json(raw: &str) -> &str {
    let Some(start) = raw.find('{') else {
        return "";
    };
    // Search for the last `}` in the part after start
    let suffix = &raw[start..];
    let Some(rel_end) = suffix.rfind('}') else {
        return "";
    };
    let end = start + rel_end + 1;
    // Defensive: if start >= end, empty slice → treated as parse failure
    if start >= end {
        return "";
    }
    &raw[start..end]
}

#[derive(Default)]
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
pub async fn run(store: &Store, llm: &Llm) -> Result<ExtractStats> {
    // Incremental: only documents whose sha changed (or are new) — saves gemma4 extraction cost (type ontology is preserved).
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
        let raw = match llm.generate(SYSTEM, &prompt).await {
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

        // Idempotency: first delete this document's existing semantic edges + attempt nodes
        store.clear_semantic_edges(path).await?;

        // problem nodes + addresses edges (Han drift filter)
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

        // solution node + resolved_by edge (Han drift filter)
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

        // tool nodes + uses edges (Han drift filter + doc-level slug dedup)
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

        // concept nodes + about edges (Han drift filter + doc-level slug dedup)
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

        // attempt nodes + tried edges (per-doc, Han drift filter)
        let mut created_idxs: Vec<usize> = Vec::new();
        for (idx, attempt) in extracted.attempts.iter().enumerate() {
            let what = attempt.what.trim();
            if what.is_empty() || has_han(what) {
                continue;
            }
            // Boundary parse: LLM outcome → Outcome enum. Unknown values are skipped (do not store illegal state).
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
        // lineage: connect consecutive attempts with leads_to (problem-solving narrative order = entity↔entity edge)
        for pair in created_idxs.windows(2) {
            if let [from, to] = pair {
                store.relate_leads_to(path, *from, *to).await?;
                stats.edges += 1;
            }
        }

        // claim: temporal-fact authority — (subject,predicate)→value, a new value supersedes the old.
        // valid_from = document mtime (chronological ordering). value embedding (bge-m3, not an extra gemma call).
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
                let emb = llm.embed(&format!("{subject} {predicate} {value}")).await?;
                store
                    .upsert_claim(&subject, &predicate, value, path, valid_from, &emb)
                    .await?;
            }
        }

        store.mark_extracted(path).await?; // incremental: mark this sha as extraction-complete
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
        // Illegal values are None → caller skips (do not store illegal state)
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
