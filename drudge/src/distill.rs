//! Session distill — distill Claude Code session text into a problem-solving narrative and record it as a vault/raw note.
//!
//! # Boundary separation (SSOT)
//! The host hook (`hooks/distill-session.py`) does only *host-specific* work: reading transcripts, text
//! extraction, throttle markers, session mtime correction. LLM distillation · the KEEP/SKIP gate · secret scrubbing · raw note
//! formatting are all consolidated into this engine module (removing the duplication where the old Python
//! reimplemented `llm.generate`/redact — `llm.rs` is the LLM-call SSOT).
//!
//! # Design principles
//! - **SRP**: separate pure logic (`redact`/`gate`/`clamp`/`render_note`) from the I/O shell (`run`).
//! - **ROP**: fallible goes on the `Result` rail. LLM/file failures propagate via `?` → the caller (serve) decides the graceful
//!   boundary. The host hook absorbs non-200 as a no-op (never blocks session termination).
use std::path::Path;

use anyhow::{Context, Result};
use regex::Regex;

use crate::llm::Llm;
use crate::vault::today_utc;

/// Upper bound on input session text (char count). When exceeded, preserve both ends via head 1/3 + tail 2/3.
const MAX_CHARS: usize = 40_000;
/// If the body is shorter than this even after a KEEP verdict, treat it as having no substance and discard.
const MIN_BODY: usize = 40;

/// Distillation system prompt — first-line KEEP/SKIP gate + problem-solving narrative frame.
/// (think=false is fixed by `llm.rs`.)
const SYSTEM: &str = "Below is a record of a session the user worked through with Claude. \
So that the future user can look back at 'how did I do this before', \
record a **problem-solving narrative**. Write markdown in the same language as the session, using this frame:\n\
  🎯 **Problem** — what were they trying to do (1 line)\n\
  🧪 **Attempts/failures** — what was tried, especially what did NOT work and *why*\n\
  🚧 **Abandoned/worked around** — paths dropped and why (so the next attempt doesn't repeat the dead end)\n\
  ✅ **Working solution** — what ultimately worked (concretely: commands, config, root cause)\n\
  🔄 **Unfinished/next** — what was left half-done, what to do next\n\
Omit any item that does not apply. Ignore config-file dumps, doc quotes, schemas, and general chit-chat \
(those are not a 'narrative'). If there is no real attempt-failure-solution flow at all, output only 'SKIP' on the first line.\n\n\
The first output line MUST be the single word 'KEEP' or 'SKIP'. If KEEP, the note body follows from the next line.";

/// Secret-scrub regex pattern — matches only known token formats. Closes the leak path before entering the vault (git-tracked).
/// Being personal/local, heavy redaction isn't needed — a lightweight gate guarding just the single git/sharing boundary.
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

/// Compile the secret regex — once at the boundary (ROP: on failure propagate `Err`, no silent fallback).
fn build_secret_re() -> Result<Regex> {
    Regex::new(SECRET_PATTERN).context("failed to compile secret regex")
}

/// Distillation request extracted and sent by the host hook (origin·phase are determined by the host from cwd — the engine trusts them as-is).
pub struct DistillRequest {
    /// user/assistant plain text extracted from the session transcript.
    pub text: String,
    /// Session ID — the raw note filename key (overwritten when the same session is re-distilled).
    pub session_id: String,
    /// `personal` | `company` — determined by the host via `DISTILL_COMPANY_CWD`.
    pub origin: String,
    /// `final`(SessionEnd) | `in-progress`(Stop) — for the note header label.
    pub phase: String,
    /// repo slug — filled by the host from the cwd git remote (fallback: folder name). May be empty.
    /// Embedded in the note header as a `repo:` marker so compile categorizes it under the `repo/<slug>` tag.
    pub repo: String,
    /// Session working directory — for the note header label.
    pub cwd: String,
}

/// Distillation result. `written=false` means it was discarded at the KEEP/SKIP gate (not an error).
pub struct DistillOutcome {
    pub written: bool,
    /// The recorded raw note filename (`session-….md`). The host joins it with its own RAW_DIR for mtime correction.
    pub filename: Option<String>,
}

/// Secret scrub — replace known token formats with `‹REDACTED›`. Pure.
fn redact(re: &Regex, text: &str) -> String {
    re.replace_all(text, "‹REDACTED›").into_owned()
}

/// First-line KEEP/SKIP gate. On KEEP returns the body (from line 2 on), otherwise None. Pure.
/// If KEEP but the body is under `MIN_BODY`, returns None as having no substance.
fn gate(note: &str) -> Option<String> {
    let mut lines = note.lines();
    let head = lines.next().unwrap_or_default().trim().to_uppercase();
    if !head.starts_with("KEEP") {
        return None;
    }
    let body = lines.collect::<Vec<_>>().join("\n").trim().to_owned();
    (body.chars().count() >= MIN_BODY).then_some(body)
}

