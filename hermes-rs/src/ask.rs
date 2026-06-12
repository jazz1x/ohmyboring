//! Ask — 회수 → 컨텍스트 → Ollama 합성 → 답변 + 출처. (v1 generate_answer 패리티)
//!
//! SRP: `answer()` 는 순수 로직(데이터 반환), `run()` 은 CLI I/O 껍질.
use std::collections::HashSet;
use std::fmt::Write as _;

use anyhow::Result;

use crate::ollama::Ollama;
use crate::retrieve;
use crate::store::Store;

const SYSTEM: &str = "너는 사용자의 개인 비서다. 한국어로 답한다.\n\
[간결] 서론·반복·군더더기 금지. 핵심만. 목록은 한 줄짜리 불릿, 질문이 작으면 1~2문장으로 끝낸다.\n\
[근거] '회수된 메모리'에 관련 내용이 있으면 그것만 근거로 쓰고 끝에 출처 파일명을 적는다.\n\
[날조 금지] 메모리에 없는 '사실·미해결 할 일·리마인더·계획·일정'은 절대 지어내지 마라. \
해당 항목이 메모리에 없으면 '메모리에 없음'이라 적거나 생략한다(그럴듯한 이름·계획을 만들어내면 안 된다).\n\
[일반지식] 순수 상식 질문은 도와주되 일반지식임을 한 줄로 밝힌다. \
단 사용자의 프로젝트·할 일·결정·사실을 일반지식으로 추측해 채우지 마라.";

/// `answer()` 반환값 — HTTP 핸들러와 CLI 모두 사용.
pub struct AnswerOut {
    pub answer: String,
    pub sources: Vec<String>,
}

/// 순수 로직: 회수 + LLM 합성 → `AnswerOut` 반환. I/O 없음.
pub async fn answer(
    store: &Store,
    ollama: &Ollama,
    question: &str,
    exclude_origins: &[String],
) -> Result<AnswerOut> {
    let hits = retrieve::retrieve(store, ollama, question, 5, exclude_origins).await?;
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

    // local GraphRAG: 상위 히트의 **개념 연결문서**(concept/tool 공유)를 본문째 끌어온다.
    // 벡터가 노이즈에 묻은 정답을 그래프로 보강 — 라벨만이 아니라 실제 내용으로.
    // 이미 벡터히트인 문서는 제외(중복 회피), 연결문서 최대 3개·각 1200자 컷.
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

    // 권위 주입: 질의와 가까운 **현재** claim(superseded_at NULL) — 시간축 사실이 청크보다 우선.
    // "DB 뭐?" → claim 'olympus database is pgvector' 가 옛 청크 노이즈를 이긴다.
    let q_emb = ollama.embed(question).await?;
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
    let answer_text = ollama.generate(SYSTEM, &prompt).await?;

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

const BRIEF_SYSTEM: &str = "너는 사용자의 개인 비서다. 한국어로 '최근 작업 브리핑'을 만든다.\n\
[최신우선] 아래 기록은 최신순으로 정렬돼 있다(위가 가장 최근). \
같은 주제에 옛 기록과 최신 기록이 충돌하면 무조건 위(최신)를 따른다 — 옛 사실로 최신을 덮지 마라. \
예: 위에 'pgvector'면 아래 'SurrealDB'는 이미 폐기된 과거다, 최신만 말한다.\n\
[구체] 어떤 프로젝트에서 무슨 결정·구현·이전 작업을 했고 무엇이 남았는지, \
고유명사(프로젝트·도구·모델·파일)를 그대로 써서 짧은 불릿으로. 추상적 취향·일반론 금지.\n\
[날조 금지] 기록에 없는 사실·할 일·일정은 지어내지 마라. 없으면 생략한다.\n\
[형식] 프로젝트별로 묶어 3~6줄. 서론·인사 없이 바로 본문.";

/// 최신우선/supersede 브리핑: 의미유사도가 아니라 `updated_at` 내림차순 회수 →
/// 최신이 옛것을 이기게 합성. cron 아침 브리핑(`/brief`)이 호출. SRP: `answer()`와 분리.
pub async fn brief(
    store: &Store,
    ollama: &Ollama,
    exclude_origins: &[String],
) -> Result<AnswerOut> {
    let docs = store.recent_docs(12, exclude_origins).await?;
    if docs.is_empty() {
        return Ok(AnswerOut {
            answer: "최근 적재된 작업 기록이 없어요. (ingest 먼저?)".to_owned(),
            sources: vec![],
        });
    }

    let mut context = String::new();
    for (i, d) in docs.iter().enumerate() {
        // i=0 이 가장 최신. 라벨에 순위를 박아 LLM 이 최신우선을 지키게 한다.
        let _ = write!(
            context,
            "## [{i}] (최신순 {}위) {} · {}\n{}\n\n",
            i + 1,
            d.project,
            d.source_path,
            d.content
        );
    }

    // 권위 주입: 현재 claim(최신순) — 옛 탐색노트(예: 폐기된 Neo4j/SurrealDB)가 mtime상
    // 최신처럼 보여도, claim 권위가 진짜 현재 사실을 못박는다.
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
    let answer_text = ollama.generate(BRIEF_SYSTEM, &prompt).await?;

    let sources: Vec<String> = docs.iter().map(|d| d.source_path.clone()).collect();
    Ok(AnswerOut {
        answer: answer_text.trim().to_owned(),
        sources,
    })
}

/// CLI 껍질: `answer()` 호출 후 stdout 출력.
pub async fn run(
    store: &Store,
    ollama: &Ollama,
    question: &str,
    exclude_origins: &[String],
) -> Result<()> {
    let out = answer(store, ollama, question, exclude_origins).await?;
    println!("{}\n", out.answer);
    if !out.sources.is_empty() {
        println!("출처:");
        for src in &out.sources {
            println!("  - {src}");
        }
    }
    Ok(())
}
