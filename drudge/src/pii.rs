//! PII / sensitive-data gate for the remember path.
//!
//! Loads shape-based rules from YAML (default: `<vault>/rules/pii.yaml` plus an optional
//! gitignored `<vault>/rules/pii.local.yaml` overlay) and applies them at the single write
//! choke-point (`mcp_remember`). Fail-closed: block rules reject the note, redact rules mask
//! in-place, flag rules persist with a `pii-flag` tag.
//!
//! Design principles borrowed from kb-corpus:
//! - Shape detection, not allowlists (allowlists drift and silently leak).
//! - Company-specific values live in a local overlay, never committed.
//! - The gate runs at write time so every adapter (Claude, Kimi, Codex, hermes, direct MCP)
//!   is subject to the same policy.
use anyhow::{Context, Result};
use fancy_regex::Regex;
use serde::{Deserialize, Serialize};
use std::path::Path;

/// Action applied when a rule matches.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PiiAction {
    /// Never persist; the caller must reject the note.
    Block,
    /// Auto-substitute `replacement` before persisting.
    Redact,
    /// Persist, but raise a review signal (tagged `pii-flag`).
    #[default]
    Flag,
    /// Explicit non-PII carve-out; skipped by block/redact/flag.
    Allow,
}

/// One PII rule as declared in YAML.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PiiRule {
    pub name: String,
    pub regex: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub action: Option<PiiAction>,
    #[serde(default)]
    pub severity: String,
    #[serde(default)]
    pub replacement: Option<String>,
    #[serde(default)]
    pub reason: String,
}

/// Top-level policy defaults.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct PiiPolicy {
    #[serde(default)]
    pub default_action: Option<PiiAction>,
    #[serde(default)]
    pub exemption_marker: Option<String>,
}

/// Raw YAML layout.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct PiiConfigFile {
    #[serde(default)]
    pub version: Option<String>,
    #[serde(default)]
    pub policy: PiiPolicy,
    #[serde(default)]
    pub rules: Vec<PiiRule>,
}

/// A compiled, ready-to-scan rule.
#[derive(Debug, Clone)]
struct CompiledRule {
    name: String,
    regex: Regex,
    action: PiiAction,
    severity: String,
    replacement: Option<String>,
    reason: String,
}

/// Result of scanning a piece of text.
#[derive(Debug, Clone, Default)]
pub struct PiiScan {
    /// Redacted copy of the input (only changed if redact rules matched).
    pub redacted: String,
    /// Number of redact substitutions applied.
    pub redacted_count: usize,
    /// First block match, if any. The caller should reject the note.
    pub block: Option<PiiMatch>,
    /// Flag matches that survived exemption/allow filtering.
    pub flags: Vec<PiiMatch>,
}

/// A single match reported to the caller.
#[derive(Debug, Clone)]
pub struct PiiMatch {
    pub rule: String,
    pub action: PiiAction,
    pub severity: String,
    pub matched: String,
    pub reason: String,
}

/// The loaded PII gate. `None` means the gate is disabled (no rule files found).
#[derive(Debug, Clone)]
pub struct PiiScanner {
    policy: PiiPolicy,
    rules: Vec<CompiledRule>,
}