/// Length clamp — when over `MAX_CHARS`, preserve both 'problem→solution' ends via head 1/3 + tail 2/3. Pure.
fn clamp(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= MAX_CHARS {
        return text.to_owned();
    }
    let head_len = MAX_CHARS / 3;
    let tail_len = MAX_CHARS - head_len;
    let head: String = chars[..head_len].iter().collect();
    let tail: String = chars[chars.len() - tail_len..].iter().collect();
    format!("{head}\n…(snip)…\n{tail}")
}

/// Session ID → filename key (alphanumeric/`_`/`-`, 16 chars). On empty ID, falls back to a date key. Pure.
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

/// Render raw note .md content (pure). H1 + source blockquote + body.
/// If repo is present, embed a `repo:` marker in the header → compile categorizes under the `repo/<slug>` tag.
fn render_note(req: &DistillRequest, body: &str) -> String {
    let repo_seg = if req.repo.is_empty() {
        String::new()
    } else {
        format!("repo: {} · ", req.repo)
    };
    format!(
        "# Session Note — {date}\n> auto-distilled (Claude Code · {phase}) · origin: {origin} · {repo_seg}cwd: {cwd}\n\n{body}\n",
        date = today_utc(),
        phase = req.phase,
        origin = req.origin,
        cwd = req.cwd,
    )
}

/// One distillation pass (I/O shell) — clamp → LLM distill → KEEP/SKIP gate → scrub → write raw note.
/// LLM/file failures propagate via `?` (ROP). SKIP/too-short is `written=false`, not an error.
pub async fn run(llm: &Llm, vault_root: &Path, req: &DistillRequest) -> Result<DistillOutcome> {
    let text = clamp(&req.text);
    let note = llm
        .generate(SYSTEM, &format!("=== session ===\n{text}"))
        .await
        .context("distill LLM generation failed")?;
    let Some(body) = gate(&note) else {
        return Ok(DistillOutcome {
            written: false,
            filename: None,
        });
    };
    let body = redact(&build_secret_re()?, &body);

    let raw_dir = vault_root.join("raw");
    std::fs::create_dir_all(&raw_dir)
        .with_context(|| format!("failed to create raw directory: {}", raw_dir.display()))?;
    let filename = format!("session-{}.md", note_key(&req.session_id));
    let path = raw_dir.join(&filename);
    std::fs::write(&path, render_note(req, &body))
        .with_context(|| format!("failed to write raw note: {}", path.display()))?;

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
        let note = "KEEP\n🎯 Problem: docker build kept breaking due to cache\n\
            ✅ Working solution: invalidated the cache layer and rebuilt with --no-cache, which succeeded";
        let body = gate(note).expect("KEEP should return the body");
        assert!(body.starts_with("🎯 Problem"));
    }

    #[test]
    fn gate_skip_returns_none() {
        assert!(gate("SKIP").is_none());
        assert!(gate("SKIP\nsome content").is_none());
    }

    #[test]
    fn gate_keep_but_too_short_is_none() {
        // KEEP but body < MIN_BODY → discard
        assert!(gate("KEEP\nshort").is_none());
    }

    #[test]
    fn redact_scrubs_known_tokens() {
        let re = build_secret_re().unwrap();
        let dirty = "token: xoxb-1234567890abcdef and sk-ant-abcdefghij1234567890XYZ end";
        let clean = redact(&re, dirty);
        assert!(
            !clean.contains("xoxb-1234567890abcdef"),
            "Slack token not scrubbed: {clean}"
        );
        assert!(
            !clean.contains("sk-ant-"),
            "Anthropic key not scrubbed: {clean}"
        );
        assert!(clean.contains("‹REDACTED›"));
    }

    #[test]
    fn redact_leaves_clean_text() {
        let re = build_secret_re().unwrap();
        let clean = "Just an ordinary plain sentence.";
        assert_eq!(redact(&re, clean), clean);
    }

    #[test]
    fn clamp_preserves_short_text() {
        let s = "short text";
        assert_eq!(clamp(s), s);
    }

    #[test]
    fn clamp_keeps_head_and_tail_when_long() {
        let head = "start".repeat(MAX_CHARS); // > MAX_CHARS chars
        let text = format!("{head}END-MARKER");
        let out = clamp(&text);
        assert!(
            out.chars().count() <= MAX_CHARS + 16,
            "length exceeded after clamp"
        );
        assert!(out.contains("…(snip)…"), "snip marker missing");
        assert!(out.ends_with("END-MARKER"), "tail (solution part) lost");
    }

    #[test]
    fn note_key_sanitizes_and_truncates() {
        assert_eq!(note_key("abc/def..gh!ij_kl-mn-op-qr"), "abcdefghij_kl-mn");
        assert!(note_key("").starts_with("ts-"));
    }
}
