//! Ask — retrieval → context → Llm synthesis → answer + sources.
//!
//! SRP: `answer()` is pure logic (returns data), `run()` is the CLI I/O shell.
use std::collections::HashSet;
use std::fmt::Write as _;

use anyhow::Result;

use std::path::Path;

use crate::llm::Llm;
use crate::retrieve;
use crate::store::Store;
use crate::wiki_recall;

const SYSTEM: &str = "너는 사용자의 개인 비서다. 한국어로 답한다.\n\
[간결] 서론·반복·군더더기 금지. 핵심만. 목록은 한 줄짜리 불릿, 질문이 작으면 1~2문장으로 끝낸다.\n\
[근거] '회수된 메모리'에 관련 내용이 있으면 그것만 근거로 쓰고 끝에 출처 파일명을 적는다.\n\
[날조 금지] 메모리에 없는 '사실·미해결 할 일·리마인더·계획·일정'은 절대 지어내지 마라. \
해당 항목이 메모리에 없으면 '메모리에 없음'이라 적거나 생략한다(그럴듯한 이름·계획을 만들어내면 안 된다).\n\
[일반지식] 순수 상식 질문은 도와주되 일반지식임을 한 줄로 밝힌다. \
단 사용자의 프로젝트·할 일·결정·사실을 일반지식으로 추측해 채우지 마라.";

/// `answer()` return value — used by both the HTTP handler and the CLI.
pub struct AnswerOut {
    pub answer: String,
    pub sources: Vec<String>,
}

/// Pure logic: retrieval + LLM synthesis → returns `AnswerOut`. No I/O.
pub async fn answer(
    store: &Store,
    llm: &Llm,
    question: &str,
    exclude_origins: &[String],
) -> Result<AnswerOut> {
    let hits = retrieve::retrieve(store, llm, question, 5, exclude_origins).await?;
    if hits.is_empty() {
        return Ok(AnswerOut {
            answer: "관련 메모리를 못 찾았어요. (ingest 먼저?)".to_owned(),
            sources: vec![],
        });
    }

    let mut context = String::new();
    for (i, h) in hits.iter().enumerate() {
        let _ = write!(context, "## [{i}] {}\n{}\n\n", h.source_path, h.content);
    }

    // local GraphRAG: pull in the **concept-linked documents** (sharing concept/tool) of the top hits, full body included.
    // Reinforce answers buried in vector noise via the graph — with actual content, not just labels.
    // Exclude documents already in the vector hits (avoid duplicates), up to 3 linked documents, each capped at 1200 chars.
    let hit_paths: HashSet<String> = hits.iter().map(|h| h.source_path.clone()).collect();
    let mut seen_g: HashSet<String> = hit_paths.clone();
    let mut graph_ctx = String::new();
    for h in hits.iter().take(2) {
        for rd in store.related_doc_content(&h.source_path, 3).await? {
            if seen_g.len() >= hit_paths.len() + 3 {
                break;
            }
            if seen_g.insert(rd.source_path.clone()) {
                let snip: String = rd.content.chars().take(1200).collect();
                let _ = write!(graph_ctx, "## {}\n{snip}\n\n", rd.source_path);
            }
        }
    }

    // Authority injection: **current** claims close to the query (superseded_at NULL) — time-axis facts take priority over chunks.
    // "What's the DB?" → the claim 'oh-my-boring database is pgvector' beats old chunk noise.
    let q_emb = llm.embed(question).await?;
    let mut claim_ctx = String::new();
    for (s, p, v) in store.current_claims(&q_emb, 5).await? {
        let _ = writeln!(claim_ctx, "- {s} {p} {v}");
    }

    let mut prompt = String::new();
    if !claim_ctx.is_empty() {
        let _ = write!(
            prompt,
            "# 현재 사실(권위 — 같은 주제 충돌 시 이게 최신, 따른다)\n{claim_ctx}\n"
        );
    }
    let _ = write!(prompt, "# 회수된 메모리\n{context}\n");
    if !graph_ctx.is_empty() {
        let _ = write!(prompt, "# 그래프로 연결된 문서\n{graph_ctx}\n");
    }
    let _ = write!(prompt, "# 질문\n{question}");
    let answer_text = llm.generate(SYSTEM, &prompt).await?;

    let mut seen = HashSet::new();
    let sources: Vec<String> = hits
        .iter()
        .filter(|h| seen.insert(h.source_path.clone()))
        .map(|h| h.source_path.clone())
        .collect();

    Ok(AnswerOut {
        answer: answer_text.trim().to_owned(),
        sources,
    })
}

