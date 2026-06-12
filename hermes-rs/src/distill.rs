//! Session distill — Claude Code 세션 텍스트를 문제해결 서사로 증류해 vault/raw 노트로 기록.
//!
//! # 경계 분리 (SSOT)
//! 호스트 훅(`hooks/distill-session.py`)은 *호스트 전용* 일만 한다: 트랜스크립트 읽기·텍스트
//! 추출·throttle 마커·세션 mtime 보정. LLM 증류·KEEP/SKIP 게이트·시크릿 스크럽·raw 노트
//! 포맷은 모두 이 엔진 모듈로 통합한다(과거 파이썬이 `ollama.generate`/redact 를 재구현하던
//! 중복을 제거 — `ollama.rs` 가 LLM 호출 SSOT).
//!
//! # 설계 원칙
//! - **SRP**: 순수 로직(`redact`/`gate`/`clamp`/`render_note`)과 I/O 쉘(`run`) 분리.
//! - **ROP**: fallible 은 `Result` 레일. LLM/파일 실패는 `?` 전파 → 호출부(serve)가 graceful
//!   boundary 결정. 호스트 훅은 비-200 을 no-op 으로 흡수(세션 종료 절대 미차단).
use std::path::Path;

use anyhow::{Context, Result};
use regex::Regex;

use crate::ollama::Ollama;
use crate::vault::today_utc;

/// 입력 세션 텍스트 상한(문자수). 초과 시 head 1/3 + tail 2/3 로 양끝 보존.
const MAX_CHARS: usize = 40_000;
/// KEEP 판정 후에도 본문이 이보다 짧으면 알맹이 없음으로 보고 폐기.
const MIN_BODY: usize = 40;

/// 증류 시스템 프롬프트 — 첫 줄 KEEP/SKIP 게이트 + 문제해결 서사 틀.
/// (think=false 는 `ollama.rs` 가 고정.)
const SYSTEM: &str = "아래는 사용자가 Claude와 함께 작업한 세션 기록이다. \
미래의 사용자가 '전에 이거 어떻게 했더라'를 다시 참고할 수 있게, \
**문제해결 서사**를 기록해라. 다음 틀로 한국어 markdown 작성:\n\
  🎯 **풀던 문제** — 무엇을 하려 했나 (1줄)\n\
  🧪 **시도/실패** — 시도한 것들, 특히 안 됐던 것과 *왜* 안 됐는지\n\
  🚧 **포기/우회** — 버린 길과 이유 (다음에 또 헛짚지 않게)\n\
  ✅ **통한 해결** — 결국 뭐가 먹혔나 (구체적으로: 명령·설정·근본원인)\n\
  🔄 **미완/다음** — 하다 만 것, 이어서 할 일\n\
해당 없는 항목은 생략. 설정파일 덤프·문서 인용·스키마·일반 잡담은 무시하라 \
(그건 '서사'가 아니다). 실제 시도-실패-해결 흐름이 전혀 없으면 첫 줄에 'SKIP'만.\n\n\
출력 첫 줄은 반드시 'KEEP' 또는 'SKIP' 한 단어. KEEP이면 다음 줄부터 노트 본문.";