impl PiiScanner {
    /// Load the base rule file and optional local overlay.
    ///
    /// - If neither file exists, returns `Ok(None)` → gate disabled.
    /// - If the base file is missing but a local file exists, returns an error (local rules
    ///   are meant to overlay a committed base, not to be the only source).
    pub fn load(base: Option<&Path>, local: Option<&Path>) -> Result<Option<Self>> {
        let base_present = base.is_some_and(Path::exists);
        let local_present = local.is_some_and(Path::exists);
        if !base_present && !local_present {
            Ok(None)
        } else {
            if !base_present && local_present {
                let local_path =
                    local.map_or_else(|| "<unknown>".to_owned(), |p| p.display().to_string());
                anyhow::bail!(
                    "PII local overlay found but base rules missing: {local_path} — local overlays must extend a committed base"
                );
            }

            let mut raw = PiiConfigFile::default();
            if let Some(p) = base.filter(|p| p.exists()) {
                raw = Self::read_file(p)?;
            }
            if let Some(p) = local.filter(|p| p.exists()) {
                let overlay = Self::read_file(p)?;
                raw.policy = merge_policy(raw.policy, overlay.policy);
                raw.rules.extend(overlay.rules);
            }

            let default_action = raw.policy.default_action.unwrap_or(PiiAction::Flag);
            let default_severity = "warning".to_owned();

            let mut rules = Vec::with_capacity(raw.rules.len());
            for r in raw.rules {
                let regex = Regex::new(&r.regex).with_context(|| {
                    format!("PII rule '{}' has invalid regex: {}", r.name, r.regex)
                })?;
                let action = r.action.unwrap_or(default_action);
                let severity = if r.severity.is_empty() {
                    default_severity.clone()
                } else {
                    r.severity.clone()
                };
                rules.push(CompiledRule {
                    name: r.name,
                    regex,
                    action,
                    severity,
                    replacement: r.replacement,
                    reason: r.reason,
                });
            }

            Ok(Some(Self {
                policy: raw.policy,
                rules,
            }))
        }
    }

    /// Convenience: load from the conventional vault paths.
    pub fn load_from_vault(vault_dir: &Path) -> Result<Option<Self>> {
        let rules_dir = vault_dir.join("rules");
        let base = rules_dir.join("pii.yaml");
        let local = rules_dir.join("pii.local.yaml");
        Self::load(Some(&base), Some(&local))
    }

    fn read_file(path: &Path) -> Result<PiiConfigFile> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("cannot read PII rules: {}", path.display()))?;
        serde_yaml::from_str(&text)
            .with_context(|| format!("cannot parse PII rules YAML: {}", path.display()))
    }

    /// True if any rules are loaded.
    pub fn is_active(&self) -> bool {
        !self.rules.is_empty()
    }

    /// Scan text and return block/redact/flag results.
    pub fn scan(&self, text: &str) -> PiiScan {
        let mut out = PiiScan {
            redacted: text.to_owned(),
            ..Default::default()
        };

        // 1. Determine allowed spans (Allow rules) and mask them so redact rules never touch them.
        let allowed = self.allowed_spans(text);
        let (masked, allowed_tokens) = mask_spans(text, &allowed);

        // 2. Apply redactions on the masked text.
        let mut redacted = masked;
        for rule in &self.rules {
            if rule.action != PiiAction::Redact {
                continue;
            }
            let replacement = rule.replacement.as_deref().unwrap_or("[REDACTED]");
            out.redacted_count += rule
                .regex
                .find_iter(&redacted)
                .filter_map(std::result::Result::ok)
                .count();
            redacted = rule.regex.replace_all(&redacted, replacement).into_owned();
        }
        out.redacted = restore_spans(&redacted, &allowed_tokens);

        // 3. Detect block/flag matches on the original text, skipping allowed spans and exemptions.
        let exemption_marker = self.policy.exemption_marker.as_deref();
        for rule in &self.rules {
            if out.block.is_some() {
                break;
            }
            if !matches!(rule.action, PiiAction::Block | PiiAction::Flag) {
                continue;
            }
            for m in rule
                .regex
                .find_iter(text)
                .filter_map(std::result::Result::ok)
            {
                if overlaps_any(m.start(), m.end(), &allowed) {
                    continue;
                }
                let matched = text[m.start()..m.end()].to_owned();
                if rule.action == PiiAction::Block {
                    // Block rules ignore exemption markers.
                    out.block = Some(PiiMatch {
                        rule: rule.name.clone(),
                        action: rule.action,
                        severity: rule.severity.clone(),
                        matched,
                        reason: rule.reason.clone(),
                    });
                    break;
                }
                // Flag rules honor exemption markers.
                if let Some(marker) = exemption_marker
                    && line_contains(text, m.start(), marker)
                {
                    continue;
                }
                out.flags.push(PiiMatch {
                    rule: rule.name.clone(),
                    action: rule.action,
                    severity: rule.severity.clone(),
                    matched,
                    reason: rule.reason.clone(),
                });
            }
        }

        out
    }

    fn allowed_spans(&self, text: &str) -> Vec<(usize, usize)> {
        let mut spans = Vec::new();
        for rule in &self.rules {
            if rule.action != PiiAction::Allow {
                continue;
            }
            for m in rule
                .regex
                .find_iter(text)
                .filter_map(std::result::Result::ok)
            {
                spans.push((m.start(), m.end()));
            }
        }
        spans.sort_by_key(|s| s.0);
        // Drop overlaps greedily: keep the earliest span, skip later ones that overlap.
        let mut merged: Vec<(usize, usize)> = Vec::new();
        for (start, end) in spans {
            if let Some(last) = merged.last_mut()
                && start <= last.1
            {
                last.1 = last.1.max(end);
                continue;
            }
            merged.push((start, end));
        }
        merged
    }
}

