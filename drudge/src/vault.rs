//! Vault lint / audit — integrity checks for the personal Obsidian markdown KB.
//!
//! # Design principles (PRINCIPLES.md)
//! - **PDV**: parse `schema.yaml` and `.md` frontmatter into a typed form once at the boundary.
//! - **ROP**: `?` propagation + anyhow Context. No unwrap/expect/panic.
//! - **ADT**: `Kind`, `Origin`, `Severity` as enums. Make impossible states unrepresentable.
//! - **SRP**: separate pure logic (parse/graph) from I/O (file reading).
//!   - `split_frontmatter`, `parse_*`, `lint_*`, `audit_*`: pure — &str/slice input → value
//!   - `run_lint` / `run_audit`: I/O shell — collect files then delegate to pure functions

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;
use serde::Serialize;

use crate::store::Store;

// ─────────────────────────────────────────────────────────────
// ADT — make impossible states unrepresentable
// ─────────────────────────────────────────────────────────────

/// Allowed values for page kind.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Note,
    Memory,
    Session,
    Decision,
}

/// Allowed values for page origin.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Origin {
    Personal,
    Company,
}

/// Issue severity.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Error,
    Warn,
}

// ─────────────────────────────────────────────────────────────
// Domain types (typed evidence — PDV)
// ─────────────────────────────────────────────────────────────

/// A lint/audit result issue. A pure value decoupled from I/O.
#[derive(Debug, Clone)]
pub struct Issue {
    pub rule: &'static str,
    pub severity: Severity,
    pub target: String,
    pub message: String,
}

impl Issue {
    fn error(rule: &'static str, target: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            rule,
            severity: Severity::Error,
            target: target.into(),
            message: message.into(),
        }
    }

    fn warn(rule: &'static str, target: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            rule,
            severity: Severity::Warn,
            target: target.into(),
            message: message.into(),
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Schema parsing (PDV: typed parse once at the boundary)
// ─────────────────────────────────────────────────────────────

/// Typed representation of `.rules/schema.yaml`.
#[derive(Debug, Deserialize)]
pub struct Schema {
    pub page_id: PageIdSchema,
    pub sources: SourcesSchema,
    pub required_frontmatter: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct PageIdSchema {
    pub pattern: String,
}

#[derive(Debug, Deserialize)]
pub struct SourcesSchema {
    pub allowed_prefixes: Vec<String>,
}

/// Read the schema from a file and parse it into a typed value. (I/O boundary)
pub fn load_schema(schema_path: &Path) -> Result<Schema> {
    let raw = std::fs::read_to_string(schema_path)
        .with_context(|| format!("failed to read schema file: {}", schema_path.display()))?;
    serde_yaml::from_str(&raw)
        .with_context(|| format!("failed to parse schema YAML: {}", schema_path.display()))
}

// ─────────────────────────────────────────────────────────────
// Frontmatter parsing (PDV: once at the boundary)
// ─────────────────────────────────────────────────────────────

/// wiki page frontmatter (optional fields consolidated via raw serde_yaml).
#[derive(Debug, Deserialize)]
pub struct RawFrontMatter {
    pub id: Option<String>,
    pub title: Option<String>,
    pub kind: Option<serde_yaml::Value>,
    pub origin: Option<serde_yaml::Value>,
    pub date: Option<String>,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default)]
    pub relates_to: Vec<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    pub superseded_by: Option<String>,
    #[allow(dead_code)] // optional field — used when extending audit/output
    pub summary: Option<String>,
}

/// A wiki page parsed into typed form at the boundary (PDV-complete state — no re-validation needed afterward).
#[derive(Debug, Clone)]
pub struct Page {
    pub id: String,
    #[allow(dead_code)]
    pub title: String,
    #[allow(dead_code)]
    pub kind: Kind,
    #[allow(dead_code)]
    pub origin: Origin,
    #[allow(dead_code)]
    pub date: String,
    #[allow(dead_code)]
    pub sources: Vec<String>,
    pub relates_to: Vec<String>,
    #[allow(dead_code)]
    pub tags: Vec<String>,
    pub superseded_by: Option<String>,
    pub body: String,
    #[allow(dead_code)]
    pub path: PathBuf,
}

/// Split a `--- yaml ---\nbody` style .md file into (raw frontmatter YAML, body).
/// Pure function — &str input, no I/O.
fn split_frontmatter(content: &str) -> Option<(&str, &str)> {
    let rest = content.strip_prefix("---\n")?;
    let end = rest.find("\n---\n")?;
    let yaml = &rest[..end];
    let body = &rest[end + 5..];
    Some((yaml, body))
}

/// raw frontmatter YAML string → `RawFrontMatter`. Pure function.
fn parse_raw_frontmatter(yaml: &str) -> Result<RawFrontMatter> {
    serde_yaml::from_str(yaml).context("failed to parse frontmatter YAML")
}

/// `/…/wiki-0002.md` → `Some("wiki-0002")`. None if not a wiki note. Pure.
fn wiki_stem(source_path: &str) -> Option<String> {
    let name = source_path.rsplit('/').next()?;
    let stem = name.strip_suffix(".md")?;
    stem.starts_with("wiki-").then(|| stem.to_owned())
}

/// Replace only the `relates_to:` block of the frontmatter YAML with a new link list (other keys preserved). Pure.
/// If the key is absent, append at the end. Empty links → `relates_to: []`.
fn set_relates_to(yaml: &str, links: &[String]) -> String {
    let render = |out: &mut Vec<String>| {
        if links.is_empty() {
            out.push("relates_to: []".to_owned());
        } else {
            out.push("relates_to:".to_owned());
            for l in links {
                out.push(format!("- {l}"));
            }
        }
    };
    let mut out: Vec<String> = Vec::new();
    let mut handled = false;
    let mut skip_list = false;
    for line in yaml.lines() {
        if skip_list {
            if line.trim_start().starts_with('-') {
                continue; // skip old relates_to list items
            }
            skip_list = false;
        }
        if !handled && line.starts_with("relates_to:") {
            render(&mut out);
            handled = true;
            skip_list = true;
            continue;
        }
        out.push(line.to_owned());
    }
    if !handled {
        render(&mut out);
    }
    out.join("\n")
}

/// Project the Postgres graph (`related_docs`) into each wiki note's `relates_to` wikilinks.
/// Makes the Obsidian graph view draw the GraphRAG connections directly. Idempotent (recomputed and rewritten every time).
/// Among related documents, only wiki notes in the same vault become `[[wiki-NNNN]]` (so Obsidian can resolve them).
pub async fn project_links(store: &Store, vault_root: &Path, limit: i64) -> Result<usize> {
    let wiki_dir = vault_root.join("wiki");
    let mut updated = 0;
    for entry in std::fs::read_dir(&wiki_dir)
        .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?
    {
        let path = entry?.path();
        let stem_ok = path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.starts_with("wiki-"));
        let ext_ok = path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("md"));
        if !(stem_ok && ext_ok) {
            continue;
        }
        let content = std::fs::read_to_string(&path)?;
        let Some((yaml, body)) = split_frontmatter(&content) else {
            continue;
        };
        let src_path = path.to_string_lossy().into_owned();
        let mut stems: Vec<String> = store
            .related_docs(&src_path, limit)
            .await?
            .iter()
            .filter_map(|p| wiki_stem(p))
            .collect();
        // isolation prevention: if there are fewer than 2 concept-overlap links, supplement with the same project's latest documents (a few).
        if stems.len() < 2 {
            for p in store.recent_project_docs(&src_path, 2).await? {
                if let Some(s) = wiki_stem(&p)
                    && !stems.contains(&s)
                {
                    stems.push(s);
                }
            }
        }
        let links: Vec<String> = stems.iter().map(|s| format!("\"[[{s}]]\"")).collect();
        let new_content = format!("---\n{}\n---\n{body}", set_relates_to(yaml, &links));
        if new_content != content {
            std::fs::write(&path, new_content)?;
            updated += 1;
        }
    }
    Ok(updated)
}

/// `kind` YAML value → `Kind` enum. Pure function.
fn parse_kind(val: &serde_yaml::Value) -> Option<Kind> {
    match val.as_str()? {
        "note" => Some(Kind::Note),
        "memory" => Some(Kind::Memory),
        "session" => Some(Kind::Session),
        "decision" => Some(Kind::Decision),
        _ => None,
    }
}