/// 시크릿 스크럽 정규식 패턴 — 알려진 토큰 포맷만 매칭. vault(git 추적) 진입 전 누수구 차단.
/// 개인 로컬이라 빡센 redact 불필요 — git/공유 경계 1곳만 막는 가벼운 게이트.
const SECRET_PATTERN: &str = concat!(
    r"(?:xox[baprs]-[0-9A-Za-z-]{10,})",
    r"|(?:xapp-[0-9A-Za-z-]{10,})",
    r"|(?:sk-(?:ant-)?[A-Za-z0-9_-]{20,})",
    r"|(?:AKIA[0-9A-Z]{16})",
    r"|(?:gh[pousr]_[A-Za-z0-9]{30,})",
    r"|(?:AIza[0-9A-Za-z_-]{35})",
    r"|(?:eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})",
    r"|(?:-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    r#"|(?:(?i:api[_-]?key|secret|token|password|passwd|bearer)["' ]*[:=]["' ]*[A-Za-z0-9._/+-]{12,})"#,
);

/// 시크릿 정규식 컴파일 — 경계 1회(ROP: 실패 시 `Err` 전파, 침묵 fallback 없음).
fn build_secret_re() -> Result<Regex> {
    Regex::new(SECRET_PATTERN).context("시크릿 정규식 컴파일 실패")
}

/// 호스트 훅이 추출해 보낸 증류 요청 (origin·phase 는 호스트가 cwd 로 판별 — 엔진은 그대로 신뢰).
pub struct DistillRequest {
    /// 세션 트랜스크립트에서 추출한 user/assistant 평문.
    pub text: String,
    /// 세션 ID — raw 노트 파일명 키(같은 세션 재증류 시 덮어씀).
    pub session_id: String,
    /// `personal` | `company` — 호스트가 `DISTILL_COMPANY_CWD` 로 결정.
    pub origin: String,
    /// `종료`(SessionEnd) | `진행중`(Stop) — 노트 헤더 표기용.
    pub phase: String,
    /// 세션 작업 디렉터리 — 노트 헤더 표기용.
    pub cwd: String,
}

/// 증류 결과. `written=false` 는 KEEP/SKIP 게이트에서 폐기됨(에러 아님).
pub struct DistillOutcome {
    pub written: bool,
    /// 기록된 raw 노트 파일명(`session-….md`). 호스트가 자신의 RAW_DIR 와 조인해 mtime 보정.
    pub filename: Option<String>,
}

/// 시크릿 스크럽 — 알려진 토큰 포맷을 `‹REDACTED›` 로 치환. 순수.
fn redact(re: &Regex, text: &str) -> String {
    re.replace_all(text, "‹REDACTED›").into_owned()
}

/// 첫 줄 KEEP/SKIP 게이트. KEEP이면 본문(2번째 줄~)을, 아니면 None. 순수.
/// KEEP인데 본문이 `MIN_BODY` 미만이면 알맹이 없음으로 None.
fn gate(note: &str) -> Option<String> {
    let mut lines = note.lines();
    let head = lines.next().unwrap_or_default().trim().to_uppercase();
    if !head.starts_with("KEEP") {
        return None;
    }
    let body = lines.collect::<Vec<_>>().join("\n").trim().to_owned();
    (body.chars().count() >= MIN_BODY).then_some(body)
}

/// 길이 클램프 — `MAX_CHARS` 초과 시 head 1/3 + tail 2/3 로 '문제→해결' 양끝 보존. 순수.
fn clamp(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= MAX_CHARS {
        return text.to_owned();
    }
    let head_len = MAX_CHARS / 3;
    let tail_len = MAX_CHARS - head_len;
    let head: String = chars[..head_len].iter().collect();
    let tail: String = chars[chars.len() - tail_len..].iter().collect();
    format!("{head}\n…(중략)…\n{tail}")
}

/// 세션 ID → 파일명 키(영숫자/`_`/`-` 16자). 빈 ID면 날짜 키로 폴백. 순수.
fn note_key(session_id: &str) -> String {
    let filtered: String = session_id
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .take(16)
        .collect();
    if filtered.is_empty() {
        format!("ts-{}", today_utc())
    } else {
        filtered
    }
}

/// raw 노트 .md 내용 렌더 — 파이썬 훅과 바이트 동일(H1 + 출처 blockquote + 본문). 순수.
fn render_note(req: &DistillRequest, body: &str) -> String {
    format!(
        "# 세션 노트 — {date}\n> 자동 증류 (Claude Code · {phase}) · origin: {origin} · cwd: {cwd}\n\n{body}\n",
        date = today_utc(),
        phase = req.phase,
        origin = req.origin,
        cwd = req.cwd,
    )
}

/// 증류 1회 (I/O 쉘) — 클램프 → LLM 증류 → KEEP/SKIP 게이트 → 스크럽 → raw 노트 기록.
/// LLM/파일 실패는 `?` 로 전파(ROP). SKIP/짧음은 에러 아닌 `written=false`.
pub async fn run(
    ollama: &Ollama,
    vault_root: &Path,
    req: &DistillRequest,
) -> Result<DistillOutcome> {
    let text = clamp(&req.text);
    let note = ollama
        .generate(SYSTEM, &format!("=== 세션 ===\n{text}"))
        .await
        .context("distill LLM 생성 실패")?;
    let Some(body) = gate(&note) else {
        return Ok(DistillOutcome {
            written: false,
            filename: None,
        });
    };
    let body = redact(&build_secret_re()?, &body);

    let raw_dir = vault_root.join("raw");
    std::fs::create_dir_all(&raw_dir)
        .with_context(|| format!("raw 디렉터리 생성 실패: {}", raw_dir.display()))?;
    let filename = format!("session-{}.md", note_key(&req.session_id));
    let path = raw_dir.join(&filename);
    std::fs::write(&path, render_note(req, &body))
        .with_context(|| format!("raw 노트 기록 실패: {}", path.display()))?;

    Ok(DistillOutcome {
        written: true,
        filename: Some(filename),
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{MAX_CHARS, build_secret_re, clamp, gate, note_key, redact};

    #[test]
    fn gate_keep_returns_body() {
        let note = "KEEP\n🎯 풀던 문제: 도커 빌드가 캐시 때문에 깨짐\n\
            ✅ 통한 해결: 캐시 레이어 무효화 후 --no-cache 로 재빌드하니 성공함";
        let body = gate(note).expect("KEEP 이면 본문 반환");
        assert!(body.starts_with("🎯 풀던 문제"));
    }

    #[test]
    fn gate_skip_returns_none() {
        assert!(gate("SKIP").is_none());
        assert!(gate("SKIP\n아무 내용").is_none());
    }

    #[test]
    fn gate_keep_but_too_short_is_none() {
        // KEEP 인데 본문 < MIN_BODY → 폐기
        assert!(gate("KEEP\n짧음").is_none());
    }

    #[test]
    fn redact_scrubs_known_tokens() {
        let re = build_secret_re().unwrap();
        let dirty = "토큰: xoxb-1234567890abcdef 그리고 sk-ant-abcdefghij1234567890XYZ 끝";
        let clean = redact(&re, dirty);
        assert!(
            !clean.contains("xoxb-1234567890abcdef"),
            "Slack 토큰 미스크럽: {clean}"
        );
        assert!(!clean.contains("sk-ant-"), "Anthropic 키 미스크럽: {clean}");
        assert!(clean.contains("‹REDACTED›"));
    }

    #[test]
    fn redact_leaves_clean_text() {
        let re = build_secret_re().unwrap();
        let clean = "그냥 평범한 한국어 문장입니다.";
        assert_eq!(redact(&re, clean), clean);
    }

    #[test]
    fn clamp_preserves_short_text() {
        let s = "짧은 텍스트";
        assert_eq!(clamp(s), s);
    }

    #[test]
    fn clamp_keeps_head_and_tail_when_long() {
        let head = "시작".repeat(MAX_CHARS); // > MAX_CHARS chars
        let text = format!("{head}끝부분마커");
        let out = clamp(&text);
        assert!(out.chars().count() <= MAX_CHARS + 16, "클램프 후 길이 초과");
        assert!(out.contains("…(중략)…"), "중략 마커 없음");
        assert!(out.ends_with("끝부분마커"), "tail(해결부) 유실");
    }

    #[test]
    fn note_key_sanitizes_and_truncates() {
        assert_eq!(note_key("abc/def..gh!ij_kl-mn-op-qr"), "abcdefghij_kl-mn");
        assert!(note_key("").starts_with("ts-"));
    }
}