fn merge_policy(mut base: PiiPolicy, overlay: PiiPolicy) -> PiiPolicy {
    if overlay.default_action.is_some() {
        base.default_action = overlay.default_action;
    }
    if overlay.exemption_marker.is_some() {
        base.exemption_marker = overlay.exemption_marker;
    }
    base
}

#[derive(Debug, Clone)]
struct AllowedToken {
    placeholder: String,
    original: String,
}

fn mask_spans(text: &str, spans: &[(usize, usize)]) -> (String, Vec<AllowedToken>) {
    if spans.is_empty() {
        (text.to_owned(), Vec::new())
    } else {
        let mut out = String::with_capacity(text.len());
        let mut tokens = Vec::with_capacity(spans.len());
        let mut cursor = 0;
        for (idx, (start, end)) in spans.iter().enumerate() {
            out.push_str(&text[cursor..*start]);
            let placeholder = format!("\u{E000}ALLOW{idx}\u{E000}");
            let original = text[*start..*end].to_owned();
            out.push_str(&placeholder);
            tokens.push(AllowedToken {
                placeholder,
                original,
            });
            cursor = *end;
        }
        out.push_str(&text[cursor..]);
        (out, tokens)
    }
}

fn restore_spans(text: &str, tokens: &[AllowedToken]) -> String {
    let mut out = text.to_owned();
    for t in tokens {
        out = out.replace(&t.placeholder, &t.original);
    }
    out
}

fn overlaps_any(start: usize, end: usize, spans: &[(usize, usize)]) -> bool {
    spans.iter().any(|(s, e)| start < *e && end > *s)
}

