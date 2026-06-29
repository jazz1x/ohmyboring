//! Curate and persist wiki notes from an LLM-produced session summary.
//!
//! This module owns the "write" side of the vault: id allocation, YAML frontmatter
//! normalization, body formatting, and the optional frontmatter repair pass.
//! Cross-reference: design decision D3 (gated write), D7 (vault SSOT).

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::frontmatter::FrontMatter;

/// Normalize into an Obsidian-safe tag (pure). Space/disallowed chars → `-`, collapse dashes, trim,
/// lowercase. Allowed set = `[a-z0-9_/-]` (`/` = nested tag). Empty · pure-number → `None`.
pub fn sanitize_tag(raw: &str) -> Option<String> {
    let mut out = String::with_capacity(raw.len());
    let mut prev_dash = false;
    for c in raw.trim().to_lowercase().chars() {
        let mapped = if c.is_ascii_alphanumeric() || c == '_' || c == '/' {
            c
        } else {
            '-'
        };
        if mapped == '-' {
            if prev_dash {
                continue;
            }
            prev_dash = true;
        } else {
            prev_dash = false;
        }
        out.push(mapped);
    }
    let trimmed = out.trim_matches(|c| c == '-' || c == '/').to_owned();
    if trimmed.is_empty() || trimmed.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some(trimmed)
}

/// True if `line` is an ATX markdown heading (`#`..`######` then a space, e.g. `## 남은 일`). Pure.
/// Requires the space so a `#tag` / `#59` reference (which is body content, not a section header) is
/// never mistaken for a heading.
fn is_atx_heading(line: &str) -> bool {
    let hashes = line.chars().take_while(|&c| c == '#').count();
    (1..=6).contains(&hashes) && line[hashes..].starts_with(' ')
}

/// Drop a trailing heading that has no content beneath it (pure). The remember prompt says omit a
/// section when it has nothing, but gemma sometimes still emits the bare header (e.g. a final
/// `## 남은 일` with no body under it, wiki-class). Strip from the end: while the last non-blank line
/// is an ATX heading, remove it (and the blank lines that follow). Loops so two stacked empty sections
/// (`## A` then an empty `## B`) both go. A heading WITH content beneath it is kept untouched.
pub(crate) fn strip_trailing_empty_heading(body: &str) -> String {
    let mut lines: Vec<&str> = body.lines().collect();
    loop {
        // peel trailing blank lines so the empty section's own blank padding doesn't count as "content"
        while lines.last().is_some_and(|l| l.trim().is_empty()) {
            lines.pop();
        }
        match lines.last() {
            Some(last) if is_atx_heading(last.trim_start()) => {
                lines.pop();
            }
            _ => break,
        }
    }
    lines.join("\n")
}

/// Decode an LLM-produced note body into clean markdown — the SSOT normalization at the write gate.
/// Local models (e.g. gemma) sometimes emit JSON-string-escaped text: the two characters backslash-n
/// instead of a real line break, and stray backslash-escapes before markdown punctuation (`` \` ``,
/// `\#`, `\"`). Decoding HERE means every writer — the SessionEnd hook, the hermes cron agent, a direct
/// MCP `remember`, `make remember` — stores real markdown, instead of relying on one adapter's
/// best-effort patch (the duplication that kept regressing). Unknown escapes are kept verbatim so a
/// genuine backslash (a path, a regex) is never harmed. Finally, a trailing empty section header the
/// model left behind (the omit-if-none case gemma misses) is stripped — same SSOT, every writer.
pub fn normalize_body(body: &str) -> String {
    let mut out = String::with_capacity(body.len());
    let mut chars = body.chars();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('n') => out.push('\n'),
            Some('t') => out.push('\t'),
            Some('r') => out.push('\r'),
            // stray markdown-punctuation escapes the model over-emits → restore the literal char
            Some(p @ ('`' | '#' | '"' | '*' | '_' | '[' | ']' | '(' | ')' | '\\')) => out.push(p),
            // unknown escape → keep both chars verbatim (don't harm real backslashes)
            Some(other) => {
                out.push('\\');
                out.push(other);
            }
            None => out.push('\\'),
        }
    }
    strip_trailing_empty_heading(out.trim()).trim().to_owned()
}

/// Scan vault/wiki (and optional DB-derived ids) for the next `wiki-NNNN` id.
/// Does NOT fill gaps: ids are monotonically increasing so a number, once used,
/// never refers to a different note later. `wiki-0000` is reserved for the seed note.
fn next_wiki_id(wiki_dir: &Path, db_ids: Option<&HashSet<u32>>) -> Result<u32> {
    let mut used: HashSet<u32> = HashSet::new();
    if wiki_dir.exists() {
        for entry in std::fs::read_dir(wiki_dir)
            .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?
        {
            let path = entry?.path();
            if let Some(n) = path
                .file_stem()
                .and_then(|s| s.to_str())
                .and_then(|s| s.strip_prefix("wiki-"))
                .and_then(|s| s.parse::<u32>().ok())
            {
                used.insert(n);
            }
        }
    }
    if let Some(db) = db_ids {
        used.extend(db);
    }
    let max = used.iter().copied().max().unwrap_or(0);
    Ok(max + 1)
}

