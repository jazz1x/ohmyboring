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

use crate::frontmatter::{Claim, FrontMatter};
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
    Mirror,
    Community,
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
        // Don't wipe: if the graph projection found nothing, preserve whatever relates_to the compile
        // relation-pass (shared tools/concepts) already set — an empty graph must not clobber it to [].
        if links.is_empty() {
            continue;
        }
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
        "mirror" => Some(Origin::Mirror),
        "community" => Some(Origin::Community),
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
// Remember note rendering (kernel A) — the agent hands drudge a complete note; drudge writes it as a
// wiki page (deterministic file IO) so Obsidian + wiki_recall + disk re-ingest all see one SSOT artifact.
// ─────────────────────────────────────────────────────────────

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

/// Decode an LLM-produced note body into clean markdown — the SSOT normalization at the write gate.
/// Local models (e.g. gemma) sometimes emit JSON-string-escaped text: the two characters backslash-n
/// instead of a real line break, and stray backslash-escapes before markdown punctuation (`` \` ``,
/// `\#`, `\"`). Decoding HERE means every writer — the SessionEnd hook, the hermes cron agent, a direct
/// MCP `remember`, `make remember` — stores real markdown, instead of relying on one adapter's
/// best-effort patch (the duplication that kept regressing). Unknown escapes are kept verbatim so a
/// genuine backslash (a path, a regex) is never harmed.
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
    out.trim().to_owned()
}

/// Scan vault/wiki for the highest `wiki-NNNN` id and return the next one (`wiki-{:04}`). Read-only IO.
pub fn next_wiki_id(wiki_dir: &Path) -> Result<String> {
    let mut max_id: u32 = 0;
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
                && n > max_id
            {
                max_id = n;
            }
        }
    }
    Ok(format!("wiki-{:04}", max_id + 1))
}

/// Render a remember-note into wiki `.md` content (pure). Frontmatter satisfies the lint schema
/// (id·title·kind·origin·date) AND carries the agent-curated semantic fields (tags·tools·concepts·claims)
/// so a disk re-ingest rebuilds the same graph deterministically. `relates_to` starts `[]` and is filled
/// by `project_links` from the graph (SSOT for relations).
pub fn render_wiki_note(wiki_id: &str, front: &FrontMatter, body: &str) -> Result<String> {
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
        claims: &'a [Claim],
        relates_to: Vec<String>,
        sources: Vec<String>,
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
        extract_wikilinks, find_cross_layer_wikilinks, lint_page, normalize_body, parse_kind,
        parse_origin, sanitize_tag,
    };
    use serde_yaml::Value;

    // ── normalize_body (LLM JSON-escape decode at the write gate) ──

    #[test]
    fn normalize_body_decodes_literal_newline() {
        // gemma emits the two characters backslash-n instead of a real break → run-on blob (wiki-0142)
        assert_eq!(
            normalize_body("### A\\n1. x\\n\\n### B"),
            "### A\n1. x\n\n### B"
        );
    }

    #[test]
    fn normalize_body_unescapes_stray_markdown_punctuation() {
        // over-escaped backtick/hash/quote the model adds inside its JSON string (wiki-0142)
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
        // unknown escapes (a Windows path, a regex) must pass through untouched
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

    // ── days_to_date ──

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
}