/// `origin` YAML value → `Origin` enum. Pure function.
fn parse_origin(val: &serde_yaml::Value) -> Option<Origin> {
    match val.as_str()? {
        "personal" => Some(Origin::Personal),
        "company" => Some(Origin::Company),
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────
// Wikilink extraction (pure)
// ─────────────────────────────────────────────────────────────

/// Extract `[[wiki-NNNN]]` / `[[wiki-NNNN|alias]]` style wikilink target IDs from the body.
/// Pure function.
fn extract_wikilinks(body: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = body;
    while let Some(open) = rest.find("[[") {
        let after_open = &rest[open + 2..];
        let Some(close) = after_open.find("]]") else {
            break;
        };
        let inner = &after_open[..close];
        // strip alias: [[id|alias]] → id
        let target = inner.split('|').next().unwrap_or(inner).trim();
        out.push(target.to_owned());
        rest = &after_open[close + 2..];
    }
    out
}

/// Normalize a `relates_to` entry to a bare page id. `project_links` writes Obsidian-style
/// `"[[wiki-NNNN]]"` (quotes + brackets, optional `|alias`) so the graph view renders links, but
/// audit/lint compare against bare ids — strip the wrapper so both writers agree. Pure.
fn normalize_link_id(raw: &str) -> String {
    let s = raw.trim().trim_matches('"').trim();
    let s = s
        .strip_prefix("[[")
        .map_or(s, |r| r.strip_suffix("]]").unwrap_or(r));
    s.split('|').next().unwrap_or(s).trim().to_owned()
}

/// Check whether the body contains cross-layer links of the form `[[raw/...]]` / `[[meta/...]]` / `[[.rules/...]]`.
/// Pure function.
fn find_cross_layer_wikilinks(body: &str) -> Vec<String> {
    extract_wikilinks(body)
        .into_iter()
        .filter(|t| t.starts_with("raw/") || t.starts_with("meta/") || t.starts_with(".rules/"))
        .collect()
}

// ─────────────────────────────────────────────────────────────
// Lint — pure sub-functions (SRP: each check as a separate function)
// ─────────────────────────────────────────────────────────────

/// Check existence of required_frontmatter keys. Pure.
fn check_required_fields(
    raw_fm: &RawFrontMatter,
    required_keys: &[String],
    stem: &str,
    issues: &mut Vec<Issue>,
) {
    for key in required_keys {
        let present = match key.as_str() {
            "id" => raw_fm.id.is_some(),
            "title" => raw_fm.title.is_some(),
            "kind" => raw_fm.kind.is_some(),
            "origin" => raw_fm.origin.is_some(),
            "date" => raw_fm.date.is_some(),
            other => {
                issues.push(Issue::warn(
                    "schema-unknown-required",
                    stem,
                    format!("unknown key in schema required_frontmatter: '{other}'"),
                ));
                true
            }
        };
        if !present {
            issues.push(Issue::error(
                "required-fm-missing",
                stem,
                format!("required frontmatter key missing: '{key}'"),
            ));
        }
    }
}

/// Check id value integrity. Pure.
fn check_id_value(
    raw_fm: &RawFrontMatter,
    id_re: &regex::Regex,
    stem: &str,
    issues: &mut Vec<Issue>,
) {
    if let Some(fm_id) = &raw_fm.id {
        if !id_re.is_match(fm_id) {
            issues.push(Issue::error(
                "id-pattern",
                stem,
                format!("frontmatter id '{fm_id}' does not match schema pattern"),
            ));
        }
        if fm_id != stem {
            issues.push(Issue::error(
                "id-mismatch",
                stem,
                format!("frontmatter id '{fm_id}' ≠ filename stem '{stem}'"),
            ));
        }
    }
}

/// Check sources (prefix + file existence). Pure — includes vault_root filesystem access.
fn check_sources(
    raw_fm: &RawFrontMatter,
    allowed_prefixes: &[String],
    vault_root: &Path,
    stem: &str,
    issues: &mut Vec<Issue>,
) {
    for src in &raw_fm.sources {
        let has_valid_prefix = allowed_prefixes.iter().any(|p| src.starts_with(p.as_str()));
        if has_valid_prefix {
            let file_part = src.split('#').next().unwrap_or(src);
            let full_path = vault_root.join(file_part);
            if !full_path.exists() {
                issues.push(Issue::warn(
                    "source-missing",
                    stem,
                    format!("sources file does not exist: {src}"),
                ));
            }
        } else {
            issues.push(Issue::error(
                "source-prefix-violation",
                stem,
                format!(
                    "sources path '{src}' has a disallowed prefix (allowed: {allowed_prefixes:?})"
                ),
            ));
        }
    }
}

/// Check wikilinks (cross-layer + dangling). Pure.
fn check_wikilinks(body: &str, stem: &str, known_ids: &HashSet<String>, issues: &mut Vec<Issue>) {
    for bad_link in find_cross_layer_wikilinks(body) {
        issues.push(Issue::error(
            "cross-layer-wikilink",
            stem,
            format!("cross-layer wikilink [[{bad_link}]] — reference it via the sources: field"),
        ));
    }
    for link in extract_wikilinks(body) {
        if link.starts_with("wiki-") && !known_ids.contains(&link) {
            issues.push(Issue::error(
                "wikilink-dangling",
                stem,
                format!("body [[{link}]] target page does not exist"),
            ));
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Lint — public entry point (pure)
// ─────────────────────────────────────────────────────────────

/// Take one file's content + path and return the list of issues. Pure function (the only I/O is checking sources existence).
///
/// # Arguments
/// - `abs_path`: absolute file path
/// - `content`: full file content
/// - `schema`: typed schema
/// - `vault_root`: vault root absolute path (for source file existence checks)
/// - `known_ids`: set of all page IDs that exist in vault/wiki
#[allow(clippy::too_many_lines)] // many integrity checks per page — a procedural list within a single responsibility (lint)
pub fn lint_page(
    abs_path: &Path,
    content: &str,
    schema: &Schema,
    vault_root: &Path,
    known_ids: &HashSet<String>,
) -> (Option<Page>, Vec<Issue>) {
    let stem = abs_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_owned();
    let mut issues = Vec::new();

    // ── id-format: filename pattern check ──
    let id_re = match regex::Regex::new(&schema.page_id.pattern) {
        Ok(r) => r,
        Err(e) => {
            issues.push(Issue::error(
                "schema-invalid",
                &stem,
                format!("failed to compile page_id.pattern: {e}"),
            ));
            return (None, issues);
        }
    };
    if !id_re.is_match(&stem) {
        issues.push(Issue::error(
            "id-format",
            &stem,
            format!(
                "filename stem '{stem}' does not match schema pattern ({})",
                schema.page_id.pattern
            ),
        ));
        return (None, issues);
    }

    // ── frontmatter split ──
    let Some((yaml, body)) = split_frontmatter(content) else {
        issues.push(Issue::error(
            "fm-parse",
            &stem,
            "no YAML frontmatter (--- ... ---)",
        ));
        return (None, issues);
    };

    // ── frontmatter YAML parsing ──
    let raw_fm = match parse_raw_frontmatter(yaml) {
        Ok(fm) => fm,
        Err(e) => {
            issues.push(Issue::error(
                "fm-parse",
                &stem,
                format!("failed to parse YAML: {e}"),
            ));
            return (None, issues);
        }
    };

    // ── checks (UX boundary — accumulated) ──
    check_required_fields(&raw_fm, &schema.required_frontmatter, &stem, &mut issues);
    check_id_value(&raw_fm, &id_re, &stem, &mut issues);

    // ── kind parsing ──
    let kind = raw_fm.kind.as_ref().and_then(parse_kind);
    if raw_fm.kind.is_some() && kind.is_none() {
        let raw_str = raw_fm.kind.as_ref().and_then(|v| v.as_str()).unwrap_or("?");
        issues.push(Issue::error(
            "kind-invalid",
            &stem,
            format!("kind '{raw_str}' is not an allowed value (note/memory/session/decision)"),
        ));
    }

    // ── origin parsing ──
    let origin = raw_fm.origin.as_ref().and_then(parse_origin);
    if raw_fm.origin.is_some() && origin.is_none() {
        let raw_str = raw_fm
            .origin
            .as_ref()
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        issues.push(Issue::error(
            "origin-invalid",
            &stem,
            format!("origin '{raw_str}' is not an allowed value (personal/company)"),
        ));
    }

    check_wikilinks(body, &stem, known_ids, &mut issues);
    check_sources(
        &raw_fm,
        &schema.sources.allowed_prefixes,
        vault_root,
        &stem,
        &mut issues,
    );

    // ── Page construction ──
    let page = if let (Some(id), Some(title), Some(kind), Some(origin), Some(date)) = (
        raw_fm.id.clone(),
        raw_fm.title.clone(),
        kind,
        origin,
        raw_fm.date.clone(),
    ) {
        Some(Page {
            id,
            title,
            kind,
            origin,
            date,
            sources: raw_fm.sources.clone(),
            // Normalize Obsidian-style "[[wiki-NNNN]]" (from project_links) to bare ids so audit's
            // superseded/adjacency checks recognize them.
            relates_to: raw_fm
                .relates_to
                .iter()
                .map(|r| normalize_link_id(r))
                .collect(),
            tags: raw_fm.tags.clone(),
            superseded_by: raw_fm.superseded_by,
            body: body.to_owned(),
            path: abs_path.to_owned(),
        })
    } else {
        None
    };

    (page, issues)
}

// ─────────────────────────────────────────────────────────────
// Audit — pure graph sub-functions (SRP)
// ─────────────────────────────────────────────────────────────

/// Check superseded_by dangling + superseded-referenced. Pure.
fn check_superseded(pages: &[Page], page_ids: &HashSet<&str>, issues: &mut Vec<Issue>) {
    let superseded_page_ids: HashSet<&str> = pages
        .iter()
        .filter(|p| p.superseded_by.is_some())
        .map(|p| p.id.as_str())
        .collect();

    for page in pages {
        if let Some(ref sup) = page.superseded_by
            && !page_ids.contains(sup.as_str())
        {
            issues.push(Issue::error(
                "superseded-dangling",
                &page.id,
                format!("superseded_by '{sup}' target page does not exist"),
            ));
        }

        // warn if a live page's relates_to points to a superseded page
        if page.superseded_by.is_none() {
            for rel in &page.relates_to {
                if superseded_page_ids.contains(rel.as_str()) {
                    issues.push(Issue::warn(
                        "superseded-referenced",
                        &page.id,
                        format!("relates_to '{rel}' points to an already-superseded page — update to the successor page"),
                    ));
                }
            }
        }
    }
}

/// Build an undirected adjacency list + edge_count. Pure (uses owned String — avoids lifetime complexity).
fn build_adjacency(
    pages: &[Page],
    page_ids: &HashSet<&str>,
) -> (HashMap<String, HashSet<String>>, usize) {
    let mut adj: HashMap<String, HashSet<String>> = HashMap::new();
    let mut edge_set: HashSet<(String, String)> = HashSet::new();

    for page in pages {
        let pid = &page.id;

        let body_links: Vec<String> = extract_wikilinks(&page.body)
            .into_iter()
            .filter(|l| l.starts_with("wiki-") && page_ids.contains(l.as_str()))
            .collect();

        let all_neighbors: HashSet<String> = page
            .relates_to
            .iter()
            .filter(|id| page_ids.contains(id.as_str()))
            .cloned()
            .chain(body_links)
            .collect();

        for nbr in &all_neighbors {
            // undirected — normalize: (min, max)
            let (a, b) = if pid <= nbr {
                (pid.clone(), nbr.clone())
            } else {
                (nbr.clone(), pid.clone())
            };
            if a != b {
                edge_set.insert((a, b));
            }
            adj.entry(pid.clone()).or_default().insert(nbr.clone());
            adj.entry(nbr.clone()).or_default().insert(pid.clone());
        }
    }

    (adj, edge_set.len())
}

/// Return the list of connected-component sizes via BFS. Pure.
fn connected_components(nodes: &[&str], adj: &HashMap<String, HashSet<String>>) -> Vec<usize> {
    let mut visited: HashSet<&str> = HashSet::new();
    let mut sizes: Vec<usize> = Vec::new();

    for &start in nodes {
        if visited.contains(start) {
            continue;
        }
        let mut queue: VecDeque<&str> = VecDeque::new();
        queue.push_back(start);
        visited.insert(start);
        let mut size = 0_usize;
        while let Some(node) = queue.pop_front() {
            size += 1;
            if let Some(neighbors) = adj.get(node) {
                for nbr in neighbors {
                    if visited.insert(nbr.as_str()) {
                        queue.push_back(nbr.as_str());
                    }
                }
            }
        }
        sizes.push(size);
    }

    sizes.sort_unstable_by(|a, b| b.cmp(a));
    sizes
}

// ─────────────────────────────────────────────────────────────
// Audit — public entry point (pure)
// ─────────────────────────────────────────────────────────────

/// Summary of the graph audit result.
#[derive(Debug)]
pub struct AuditSummary {
    pub page_count: usize,
    pub edge_count: usize,
    pub component_count: usize,
    pub component_sizes: Vec<usize>,
    pub orphan_count: usize,
    pub superseded_count: usize,
    pub issues: Vec<Issue>,
}

/// Build the graph from the pages list and return the audit result. Pure function (no I/O).
pub fn audit_pages(pages: &[Page]) -> AuditSummary {
    let mut issues = Vec::new();

    let page_ids: HashSet<&str> = pages.iter().map(|p| p.id.as_str()).collect();
    let superseded_count = pages.iter().filter(|p| p.superseded_by.is_some()).count();

    check_superseded(pages, &page_ids, &mut issues);

    let (adj, edge_count) = build_adjacency(pages, &page_ids);

    // ── orphan check ──
    let mut orphan_count = 0_usize;
    for page in pages {
        let has_edges = adj.get(&page.id).is_some_and(|s| !s.is_empty());
        if !has_edges {
            issues.push(Issue::warn(
                "orphan",
                &page.id,
                "inbound·outbound edges both 0 — orphan page",
            ));
            orphan_count += 1;
        }
    }

    // ── connected components (BFS) ──
    let all_nodes: Vec<&str> = pages.iter().map(|p| p.id.as_str()).collect();
    let component_sizes = connected_components(&all_nodes, &adj);
    let component_count = component_sizes.len();

    if component_count > 1 {
        issues.push(Issue::warn(
            "graph-fragmented",
            "graph",
            format!(
                "{component_count} connected components (sizes: {component_sizes:?}) — add [[wiki-NNNN]] bridges between components"
            ),
        ));
    }

    AuditSummary {
        page_count: pages.len(),
        edge_count,
        component_count,
        component_sizes,
        orphan_count,
        superseded_count,
        issues,
    }
}

// ─────────────────────────────────────────────────────────────
// I/O shell — run_lint / run_audit
// ─────────────────────────────────────────────────────────────

/// Collect the list of vault wiki pages from the filesystem. (I/O)
fn collect_wiki_pages(wiki_dir: &Path) -> Result<(HashSet<String>, Vec<PathBuf>)> {
    let mut known_ids: HashSet<String> = HashSet::new();
    let mut entries: Vec<PathBuf> = Vec::new();

    let read_dir = std::fs::read_dir(wiki_dir)
        .with_context(|| format!("failed to read wiki directory: {}", wiki_dir.display()))?;

    for entry in read_dir {
        let entry = entry.context("failed to read wiki directory entry")?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("md") {
            if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                known_ids.insert(stem.to_owned());
            }
            entries.push(path);
        }
    }
    entries.sort();
    Ok((known_ids, entries))
}

/// Take the vault root, run lint, and return the exit code.
///
/// # Exit code semantics
/// - `0`: no errors (warnings may exist)
/// - `1`: errors present
/// - `2`: only warnings, in strict mode
pub fn run_lint(vault_root: &Path, strict: bool) -> Result<i32> {
    let schema = load_schema(&vault_root.join(".rules/schema.yaml"))?;

    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!(
            "vault/wiki directory does not exist: {}",
            wiki_dir.display()
        );
    }

    let (known_ids, entries) = collect_wiki_pages(&wiki_dir)?;
    let mut all_issues: Vec<Issue> = Vec::new();

    for path in &entries {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read file: {}", path.display()))?;
        let (_page, issues) = lint_page(path, &content, &schema, vault_root, &known_ids);
        all_issues.extend(issues);
    }

    print_issues(&all_issues);
    Ok(exit_code(&all_issues, strict))
}

/// Take the vault root, run audit, and return the exit code.
pub fn run_audit(vault_root: &Path, strict: bool) -> Result<i32> {
    let schema = load_schema(&vault_root.join(".rules/schema.yaml"))?;

    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!(
            "vault/wiki directory does not exist: {}",
            wiki_dir.display()
        );
    }

    let (known_ids, entries) = collect_wiki_pages(&wiki_dir)?;
    let mut pages: Vec<Page> = Vec::new();
    let mut parse_issues: Vec<Issue> = Vec::new();

    for path in &entries {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read file: {}", path.display()))?;
        let (page, issues) = lint_page(path, &content, &schema, vault_root, &known_ids);
        parse_issues.extend(issues);
        if let Some(p) = page {
            pages.push(p);
        }
    }

    let summary = audit_pages(&pages);

    println!("Vault Audit Summary");
    println!("  pages      : {}", summary.page_count);
    println!("  edges      : {}", summary.edge_count);
    println!(
        "  components : {} (sizes: {:?})",
        summary.component_count, summary.component_sizes
    );
    println!("  orphans    : {}", summary.orphan_count);
    println!("  superseded : {}", summary.superseded_count);
    println!();

    let mut all_issues = parse_issues;
    all_issues.extend(summary.issues);
    print_issues(&all_issues);
    Ok(exit_code(&all_issues, strict))
}

// ─────────────────────────────────────────────────────────────
// Output helpers (I/O)
// ─────────────────────────────────────────────────────────────

fn print_issues(issues: &[Issue]) {
    if issues.is_empty() {
        println!("PASSED (0 issues)");
        return;
    }

    let errors: Vec<_> = issues
        .iter()
        .filter(|i| i.severity == Severity::Error)
        .collect();
    let warns: Vec<_> = issues
        .iter()
        .filter(|i| i.severity == Severity::Warn)
        .collect();

    for i in &errors {
        println!("✗ [ERROR] {:30}  {:20}  {}", i.rule, i.target, i.message);
    }
    for i in &warns {
        println!("⚠ [WARN]  {:30}  {:20}  {}", i.rule, i.target, i.message);
    }

    println!("──────────────────────────────────────────────────────────");
    if errors.is_empty() {
        println!("WARNINGS: {} warning(s)", warns.len());
    } else {
        println!(
            "FAILED: {} error(s), {} warning(s)",
            errors.len(),
            warns.len()
        );
    }
}

fn exit_code(issues: &[Issue], strict: bool) -> i32 {
    let has_errors = issues.iter().any(|i| i.severity == Severity::Error);
    let has_warns = issues.iter().any(|i| i.severity == Severity::Warn);

    if has_errors {
        1
    } else if has_warns && strict {
        2
    } else {
        0
    }
}

// ─────────────────────────────────────────────────────────────
// Compile — raw → wiki curation (pure logic + I/O shell)
// ─────────────────────────────────────────────────────────────

/// LLM curation result (parse-don't-validate — typed parse once).
#[derive(Debug, Deserialize)]
struct CuratedLlm {
    // NOTE: no `body` field — the curated body is taken from the post-`<<<BODY>>>` markdown split,
    // never from JSON (avoids escape-fragility of large markdown inside a JSON string).
    title: String,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    tools: Vec<String>,
    #[serde(default)]
    concepts: Vec<String>,
}

/// In-memory representation of a compiled page (before relation links are computed).
#[derive(Debug, Clone)]
pub struct CompiledDraft {
    pub wiki_id: String,
    pub title: String,
    pub kind: Kind,
    pub origin: Origin,
    pub date: String,
    pub raw_rel_path: String, // raw/<filename>
    pub raw_sha: String,
    pub body: String,
    pub tags: Vec<String>,
    pub tools: Vec<String>,
    pub concepts: Vec<String>,
    pub relates_to: Vec<String>, // filled after relation pass
}

/// Compile-source info extracted from an existing wiki page (idempotency key).
#[derive(Debug, Clone)]
struct WikiMeta {
    wiki_id: String,
    #[allow(dead_code)] // used as HashMap key; kept for debug clarity
    compiled_from: String, // raw/<filename>
    raw_sha: String,
}

/// Extended fields of wiki frontmatter (compile-only).
#[derive(Debug, Deserialize)]
struct WikiFrontMatterExt {
    #[serde(default)]
    compiled_from: Option<String>,
    #[serde(default)]
    raw_sha: Option<String>,
    #[serde(default)]
    id: Option<String>,
}

/// Whether it contains CJK Unified Ideographs (U+4E00..=U+9FFF) (same logic as extract.rs).
fn has_han(s: &str) -> bool {
    s.chars().any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c))
}