/// Atomically allocate the next `wiki-NNNN.md` path in `wiki_dir`.
///
/// Uses `O_CREAT | O_EXCL` so concurrent `remember` calls cannot allocate the
/// same id. On collision (another caller won the race) we retry with the next
/// integer until we successfully create a fresh file. The returned path points
/// to an empty file that the caller can write into.
///
/// `db_ids` optionally contains ids already persisted in the vector store, so
/// a new note never reuses a source_path that still exists in Postgres even if
/// its wiki file is temporarily missing.
pub fn allocate_wiki_path(
    wiki_dir: &Path,
    db_ids: Option<&HashSet<u32>>,
) -> Result<(String, PathBuf)> {
    std::fs::create_dir_all(wiki_dir)
        .with_context(|| format!("failed to create wiki dir: {}", wiki_dir.display()))?;
    let mut n = next_wiki_id(wiki_dir, db_ids)?;
    loop {
        let id = format!("wiki-{n:04}");
        let path = wiki_dir.join(format!("{id}.md"));
        match std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&path)
        {
            Ok(file) => {
                drop(file);
                return Ok((id, path));
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => n += 1,
            Err(e) => {
                return Err(anyhow::Error::new(e)
                    .context(format!("failed to allocate wiki path: {}", path.display())));
            }
        }
    }
}

/// Render a remember-note into wiki `.md` content (pure). Frontmatter satisfies the lint schema
/// (id·title·kind·origin·date) AND carries the agent-curated semantic fields (tags·tools·concepts·claims)
/// so a disk re-ingest rebuilds the same graph deterministically. `relates_to` starts `[]` and is filled
/// by `project_links` from the graph (SSOT for relations).
pub fn render_wiki_note(wiki_id: &str, front: &FrontMatter, body: &str) -> Result<String> {
    use serde::Serialize;

    #[derive(Serialize)]
    struct Fm<'a> {
        id: &'a str,
        title: &'a str,
        kind: &'a str,
        origin: &'a str,
        project: &'a str,
        date: &'a str,
        tags: &'a [String],
        tools: &'a [String],
        concepts: &'a [String],
        claims: &'a [crate::frontmatter::Claim],
        relates_to: Vec<String>,
        sources: Vec<String>,
        /// Session provenance: which session produced this note. Persisted when present so a note
        /// can be traced back to (and deduped against) its originating session. Absent on legacy
        /// notes and manual remembers → skipped, keeping their frontmatter unchanged.
        #[serde(skip_serializing_if = "Option::is_none")]
        omb_session_id: Option<&'a str>,
    }
    let title = front.title.as_deref().unwrap_or(wiki_id);
    let kind = if front.kind.is_empty() {
        "note"
    } else {
        front.kind.as_str()
    };
    let fm = Fm {
        id: wiki_id,
        title,
        kind,
        origin: &front.origin,
        project: &front.project,
        date: &front.date,
        tags: &front.tags,
        tools: &front.tools,
        concepts: &front.concepts,
        claims: &front.claims,
        relates_to: Vec::new(),
        sources: Vec::new(),
        omb_session_id: front.omb_session_id.as_deref(),
    };
    let yaml = serde_yaml::to_string(&fm).context("failed to serialize wiki frontmatter YAML")?;
    Ok(format!("---\n{yaml}---\n{}\n", body.trim_end()))
}

