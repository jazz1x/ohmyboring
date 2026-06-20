//! Secret scrub — the single leak-boundary guard before content enters the git-tracked vault.
//!
//! The agent (reasoner) curates notes and is the first line of defense, but `remember` writes into a
//! git-tracked vault — a real leak boundary. A lightweight regex scrub here closes the one sharing/git
//! boundary (not symptom-masking: the root cause is "arbitrary text may carry a token", outside our
//! control → the write boundary is the SSOT to normalize it). Being personal/local, heavy redaction
//! isn't needed.
use std::sync::LazyLock;

use anyhow::Result;
use regex::Regex;

/// Secret-scrub regex pattern — matches only known token formats.
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

static SECRET_RE: LazyLock<Result<Regex, regex::Error>> =
    LazyLock::new(|| Regex::new(SECRET_PATTERN));

/// Return the compiled secret regex, compiling it once at process startup.
///
/// The pattern is a static constant; compilation can only fail if the pattern itself is malformed.
/// Caching the compiled `Regex` with `LazyLock` means every `remember` call reuses the same matcher
/// instead of re-parsing the regex — a high-leverage single boundary (Layer 3: block the waste with
/// the least structure).
pub fn build_secret_re() -> Result<&'static Regex> {
    SECRET_RE
        .as_ref()
        .map_err(|e| anyhow::anyhow!("failed to compile secret regex: {e}"))
}

/// Secret scrub — replace known token formats with `‹REDACTED›`. Pure.
pub fn redact(re: &Regex, text: &str) -> String {
    re.replace_all(text, "‹REDACTED›").into_owned()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{build_secret_re, redact};

    #[test]
    fn redact_scrubs_known_tokens() {
        let re = build_secret_re().unwrap();
        let dirty = "token: xoxb-1234567890abcdef and sk-ant-abcdefghij1234567890XYZ end";
        let clean = redact(re, dirty);
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
        assert_eq!(redact(re, clean), clean);
    }

    #[test]
    fn build_secret_re_is_cached() {
        let r1 = build_secret_re().unwrap();
        let r2 = build_secret_re().unwrap();
        // Both calls return the same static allocation.
        assert!(std::ptr::eq(r1, r2));
    }
}