/// Apply the Han filter and keep only valid items.
fn filter_han(items: Vec<String>) -> Vec<String> {
    items.into_iter().filter(|s| !has_han(s)).collect()
}

/// Normalize into an Obsidian-safe tag (pure). Prevents tags containing spaces that the LLM emits (`claude code`)
/// from breaking in Obsidian — space/disallowed chars → `-`, collapse consecutive dashes,
/// trim leading/trailing `-`·`/`, lowercase. Allowed set = `[a-z0-9_/-]` (`/` = nested tag).
/// Empty value · pure-number (Obsidian-invalid tags) → `None`.
fn sanitize_tag(raw: &str) -> Option<String> {
    let mut out = String::with_capacity(raw.len());
    let mut prev_dash = false;
    for c in raw.trim().to_lowercase().chars() {
        let mapped = if c.is_ascii_alphanumeric() || c == '_' || c == '/' {
            c
        } else {
            '-' // spaces, hyphens, and other punctuation all converge to a hyphen
        };
        if mapped == '-' {
            if prev_dash {
                continue; // collapse consecutive dashes
            }
            prev_dash = true;
        } else {
            prev_dash = false;
        }
        out.push(mapped);
    }
    let trimmed = out.trim_matches(|c| c == '-' || c == '/').to_owned();
    if trimmed.is_empty() || trimmed.chars().all(|c| c.is_ascii_digit()) {
        return None; // empty value · pure-number = Obsidian-invalid
    }
    Some(trimmed)
}