fn line_contains(text: &str, offset: usize, needle: &str) -> bool {
    let line_start = text[..offset].rfind('\n').map_or(0, |i| i + 1);
    let line_end = text[offset..].find('\n').map_or(text.len(), |i| offset + i);
    text[line_start..line_end].contains(needle)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used, clippy::panic, clippy::unwrap_used)]

    use super::*;

    fn scanner() -> PiiScanner {
        // Static test fixture; panics here are test bugs, not production errors.
        #[allow(clippy::expect_used, clippy::panic)]
        fn build() -> PiiScanner {
            let yaml = r#"
version: "1.0"
policy:
  exemption_marker: "<!-- pii-allow -->"
rules:
  - name: email
    regex: '(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b'
    action: redact
    replacement: "[EMAIL]"
    severity: warning
    reason: email
  - name: phone
    regex: '\b01[0-9][-\s]?\d{3,4}[-\s]?\d{4}\b'
    action: redact
    replacement: "[PHONE]"
    severity: warning
    reason: phone
  - name: rrn
    regex: '\b\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-[1-4]\d{6}\b'
    action: block
    severity: critical
    reason: rrn
  - name: ticket
    regex: '\b[A-Z]{2,5}-\d+\b'
    action: flag
    severity: warning
    reason: ticket
"#;
            let file: PiiConfigFile = serde_yaml::from_str(yaml).expect("test PII YAML is valid");
            let rules = file
                .rules
                .into_iter()
                .map(|r| CompiledRule {
                    name: r.name,
                    regex: Regex::new(&r.regex)
                        .unwrap_or_else(|_| panic!("test PII regex '{}' is valid", r.regex)),
                    action: r.action.unwrap_or(PiiAction::Flag),
                    severity: r.severity,
                    replacement: r.replacement,
                    reason: r.reason,
                })
                .collect();
            PiiScanner {
                policy: file.policy,
                rules,
            }
        }
        build()
    }

    fn load_scanner(base_yaml: &str, local_yaml: Option<&str>) -> PiiScanner {
        let tmp = tempfile::tempdir().unwrap();
        let base = tmp.path().join("pii.yaml");
        std::fs::write(&base, base_yaml).unwrap();
        let local = tmp.path().join("pii.local.yaml");
        let local_ref = if let Some(yaml) = local_yaml {
            std::fs::write(&local, yaml).unwrap();
            Some(local.as_path())
        } else {
            None
        };
        PiiScanner::load(Some(&base), local_ref).unwrap().unwrap()
    }

    #[test]
    fn redacts_email_and_phone() {
        let s = scanner();
        let out = s.scan("contact me at foo@example.com or 010-1234-5678");
        assert!(out.block.is_none());
        assert_eq!(out.redacted, "contact me at [EMAIL] or [PHONE]");
        assert!(out.redacted_count >= 2);
    }

    #[test]
    fn blocks_rrn() {
        let s = scanner();
        let out = s.scan("ssn 900101-1234567 here");
        assert!(out.block.as_ref().is_some_and(|b| b.rule == "rrn"));
    }

    #[test]
    fn flags_ticket() {
        let s = scanner();
        let out = s.scan("see FDS-12345 for context");
        assert!(out.block.is_none());
        assert_eq!(out.flags.len(), 1);
        assert_eq!(out.flags[0].rule, "ticket");
    }

    #[test]
    fn exemption_marker_skips_flag() {
        let s = scanner();
        let out = s.scan("see FDS-12345 <!-- pii-allow --> for context");
        assert!(out.block.is_none());
        assert!(out.flags.is_empty());
    }

    #[test]
    fn default_action_only_applies_when_rule_action_is_omitted() {
        let s = load_scanner(
            r#"
version: "1.0"
policy:
  default_action: block
rules:
  - name: explicit-ticket
    regex: '\bABC-\d+\b'
    action: flag
    reason: explicit flag must survive
  - name: implicit-secret
    regex: '\bSECRET-\d+\b'
    reason: omitted action inherits the policy default
"#,
            None,
        );

        let explicit = s.scan("ABC-123");
        assert!(explicit.block.is_none());
        assert_eq!(explicit.flags.len(), 1);
        assert_eq!(explicit.flags[0].rule, "explicit-ticket");

        let implicit = s.scan("SECRET-123");
        assert!(
            implicit
                .block
                .as_ref()
                .is_some_and(|b| b.rule == "implicit-secret")
        );
    }

    #[test]
    fn local_overlay_merges_policy_without_erasing_base_marker() {
        let s = load_scanner(
            r#"
version: "1.0"
policy:
  default_action: flag
  exemption_marker: "<!-- pii-allow:"
rules:
  - name: ticket
    regex: '\bABC-\d+\b'
    action: flag
    reason: ticket id
"#,
            Some(
                r#"
version: "1.0"
policy:
  default_action: flag
rules: []
"#,
            ),
        );

        let out = s.scan("ABC-123 <!-- pii-allow: public ticket -->");
        assert!(out.block.is_none());
        assert!(out.flags.is_empty());
    }

    #[test]
    fn absent_local_overlay_does_not_fail_load() {
        let tmp = tempfile::tempdir().unwrap();
        let base = tmp.path().join("pii.yaml");
        let local = tmp.path().join("pii.local.yaml");
        std::fs::write(
            &base,
            r#"
version: "1.0"
rules:
  - name: ticket
    regex: '\bABC-\d+\b'
    action: flag
    reason: ticket id
"#,
        )
        .unwrap();

        let scanner = PiiScanner::load(Some(&base), Some(&local)).unwrap();
        assert!(scanner.is_some());
    }

    #[test]
    fn unknown_policy_key_fails_closed() {
        let tmp = tempfile::tempdir().unwrap();
        let base = tmp.path().join("pii.yaml");
        std::fs::write(
            &base,
            r#"
version: "1.0"
policy:
  scan_scope: [wiki, raw, dist]
rules: []
"#,
        )
        .unwrap();

        let err = PiiScanner::load(Some(&base), None).unwrap_err();
        assert!(format!("{err:#}").contains("unknown field"));
    }
}