/// wiki-first-class retrieval (`DRUDGE_VECTOR=off`): direct read of vault/wiki → LLM synthesis. No graph/claim authority (vector-only).
/// If `wiki_dir` is unset, returns an empty-memory notice. SRP: pure logic (IO lives only in wiki_recall).
pub async fn answer_wiki(llm: &Llm, wiki_dir: Option<&Path>, question: &str) -> Result<AnswerOut> {
    let Some(dir) = wiki_dir else {
        return Ok(AnswerOut {
            answer: "vault 가 설정되지 않았어요. (DRUDGE_VAULT_DIR)".to_owned(),
            sources: vec![],
        });
    };
    let hits = wiki_recall::recall(dir, question, 5)?;
    if hits.is_empty() {
        return Ok(AnswerOut {
            answer: "관련 메모리를 못 찾았어요. (vault/wiki 비었거나 sync 전?)".to_owned(),
            sources: vec![],
        });
    }
    let mut context = String::new();
    for (i, h) in hits.iter().enumerate() {
        let _ = write!(
            context,
            "## [{i}] {} ({})\n{}\n\n",
            h.title, h.source_path, h.snippet
        );
    }
    let prompt = format!("# 회수된 메모리(vault/wiki)\n{context}\n# 질문\n{question}");
    let answer_text = llm.generate(SYSTEM, &prompt).await?;
    let sources: Vec<String> = hits.into_iter().map(|h| h.source_path).collect();
    Ok(AnswerOut {
        answer: answer_text.trim().to_owned(),
        sources,
    })
}

const BRIEF_SYSTEM: &str = "너는 사용자의 개인 비서다. 한국어로 '최근 작업 브리핑'을 만든다.\n\
[최신우선] 아래 기록은 최신순으로 정렬돼 있다(위가 가장 최근). \
같은 주제에 옛 기록과 최신 기록이 충돌하면 무조건 위(최신)를 따른다 — 옛 사실로 최신을 덮지 마라. \
예: 위에 'pgvector'면 아래 'SurrealDB'는 이미 폐기된 과거다, 최신만 말한다.\n\
[구체] 어떤 프로젝트에서 무슨 결정·구현·이전 작업을 했고 무엇이 남았는지, \
고유명사(프로젝트·도구·모델·파일)를 그대로 써서 짧은 불릿으로. 추상적 취향·일반론 금지.\n\
[날조 금지] 기록에 없는 사실·할 일·일정은 지어내지 마라. 없으면 생략한다.\n\
[형식] 프로젝트별로 묶어 3~6줄. 서론·인사 없이 바로 본문.";

/// Recency-first/supersede briefing: retrieve by `updated_at` descending rather than semantic similarity →
/// synthesize so the latest beats the old. Called by the cron morning briefing (`/brief`). SRP: separate from `answer()`.
pub async fn brief(store: &Store, llm: &Llm, exclude_origins: &[String]) -> Result<AnswerOut> {
    let docs = store.recent_docs(12, exclude_origins).await?;
    if docs.is_empty() {
        return Ok(AnswerOut {
            answer: "최근 적재된 작업 기록이 없어요. (ingest 먼저?)".to_owned(),
            sources: vec![],
        });
    }

    let mut context = String::new();
    for (i, d) in docs.iter().enumerate() {
        // i=0 is the most recent. Embed the rank in the label so the LLM keeps recency-first.
        let _ = write!(
            context,
            "## [{i}] (최신순 {}위) {} · {}\n{}\n\n",
            i + 1,
            d.project,
            d.source_path,
            d.content
        );
    }

    // Authority injection: current claims (recency order) — even if old exploration notes (e.g. discarded Neo4j/SurrealDB)
    // look recent by mtime, claim authority nails down the true current fact.
    let mut claim_ctx = String::new();
    for (s, p, v) in store.recent_claims(12).await? {
        let _ = writeln!(claim_ctx, "- {s} {p} {v}");
    }
    let prompt = if claim_ctx.is_empty() {
        format!("# 최근 작업 기록 (최신순, 위가 최신)\n{context}")
    } else {
        format!(
            "# 현재 사실(권위 — 충돌 시 이게 최신, 따른다)\n{claim_ctx}\n# 최근 작업 기록 (최신순, 위가 최신)\n{context}"
        )
    };
    let answer_text = llm.generate(BRIEF_SYSTEM, &prompt).await?;

    let sources: Vec<String> = docs.iter().map(|d| d.source_path.clone()).collect();
    Ok(AnswerOut {
        answer: answer_text.trim().to_owned(),
        sources,
    })
}

/// CLI shell: call `answer()` then print to stdout.
pub async fn run(
    store: &Store,
    llm: &Llm,
    question: &str,
    exclude_origins: &[String],
) -> Result<()> {
    let out = answer(store, llm, question, exclude_origins).await?;
    println!("{}\n", out.answer);
    if !out.sources.is_empty() {
        println!("출처:");
        for src in &out.sources {
            println!("  - {src}");
        }
    }
    Ok(())
}