/// Extract the `repo: <slug>` marker from a distill note header (pure). A deterministic value that
/// distill-session.py → /distill fills from the host git remote (falling back to the folder name) and render_note embeds in a blockquote.
/// Scans only the front portion (header area) to avoid body false positives. `None` if absent.
fn parse_repo_marker(raw: &str) -> Option<String> {
    let head = raw.get(..raw.len().min(400)).unwrap_or(raw);
    let idx = head.find("repo:")?;
    // The writer (render_note) delimits header fields with " · ", so split on that — not on
    // whitespace, which would truncate a folder-name fallback slug that contains spaces.
    let tok = head[idx + "repo:".len()..]
        .split(" · ")
        .next()
        .unwrap_or("")
        .lines()
        .next()
        .unwrap_or("")
        .trim();
    (!tok.is_empty()).then(|| tok.to_owned())
}

/// Scan the current wiki directory for the (compiled_from → WikiMeta) map + the maximum numeric id.
/// Pure file-parsing logic (includes I/O but SRP-separated: read-only scan).
fn scan_existing_wiki(wiki_dir: &Path) -> Result<(HashMap<String, WikiMeta>, u32)> {
    let mut map: HashMap<String, WikiMeta> = HashMap::new();
    let mut max_id: u32 = 0;

    if !wiki_dir.exists() {
        return Ok((map, max_id));
    }

    let read_dir = std::fs::read_dir(wiki_dir)
        .with_context(|| format!("failed to read wiki directory: {}", wiki_dir.display()))?;

    for entry in read_dir {
        let entry = entry.context("failed to read wiki entry")?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();

        // extract numeric id: wiki-NNNN → NNNN
        if let Some(n) = stem
            .strip_prefix("wiki-")
            .and_then(|s| s.parse::<u32>().ok())
            && n > max_id
        {
            max_id = n;
        }

        let content = std::fs::read_to_string(&path)
            .with_context(|| format!("failed to read wiki file: {}", path.display()))?;
        let Some((yaml, _body)) = split_frontmatter(&content) else {
            continue;
        };
        let ext: WikiFrontMatterExt = match serde_yaml::from_str(yaml) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if let (Some(wiki_id), Some(compiled_from), Some(raw_sha)) =
            (ext.id, ext.compiled_from, ext.raw_sha)
        {
            map.insert(
                compiled_from.clone(),
                WikiMeta {
                    wiki_id,
                    compiled_from,
                    raw_sha,
                },
            );
        }
    }

    Ok((map, max_id))
}