/// Today's date "YYYY-MM-DD" — the SSOT default for a remember note's `date`.
/// I/O boundary: `SystemTime::now()` is isolated to this single spot (shared by main·serve) → conversion is the pure `days_to_date`.
#[must_use]
pub fn today_utc() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let days = (secs / 86_400).cast_signed();
    crate::vault::days_to_date(days)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::{normalize_body, render_wiki_note, sanitize_tag};
    use crate::frontmatter::FrontMatter;
    use crate::vault::split_frontmatter;

    // ── normalize_body (LLM JSON-escape decode at the write gate) ──

    #[test]
    fn normalize_body_decodes_literal_newline() {
        assert_eq!(
            normalize_body("### A\\n1. x\\n\\n### B\\n2. y"),
            "### A\n1. x\n\n### B\n2. y"
        );
    }

    #[test]
    fn normalize_body_unescapes_stray_markdown_punctuation() {
        assert_eq!(
            normalize_body("use \\`corpus_status\\`"),
            "use `corpus_status`"
        );
        assert_eq!(
            normalize_body("fix \\#59 \\\"claim\\\""),
            "fix #59 \"claim\""
        );
    }

    #[test]
    fn normalize_body_keeps_genuine_backslashes() {
        assert_eq!(
            normalize_body("path C:\\Users and re \\d+"),
            "path C:\\Users and re \\d+"
        );
    }

    #[test]
    fn normalize_body_leaves_clean_markdown_unchanged() {
        let clean = "## 배경\n결정함.\n\n## 결과\n- 끝";
        assert_eq!(normalize_body(clean), clean);
    }

    #[test]
    fn normalize_body_strips_bare_trailing_heading() {
        assert_eq!(
            normalize_body("## 배경\n결정함.\n\n## 남은 일\n"),
            "## 배경\n결정함."
        );
    }

    #[test]
    fn normalize_body_strips_stacked_empty_trailing_headings() {
        assert_eq!(
            normalize_body("## 결과\n- 끝\n\n## 남은 일\n\n## 다음 단계\n"),
            "## 결과\n- 끝"
        );
    }

    #[test]
    fn normalize_body_keeps_heading_with_content() {
        let kept = "## 배경\n결정함.\n\n## 남은 일\n- 후속 PR";
        assert_eq!(normalize_body(kept), kept);
    }

    #[test]
    fn normalize_body_keeps_hashtag_reference() {
        assert_eq!(
            normalize_body("작업 요약.\n\n관련 #59"),
            "작업 요약.\n\n관련 #59"
        );
    }

    #[test]
    fn normalize_body_strips_trailing_heading_after_escape_decode() {
        assert_eq!(
            normalize_body("## 배경\\n결정함.\\n\\n## 남은 일"),
            "## 배경\n결정함."
        );
    }

    // ── sanitize_tag (Obsidian-safe normalization) ──

    #[test]
    fn sanitize_tag_space_to_hyphen() {
        assert_eq!(sanitize_tag("claude code").as_deref(), Some("claude-code"));
        assert_eq!(
            sanitize_tag("data management").as_deref(),
            Some("data-management")
        );
        assert_eq!(
            sanitize_tag("session hook").as_deref(),
            Some("session-hook")
        );
    }

    #[test]
    fn sanitize_tag_keeps_valid() {
        assert_eq!(sanitize_tag("rag").as_deref(), Some("rag"));
        assert_eq!(sanitize_tag("pre-commit").as_deref(), Some("pre-commit"));
        assert_eq!(
            sanitize_tag("repo/oh-my-boring").as_deref(),
            Some("repo/oh-my-boring")
        );
    }

    #[test]
    fn sanitize_tag_strips_and_collapses() {
        assert_eq!(
            sanitize_tag("  Rust!! Style  ").as_deref(),
            Some("rust-style")
        );
        assert_eq!(
            sanitize_tag("-leading-trailing-").as_deref(),
            Some("leading-trailing")
        );
    }

    // ── render_wiki_note: session provenance persistence ──

    #[test]
    fn render_persists_session_id_when_present_and_omits_when_absent() {
        let mut front = FrontMatter {
            origin: "personal".to_owned(),
            project: "olympus".to_owned(),
            date: "2026-06-22".to_owned(),
            kind: "note".to_owned(),
            ..Default::default()
        };

        // absent → field must not appear (legacy/manual notes stay clean)
        let out = render_wiki_note("wiki-0042", &front, "body").unwrap();
        assert!(!out.contains("omb_session_id"), "{out}");

        // present → persisted as provenance, and the note round-trips through YAML parse
        front.omb_session_id = Some("sess-abc123".to_owned());
        let out = render_wiki_note("wiki-0042", &front, "body").unwrap();
        assert!(out.contains("omb_session_id: sess-abc123"), "{out}");
        let (yaml, _) = split_frontmatter(&out).expect("frontmatter splits");
        let parsed: serde_yaml::Value = serde_yaml::from_str(yaml).expect("valid YAML");
        assert_eq!(
            parsed["omb_session_id"],
            serde_yaml::Value::from("sess-abc123")
        );
    }

    // ── next_wiki_id ──

    #[test]
    fn next_wiki_id_uses_max_plus_one() {
        let tmp = tempfile::tempdir().unwrap();
        let wiki = tmp.path().join("wiki");
        std::fs::create_dir(&wiki).unwrap();
        std::fs::File::create(wiki.join("wiki-0000.md")).unwrap();
        std::fs::File::create(wiki.join("wiki-0002.md")).unwrap();
        std::fs::File::create(wiki.join("wiki-0005.md")).unwrap();
        // monotonic: do not reuse the gaps at 1, 3, 4.
        assert_eq!(super::next_wiki_id(&wiki, None).unwrap(), 6);
        std::fs::File::create(wiki.join("wiki-0006.md")).unwrap();
        assert_eq!(super::next_wiki_id(&wiki, None).unwrap(), 7);
    }

    #[test]
    fn next_wiki_id_considers_db_ids() {
        let tmp = tempfile::tempdir().unwrap();
        let wiki = tmp.path().join("wiki");
        std::fs::create_dir(&wiki).unwrap();
        std::fs::File::create(wiki.join("wiki-0001.md")).unwrap();
        std::fs::File::create(wiki.join("wiki-0002.md")).unwrap();
        let db_ids: std::collections::HashSet<u32> = [5, 7].into_iter().collect();
        // max is 7 (from DB), so next is 8 even though files only go up to 2.
        assert_eq!(super::next_wiki_id(&wiki, Some(&db_ids)).unwrap(), 8);
    }
}