/// sha256 of file bytes (hex string).
fn sha256_file(path: &Path) -> Result<String> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(path)
        .with_context(|| format!("failed to read raw file: {}", path.display()))?;
    let hash = Sha256::digest(&bytes);
    Ok(hex::encode(hash))
}

/// LLM curation system prompt.
const COMPILE_SYSTEM: &str = "You are a precise curator. Output one line of JSON metadata, then a line with exactly <<<BODY>>>, then the curated markdown body as plain text (do NOT JSON-encode or escape the body). No fences, no prose. /no_think";

/// LLM curation prompt template. Two-part output: a small JSON header (escapable) + a raw
/// markdown body after a `<<<BODY>>>` delimiter — the body is never JSON-encoded, which avoids
/// the escape-fragility of stuffing large markdown into a JSON string field.
const COMPILE_PROMPT_TMPL: &str = r#"Curate the raw note below into a wiki page. Output in TWO parts:

PART 1 — one line of JSON metadata (NO body key, no extra keys):
{"title":"<short title, ≤60 chars>","tags":["tag1"],"tools":["tool1"],"concepts":["concept1"]}

PART 2 — a line containing exactly:
<<<BODY>>>
then the curated markdown body. Write it as plain markdown — do NOT escape or JSON-encode it. Keep all important insights, add WHY context.
Do NOT add a "Related" / "See also" / "관련" section or any [[wikilinks]] — cross-links are managed separately.

Rules:
- title: short, descriptive, ≤60 chars
- tags: ≤6 topical tags, lowercase, no Han/CJK characters
- tools: ≤6 software tools/libraries used, short canonical names, no Han/CJK
- concepts: ≤6 key technical concepts or patterns, no Han/CJK
- title/tags/tools/concepts: NO Chinese/Japanese characters (漢字/汉字/CJK). Use empty arrays [] if not applicable
- body: follow the LANGUAGE instruction appended below

Raw note:
---
{BODY}
---
/no_think"#;

/// Compute the relation map based on shared tools/concepts (pure function).
/// Returns: wiki_id → Vec<related_wiki_id> (self excluded, no duplicates).
fn compute_relations(drafts: &[CompiledDraft]) -> HashMap<String, Vec<String>> {
    // tool/concept slug → wiki_id inverted index
    let mut tool_idx: HashMap<String, Vec<String>> = HashMap::new();
    let mut concept_idx: HashMap<String, Vec<String>> = HashMap::new();

    for d in drafts {
        for t in &d.tools {
            tool_idx
                .entry(t.clone())
                .or_default()
                .push(d.wiki_id.clone());
        }
        for c in &d.concepts {
            concept_idx
                .entry(c.clone())
                .or_default()
                .push(d.wiki_id.clone());
        }
    }

    // wiki_id → set of related ids
    let mut rel: HashMap<String, HashSet<String>> = HashMap::new();

    let add_relations = |idx: &HashMap<String, Vec<String>>,
                         rel: &mut HashMap<String, HashSet<String>>| {
        for ids in idx.values() {
            for i in ids {
                for j in ids {
                    if i != j {
                        rel.entry(i.clone()).or_default().insert(j.clone());
                    }
                }
            }
        }
    };

    add_relations(&tool_idx, &mut rel);
    add_relations(&concept_idx, &mut rel);

    rel.into_iter()
        .map(|(k, v)| {
            let mut sorted: Vec<String> = v.into_iter().collect();
            sorted.sort();
            (k, sorted)
        })
        .collect()
}

/// CompiledDraft → render wiki .md file content (pure function).
#[allow(clippy::items_after_statements)]
fn render_wiki_page(draft: &CompiledDraft) -> Result<String> {
    let kind_str = match draft.kind {
        Kind::Note => "note",
        Kind::Memory => "memory",
        Kind::Session => "session",
        Kind::Decision => "decision",
    };
    let origin_str = match draft.origin {
        Origin::Personal => "personal",
        Origin::Company => "company",
    };

    // serialize frontmatter via serde_yaml (SSOT)
    #[derive(Serialize)]
    struct Fm<'a> {
        id: &'a str,
        title: &'a str,
        kind: &'a str,
        origin: &'a str,
        date: &'a str,
        sources: Vec<&'a str>,
        compiled_from: &'a str,
        raw_sha: &'a str,
        relates_to: &'a [String],
        tags: &'a [String],
    }

    let fm = Fm {
        id: &draft.wiki_id,
        title: &draft.title,
        kind: kind_str,
        origin: origin_str,
        date: &draft.date,
        sources: vec![draft.raw_rel_path.as_str()],
        compiled_from: &draft.raw_rel_path,
        raw_sha: &draft.raw_sha,
        relates_to: &draft.relates_to,
        tags: &draft.tags,
    };

    let yaml = serde_yaml::to_string(&fm).context("failed to serialize frontmatter YAML")?;

    // ## Related section + [[wiki-NNNN]] wikilinks
    let related_section = if draft.relates_to.is_empty() {
        String::new()
    } else {
        let links: String = draft
            .relates_to
            .iter()
            .map(|id| format!("- [[{id}]]"))
            .collect::<Vec<_>>()
            .join("\n");
        format!("\n\n## Related\n\n{links}")
    };

    Ok(format!("---\n{yaml}---\n{}{related_section}\n", draft.body))
}

/// Compile stats.
#[derive(Debug, Default)]
pub struct CompileStats {
    pub compiled: usize,
    pub recompiled: usize,
    pub skipped: usize,
    pub total_raw: usize,
}

/// `drudge vault compile` entry point (I/O shell — delegates to pure logic).
#[allow(clippy::too_many_lines)]
/// Strip inline `[[wikilinks]]` from a string (manual scan — no regex dep).
fn strip_wikilinks(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find("[[") {
        out.push_str(&rest[..i]);
        rest = rest[i + 2..]
            .find("]]")
            .map_or(&rest[i + 2..], |j| &rest[i + 2..][j + 2..]);
    }
    out.push_str(rest);
    out
}

/// Sanitize a curated body: drop a trailing "Related"/"관련"/"See also" section and any inline
/// `[[wikilinks]]`. Relations are SSOT in frontmatter `relates_to` (computed from the graph) — gemma
/// otherwise hallucinates long bogus `[[wiki-NNNN]]` lists that pollute the body + Obsidian graph.
fn sanitize_body(body: &str) -> String {
    let lines: Vec<&str> = body.lines().collect();
    let cut = lines.iter().position(|l| {
        let t = l.trim_start_matches('#').trim();
        t.eq_ignore_ascii_case("related") || t == "관련" || t.eq_ignore_ascii_case("see also")
    });
    let kept = match cut {
        Some(i) => lines[..i].join("\n"),
        None => body.to_owned(),
    };
    strip_wikilinks(&kept).trim_end().to_owned()
}

/// Parse a compile LLM response: leading JSON metadata object + trailing markdown body.
/// The `<<<BODY>>>` delimiter is stripped if present but not required (key off the JSON object's end).
/// Returns None on any malformation (no header / bad JSON / empty body) — caller retries or skips.
fn parse_compiled(llm_raw: &str) -> Option<(CuratedLlm, String)> {
    let start = llm_raw.find('{')?;
    let after = &llm_raw[start..];
    let mut stream = serde_json::Deserializer::from_str(after).into_iter::<CuratedLlm>();
    let curated = stream.next()?.ok()?;
    let rest = after[stream.byte_offset()..].trim_start();
    // Take everything after the LAST `<<<BODY>>>` marker if present (not just a prefix) — a model
    // may emit a stray fence/preamble between the JSON and the marker, which must not leak into the
    // wiki body (→ corpus). Fall back to the post-JSON remainder when the marker is absent.
    let body = rest
        .rsplit_once("<<<BODY>>>")
        .map_or(rest, |(_, b)| b)
        .trim()
        .to_owned();
    if body.is_empty() {
        return None;
    }
    Some((curated, body))
}

// Legit long orchestration: scan existing wiki → per-note compile+incremental-write loop →
// relation pass. Splitting the linear pipeline into sub-fns with shared mutable state (max_id,
// stats, maps) would obscure, not clarify.
#[allow(clippy::too_many_lines)]
pub async fn run_compile(
    vault_root: &Path,
    raw_dir: &Path,
    today: &str,
    llm: &crate::llm::Llm,
) -> Result<CompileStats> {
    let wiki_dir = vault_root.join("wiki");
    std::fs::create_dir_all(&wiki_dir)
        .with_context(|| format!("failed to create wiki directory: {}", wiki_dir.display()))?;

    // 1. scan existing wiki — idempotency key map + max id
    let (existing_map, mut max_id) = scan_existing_wiki(&wiki_dir)?;

    // 2. collect raw directory (*.md only)
    let mut raw_entries: Vec<PathBuf> = {
        let rd = std::fs::read_dir(raw_dir)
            .with_context(|| format!("failed to read raw directory: {}", raw_dir.display()))?;
        rd.filter_map(|e| {
            let e = e.ok()?;
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) == Some("md") {
                Some(p)
            } else {
                None
            }
        })
        .collect()
    };
    raw_entries.sort();

    let mut stats = CompileStats {
        total_raw: raw_entries.len(),
        ..Default::default()
    };

    // 3. process each raw file
    let mut drafts: Vec<CompiledDraft> = Vec::new();
    // wiki_id → path (for overwriting on recompile)
    let mut wiki_path_map: HashMap<String, PathBuf> = HashMap::new();

    for raw_path in &raw_entries {
        let filename = raw_path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_owned();
        let raw_rel = format!("raw/{filename}");

        let sha = sha256_file(raw_path)?;

        // idempotency check
        let (wiki_id, is_new) = if let Some(meta) = existing_map.get(&raw_rel) {
            if meta.raw_sha == sha {
                eprintln!("↷ skip (unchanged): {filename}");
                stats.skipped += 1;
                continue;
            }
            // sha changed → recompile with the same id
            (meta.wiki_id.clone(), false)
        } else {
            // new → next id
            max_id += 1;
            (format!("wiki-{max_id:04}"), true)
        };

        // determine origin — env `DRUDGE_COMPANY_SUBSTR` token match (always Personal if unset)
        let origin = if raw_path
            .to_str()
            .is_some_and(crate::frontmatter::is_company_path)
        {
            Origin::Company
        } else {
            Origin::Personal
        };

        // mtime → date (once at the I/O boundary)
        let date = raw_path
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| {
                use std::time::UNIX_EPOCH;
                let secs = t.duration_since(UNIX_EPOCH).ok()?.as_secs();
                let days = (secs / 86400).cast_signed();
                // simple date computation (without chrono — deterministic)
                // epoch = 1970-01-01. days_since_epoch → gregorian date
                Some(days_to_date(days))
            })
            .unwrap_or_else(|| today.to_owned());

        // LLM curation
        let body_raw = std::fs::read_to_string(raw_path)
            .with_context(|| format!("failed to read raw file: {}", raw_path.display()))?;
        let body_snip: String = body_raw.chars().take(4000).collect();
        let prompt = COMPILE_PROMPT_TMPL.replace("{BODY}", &body_snip);
        // Language directive goes in the SYSTEM message (same as distill), not appended after the
        // user prompt's `/no_think` sentinel where some models weakly honor it.
        let system = format!("{COMPILE_SYSTEM}\n{}", crate::distill::lang_directive());

        // LLM curation with one retry — covers BOTH a transient LLM error and a 12B model emitting a
        // malformed JSON header (non-deterministic); a second attempt usually succeeds. parse_compiled()
        // splits the leading JSON metadata object from the trailing markdown body (delimiter optional).
        let mut parsed: Option<(CuratedLlm, String)> = None;
        for attempt in 1..=2u8 {
            match llm.generate(&system, &prompt).await {
                Ok(raw) => {
                    if let Some(v) = parse_compiled(&raw) {
                        parsed = Some(v);
                        break;
                    }
                    eprintln!("⚠ compile parse retry {attempt} [{filename}] — raw: {raw:.120}");
                }
                Err(e) if attempt < 2 => {
                    eprintln!("⚠ compile LLM error [{filename}] attempt {attempt}: {e} — retrying");
                }
                Err(e) => {
                    eprintln!("⚠ compile LLM error [{filename}]: {e} — skipping");
                }
            }
        }
        let Some((curated, body)) = parsed else {
            stats.skipped += 1;
            continue;
        };
        // Relations live in frontmatter relates_to (SSOT) — strip any LLM-invented body wikilinks/Related section.
        let body = sanitize_body(&body);

        // Han filter + Obsidian-safe normalization (space→-, drop invalid). ≤6 items.
        let mut tags: Vec<String> = filter_han(curated.tags)
            .into_iter()
            .filter_map(|t| sanitize_tag(&t))
            .take(6)
            .collect();
        // new category axis: repo slug (host git, distill marker) → Obsidian nested tag repo/<slug>.
        if let Some(repo) = parse_repo_marker(&body_raw)
            .as_deref()
            .and_then(sanitize_tag)
        {
            tags.insert(0, format!("repo/{repo}"));
        }
        let tools = filter_han(curated.tools).into_iter().take(6).collect();
        let concepts = filter_han(curated.concepts).into_iter().take(6).collect();
        let title = if has_han(&curated.title) {
            filename.trim_end_matches(".md").to_owned()
        } else {
            curated.title
        };

        let draft = CompiledDraft {
            wiki_id: wiki_id.clone(),
            title,
            kind: Kind::Note,
            origin,
            date,
            raw_rel_path: raw_rel,
            raw_sha: sha,
            body,
            tags,
            tools,
            concepts,
            relates_to: Vec::new(), // filled after relation pass
        };

        let wiki_path = wiki_dir.join(format!("{wiki_id}.md"));
        // Incremental write: persist each wiki the moment it compiles (relates_to filled in the
        // post-loop pass). Interruption-safe — a killed compile leaves finished wiki on disk, and the
        // sha-skip above resumes the rest next run (no 60-min all-or-nothing batch that loses all on kill).
        let content = render_wiki_page(&draft)?;
        std::fs::write(&wiki_path, content)
            .with_context(|| format!("failed to write wiki file: {}", wiki_path.display()))?;
        println!("✓ {}: {}", draft.wiki_id, draft.title);
        wiki_path_map.insert(wiki_id.clone(), wiki_path);
        drafts.push(draft);

        if is_new {
            stats.compiled += 1;
        } else {
            stats.recompiled += 1;
        }
    }

    // 4. compute relations, then rewrite ONLY the wiki that gained relates_to (every wiki was already
    //    written incrementally in the loop with empty relates_to). Preserves the relation graph while
    //    keeping the whole compile interruption-safe + resumable.
    let relations = compute_relations(&drafts);
    for draft in &mut drafts {
        let links = relations.get(&draft.wiki_id).cloned().unwrap_or_default();
        if links.is_empty() {
            continue; // already on disk with relates_to: [] — nothing to update
        }
        draft.relates_to = links;
        let content = render_wiki_page(draft)?;
        let path = wiki_path_map
            .get(&draft.wiki_id)
            .with_context(|| format!("wiki path not found: {}", draft.wiki_id))?;
        std::fs::write(path, content)
            .with_context(|| format!("failed to rewrite wiki file: {}", path.display()))?;
    }

    Ok(stats)
}

/// Today's date "YYYY-MM-DD" — the SSOT default when compile's `--date` is unspecified.
/// I/O boundary: `SystemTime::now()` is isolated to this single spot (shared by main·serve) → conversion is the pure `days_to_date`.
#[must_use]
pub fn today_utc() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let days = (secs / 86_400).cast_signed();
    days_to_date(days)
}

/// days since Unix epoch (1970-01-01) → "YYYY-MM-DD" string (pure function, no SystemTime).
fn days_to_date(days: i64) -> String {
    // Proleptic Gregorian calendar conversion (based on the Richards algorithm).
    // 1970-01-01 = JDN 2440588
    let jdn = days + 2_440_588_i64;
    let f = jdn + 1_401 + (((4 * jdn + 274_277) / 146_097) * 3) / 4 - 38;
    let e = 4 * f + 3;
    let g = (e % 1461) / 4;
    let h = 5 * g + 2;
    let day = (h % 153) / 5 + 1;
    let month = (h / 153 + 2) % 12 + 1;
    let year = e / 1461 - 4_716 + (14 - month) / 12;
    format!("{year:04}-{month:02}-{day:02}")
}

// ─────────────────────────────────────────────────────────────
// Unit tests (pure-function tests — no I/O)
// ─────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use std::collections::HashSet;
    use std::path::{Path, PathBuf};

    use super::{
        Kind, Origin, Page, PageIdSchema, Schema, Severity, SourcesSchema, audit_pages,
        extract_wikilinks, find_cross_layer_wikilinks, lint_page, parse_kind, parse_origin,
        parse_repo_marker, sanitize_tag,
    };
    use serde_yaml::Value;

    // ── sanitize_tag (Obsidian-safe normalization) ──

    #[test]
    fn sanitize_tag_space_to_hyphen() {
        // space tags that broke in Obsidian → hyphen (interior hyphens are valid, so kept)
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

    #[test]
    fn sanitize_tag_drops_invalid() {
        assert!(sanitize_tag("2024").is_none()); // pure-number = Obsidian-invalid
        assert!(sanitize_tag("").is_none());
        assert!(sanitize_tag("  ").is_none());
        assert!(sanitize_tag("!!!").is_none());
    }

    // ── parse_repo_marker (distill note header marker) ──

    #[test]
    fn parse_repo_marker_extracts_slug() {
        let note = "# Session Note — 2026-06-12\n> auto-distilled (Claude Code · final) · origin: personal · repo: jazz1x/oh-my-boring · cwd: /x\n\nbody";
        assert_eq!(
            parse_repo_marker(note).as_deref(),
            Some("jazz1x/oh-my-boring")
        );
    }

    #[test]
    fn normalize_link_id_strips_obsidian_wrapper() {
        use super::normalize_link_id;
        assert_eq!(normalize_link_id("\"[[wiki-0002]]\""), "wiki-0002"); // project_links form
        assert_eq!(normalize_link_id("[[wiki-0003|alias]]"), "wiki-0003"); // alias
        assert_eq!(normalize_link_id("wiki-0004"), "wiki-0004"); // already bare
    }

    #[test]
    fn parse_repo_marker_keeps_spaced_folder_slug() {
        // folder-name fallback with a space must not be truncated at the first whitespace
        let note = "# x\n> auto-distilled · origin: personal · repo: my project · cwd: /x\n\nbody";
        assert_eq!(parse_repo_marker(note).as_deref(), Some("my project"));
    }

    #[test]
    fn parse_repo_marker_absent_is_none() {
        let note = "# Session Note\n> origin: personal · cwd: /x\n\nbody";
        assert!(parse_repo_marker(note).is_none());
    }

    fn test_schema() -> Schema {
        Schema {
            page_id: PageIdSchema {
                pattern: r"^wiki-\d{4,5}$".to_owned(),
            },
            sources: SourcesSchema {
                allowed_prefixes: vec!["raw/".to_owned(), "meta/".to_owned(), ".rules/".to_owned()],
            },
            required_frontmatter: vec![
                "id".to_owned(),
                "title".to_owned(),
                "kind".to_owned(),
                "origin".to_owned(),
                "date".to_owned(),
            ],
        }
    }

    fn known_ids(ids: &[&str]) -> HashSet<String> {
        ids.iter().map(|s| (*s).to_owned()).collect()
    }

    // ── extract_wikilinks ──

    #[test]
    fn wikilinks_extracted_correctly() {
        let body = "see: [[wiki-0001]] and [[wiki-0002|second]].";
        let links = extract_wikilinks(body);
        assert_eq!(links, vec!["wiki-0001", "wiki-0002"]);
    }

    #[test]
    fn no_wikilinks_returns_empty() {
        assert!(extract_wikilinks("body with no links").is_empty());
    }

    // ── cross-layer wikilinks ──

    #[test]
    fn cross_layer_detected() {
        let body = "[[raw/seed.md]] and [[wiki-0001]]";
        let bad = find_cross_layer_wikilinks(body);
        assert_eq!(bad, vec!["raw/seed.md"]);
    }

    // ── parse_kind / parse_origin ──

    #[test]
    fn parse_kind_valid() {
        assert_eq!(
            parse_kind(&Value::String("note".to_owned())),
            Some(Kind::Note)
        );
        assert_eq!(
            parse_kind(&Value::String("memory".to_owned())),
            Some(Kind::Memory)
        );
        assert_eq!(
            parse_kind(&Value::String("session".to_owned())),
            Some(Kind::Session)
        );
        assert_eq!(
            parse_kind(&Value::String("decision".to_owned())),
            Some(Kind::Decision)
        );
    }

    #[test]
    fn parse_kind_invalid_returns_none() {
        assert_eq!(parse_kind(&Value::String("unknown".to_owned())), None);
    }

    #[test]
    fn parse_origin_valid() {
        assert_eq!(
            parse_origin(&Value::String("personal".to_owned())),
            Some(Origin::Personal)
        );
        assert_eq!(
            parse_origin(&Value::String("company".to_owned())),
            Some(Origin::Company)
        );
    }

    // ── lint_page: dangling wikilink check ──

    #[test]
    fn dangling_wikilink_detected() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: T\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n[[wiki-9999]]";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            issues.iter().any(|i| i.rule == "wikilink-dangling"),
            "no dangling wikilink issue: {issues:?}"
        );
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "wikilink-dangling" && i.severity == Severity::Error)
        );
    }

    #[test]
    fn valid_wikilink_no_error() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: T\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n[[wiki-0002]]";
        let ids = known_ids(&["wiki-0001", "wiki-0002"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            !issues.iter().any(|i| i.rule == "wikilink-dangling"),
            "dangling issue on a valid wikilink: {issues:?}"
        );
    }

    // ── lint_page: schema frontmatter parsing ──

    #[test]
    fn valid_frontmatter_no_errors() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        let errors: Vec<_> = issues
            .iter()
            .filter(|i| i.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "there should be no errors: {errors:?}");
        assert!(page.is_some(), "Page parse failed");
    }

    #[test]
    fn missing_required_field_is_error() {
        let schema = test_schema();
        // title missing
        let content =
            "---\nid: wiki-0001\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\nbody";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "required-fm-missing" && i.message.contains("title")),
            "no title-missing issue: {issues:?}"
        );
    }

    // ── audit_pages: connected components ──

    fn make_page(id: &str, relates_to: Vec<&str>, body: &str) -> Page {
        Page {
            id: id.to_owned(),
            title: id.to_owned(),
            kind: Kind::Note,
            origin: Origin::Personal,
            date: "2026-01-01".to_owned(),
            sources: vec![],
            relates_to: relates_to.into_iter().map(str::to_owned).collect(),
            tags: vec![],
            superseded_by: None,
            body: body.to_owned(),
            path: PathBuf::from(format!("/vault/wiki/{id}.md")),
        }
    }

    #[test]
    fn two_connected_pages_single_component() {
        let pages = vec![
            make_page("wiki-0001", vec!["wiki-0002"], "[[wiki-0002]]"),
            make_page("wiki-0002", vec!["wiki-0001"], "[[wiki-0001]]"),
        ];
        let summary = audit_pages(&pages);
        assert_eq!(summary.page_count, 2);
        assert_eq!(summary.component_count, 1);
        assert_eq!(summary.orphan_count, 0);
        assert!(!summary.issues.iter().any(|i| i.rule == "graph-fragmented"));
        assert!(!summary.issues.iter().any(|i| i.rule == "orphan"));
    }

    #[test]
    fn disconnected_pages_multiple_components() {
        let pages = vec![
            make_page("wiki-0001", vec![], ""),
            make_page("wiki-0002", vec![], ""),
        ];
        let summary = audit_pages(&pages);
        assert_eq!(summary.component_count, 2);
        assert_eq!(summary.orphan_count, 2);
        assert!(summary.issues.iter().any(|i| i.rule == "orphan"));
        assert!(summary.issues.iter().any(|i| i.rule == "graph-fragmented"));
    }

    #[test]
    fn superseded_dangling_is_error() {
        let mut p = make_page("wiki-0001", vec![], "");
        p.superseded_by = Some("wiki-9999".to_owned());
        let pages = vec![p];
        let summary = audit_pages(&pages);
        assert!(
            summary
                .issues
                .iter()
                .any(|i| i.rule == "superseded-dangling" && i.severity == Severity::Error)
        );
    }

    #[test]
    fn superseded_referenced_is_warn() {
        let mut old = make_page("wiki-0001", vec![], "");
        old.superseded_by = Some("wiki-0002".to_owned());
        let new_page = make_page("wiki-0002", vec![], "");
        // live wiki-0003 references the superseded wiki-0001 via relates_to
        let live = make_page("wiki-0003", vec!["wiki-0001"], "");
        let pages = vec![old, new_page, live];
        let summary = audit_pages(&pages);
        assert!(
            summary
                .issues
                .iter()
                .any(|i| i.rule == "superseded-referenced" && i.severity == Severity::Warn),
            "no superseded-referenced warn: {:?}",
            summary.issues
        );
    }

    // ── compile: monotonic id ──

    #[test]
    fn monotonic_id_next_from_zero() {
        // max_id=0 → next = wiki-0001
        let max_id: u32 = 0;
        let next = format!("wiki-{:04}", max_id + 1);
        assert_eq!(next, "wiki-0001");
    }

    #[test]
    fn monotonic_id_next_increments() {
        let max_id: u32 = 42;
        let next = format!("wiki-{:04}", max_id + 1);
        assert_eq!(next, "wiki-0043");
    }

    #[test]
    fn monotonic_id_pads_to_four_digits() {
        let next = format!("wiki-{:04}", 9_u32 + 1);
        assert_eq!(next, "wiki-0010");
    }

    // ── compile: idempotency — same sha → skip ──

    #[test]
    fn idempotency_same_sha_means_skip() {
        use super::WikiFrontMatterExt;
        // simulate: existing map has raw/foo.md → sha abc123
        let mut existing: std::collections::HashMap<String, super::WikiMeta> =
            std::collections::HashMap::new();
        existing.insert(
            "raw/foo.md".to_owned(),
            super::WikiMeta {
                wiki_id: "wiki-0001".to_owned(),
                compiled_from: "raw/foo.md".to_owned(),
                raw_sha: "abc123".to_owned(),
            },
        );
        let current_sha = "abc123";
        let should_skip = existing
            .get("raw/foo.md")
            .is_some_and(|m| m.raw_sha == current_sha);
        assert!(should_skip, "same sha should be skipped");

        // WikiFrontMatterExt can parse yaml with compiled_from + raw_sha
        let yaml = "id: wiki-0001\ncompiled_from: raw/foo.md\nraw_sha: abc123\n";
        let ext: WikiFrontMatterExt = serde_yaml::from_str(yaml).unwrap();
        assert_eq!(ext.compiled_from.as_deref(), Some("raw/foo.md"));
        assert_eq!(ext.raw_sha.as_deref(), Some("abc123"));
    }

    #[test]
    fn idempotency_changed_sha_reuses_id() {
        let mut existing: std::collections::HashMap<String, super::WikiMeta> =
            std::collections::HashMap::new();
        existing.insert(
            "raw/foo.md".to_owned(),
            super::WikiMeta {
                wiki_id: "wiki-0005".to_owned(),
                compiled_from: "raw/foo.md".to_owned(),
                raw_sha: "old_sha".to_owned(),
            },
        );
        let current_sha = "new_sha";
        let meta = existing.get("raw/foo.md").unwrap();
        let should_skip = meta.raw_sha == current_sha;
        assert!(!should_skip);
        // reuse same wiki id
        assert_eq!(meta.wiki_id, "wiki-0005");
    }

    // ── compile: relation linking ──

    #[test]
    fn relation_linking_shared_tool() {
        let drafts = vec![
            super::CompiledDraft {
                wiki_id: "wiki-0001".to_owned(),
                title: "A".to_owned(),
                kind: Kind::Note,
                origin: Origin::Personal,
                date: "2026-01-01".to_owned(),
                raw_rel_path: "raw/a.md".to_owned(),
                raw_sha: "sha1".to_owned(),
                body: String::new(),
                tags: vec![],
                tools: vec!["rust".to_owned()],
                concepts: vec![],
                relates_to: vec![],
            },
            super::CompiledDraft {
                wiki_id: "wiki-0002".to_owned(),
                title: "B".to_owned(),
                kind: Kind::Note,
                origin: Origin::Personal,
                date: "2026-01-01".to_owned(),
                raw_rel_path: "raw/b.md".to_owned(),
                raw_sha: "sha2".to_owned(),
                body: String::new(),
                tags: vec![],
                tools: vec!["rust".to_owned()],
                concepts: vec![],
                relates_to: vec![],
            },
        ];
        let rels = super::compute_relations(&drafts);
        assert!(
            rels.get("wiki-0001")
                .is_some_and(|v| v.contains(&"wiki-0002".to_owned())),
            "wiki-0001 should relate to wiki-0002 via shared tool 'rust'"
        );
        assert!(
            rels.get("wiki-0002")
                .is_some_and(|v| v.contains(&"wiki-0001".to_owned())),
            "wiki-0002 should relate to wiki-0001 via shared tool 'rust'"
        );
    }

    #[test]
    fn relation_linking_no_shared_entity_no_link() {
        let drafts = vec![
            super::CompiledDraft {
                wiki_id: "wiki-0001".to_owned(),
                title: "A".to_owned(),
                kind: Kind::Note,
                origin: Origin::Personal,
                date: "2026-01-01".to_owned(),
                raw_rel_path: "raw/a.md".to_owned(),
                raw_sha: "sha1".to_owned(),
                body: String::new(),
                tags: vec![],
                tools: vec!["rust".to_owned()],
                concepts: vec![],
                relates_to: vec![],
            },
            super::CompiledDraft {
                wiki_id: "wiki-0002".to_owned(),
                title: "B".to_owned(),
                kind: Kind::Note,
                origin: Origin::Personal,
                date: "2026-01-01".to_owned(),
                raw_rel_path: "raw/b.md".to_owned(),
                raw_sha: "sha2".to_owned(),
                body: String::new(),
                tags: vec![],
                tools: vec!["python".to_owned()],
                concepts: vec![],
                relates_to: vec![],
            },
        ];
        let rels = super::compute_relations(&drafts);
        assert!(
            rels.get("wiki-0001").is_none_or(Vec::is_empty),
            "no shared entity → no link"
        );
    }

    // ── compile: JSON parse (typed parsing) ──

    #[test]
    fn curated_llm_parse_valid() {
        let json = r#"{"title":"Test Title","tags":["rust","rag"],"tools":["surrealdb"],"concepts":["rop"]}"#;
        let c: super::CuratedLlm = serde_json::from_str(json).unwrap();
        assert_eq!(c.title, "Test Title");
        assert_eq!(c.tags, vec!["rust", "rag"]);
        assert_eq!(c.tools, vec!["surrealdb"]);
    }

    #[test]
    fn curated_llm_parse_missing_arrays_defaults_empty() {
        let json = r#"{"title":"T"}"#;
        let c: super::CuratedLlm = serde_json::from_str(json).unwrap();
        assert!(c.tags.is_empty());
        assert!(c.tools.is_empty());
        assert!(c.concepts.is_empty());
    }

    // ── compile: days_to_date ──

    #[test]
    fn days_to_date_epoch() {
        // 1970-01-01 = day 0
        assert_eq!(super::days_to_date(0), "1970-01-01");
    }

    #[test]
    fn days_to_date_known_date() {
        // 2026-06-07: days since epoch
        // 2026-01-01 = 56*365+14 leap days = 20454 days from epoch
        // Jan=0, Feb=31, Mar=59, Apr=90, May=120, Jun=151; Jun 7 = 151+6=157
        // 2026-01-01 from epoch: 2026 years * 365 + leaps
        // We'll test a known value: 2000-01-01 = 10957
        assert_eq!(super::days_to_date(10_957), "2000-01-01");
    }

    // ── han filter ──

    #[test]
    fn han_filter_removes_cjk() {
        let items = vec!["rust".to_owned(), "漢字".to_owned(), "rag".to_owned()];
        let filtered = super::filter_han(items);
        assert_eq!(filtered, vec!["rust", "rag"]);
    }
}
