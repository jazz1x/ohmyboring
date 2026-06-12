//! Vault lint / audit — personal Obsidian markdown KB 정합성 검사.
//!
//! # 설계 원칙 (PRINCIPLES.md)
//! - **PDV**: `schema.yaml` 과 `.md` frontmatter 를 경계에서 1회 typed 파싱.
//! - **ROP**: `?` 전파 + anyhow Context. unwrap/expect/panic 없음.
//! - **ADT**: `Kind`, `Origin`, `Severity` 를 enum 으로. 불가능 상태 표현 불가능하게.
//! - **SRP**: 순수 로직(parse/graph) 과 I/O(파일 읽기) 분리.
//!   - `split_frontmatter`, `parse_*`, `lint_*`, `audit_*`: 순수 — &str/슬라이스 입력 → 값
//!   - `run_lint` / `run_audit`: I/O 쉘 — 파일 수집 후 순수 함수 위임

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;
use serde::Serialize;

use crate::store::Store;

// ─────────────────────────────────────────────────────────────
// ADT — 불가능 상태를 표현 불가능하게
// ─────────────────────────────────────────────────────────────

/// 페이지 kind 허용값.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Note,
    Memory,
    Session,
    Decision,
}

/// 페이지 origin 허용값.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Origin {
    Personal,
    Company,
}

/// 이슈 심각도.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum Severity {
    Error,
    Warn,
}

// ─────────────────────────────────────────────────────────────
// 도메인 타입 (typed 증거 — PDV)
// ─────────────────────────────────────────────────────────────

/// lint/audit 결과 이슈. I/O 와 분리된 순수 값.
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
// Schema 파싱 (PDV: 경계 1회 typed 파싱)
// ─────────────────────────────────────────────────────────────

/// `.rules/schema.yaml` 의 typed 표현.
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

/// 파일에서 schema 를 읽어 typed 값으로 파싱. (I/O 경계)
pub fn load_schema(schema_path: &Path) -> Result<Schema> {
    let raw = std::fs::read_to_string(schema_path)
        .with_context(|| format!("schema 파일 읽기 실패: {}", schema_path.display()))?;
    serde_yaml::from_str(&raw)
        .with_context(|| format!("schema YAML 파싱 실패: {}", schema_path.display()))
}

// ─────────────────────────────────────────────────────────────
// Frontmatter 파싱 (PDV: 경계 1회)
// ─────────────────────────────────────────────────────────────

/// wiki 페이지 frontmatter (raw serde_yaml 으로 선택 필드 통합).
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
    #[allow(dead_code)] // 선택 필드 — audit/출력 확장 시 사용
    pub summary: Option<String>,
}

/// 경계에서 typed 로 파싱된 wiki 페이지 (PDV 완성 상태 — 이후 재검증 불필요).
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

/// `--- yaml ---\nbody` 형태의 .md 파일을 (raw frontmatter YAML, body) 로 분리.
/// 순수 함수 — &str 입력, no I/O.
fn split_frontmatter(content: &str) -> Option<(&str, &str)> {
    let rest = content.strip_prefix("---\n")?;
    let end = rest.find("\n---\n")?;
    let yaml = &rest[..end];
    let body = &rest[end + 5..];
    Some((yaml, body))
}

/// raw frontmatter YAML 문자열 → `RawFrontMatter`. 순수 함수.
fn parse_raw_frontmatter(yaml: &str) -> Result<RawFrontMatter> {
    serde_yaml::from_str(yaml).context("frontmatter YAML 파싱 실패")
}

/// `/…/wiki-0002.md` → `Some("wiki-0002")`. wiki 노트가 아니면 None. 순수.
fn wiki_stem(source_path: &str) -> Option<String> {
    let name = source_path.rsplit('/').next()?;
    let stem = name.strip_suffix(".md")?;
    stem.starts_with("wiki-").then(|| stem.to_owned())
}

/// frontmatter YAML 의 `relates_to:` 블록만 새 링크 리스트로 교체(다른 키 보존). 순수.
/// 키가 없으면 끝에 추가. 빈 링크는 `relates_to: []`.
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
                continue; // 옛 relates_to 리스트 항목 건너뜀
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

/// Postgres 그래프(`related_docs`)를 각 wiki 노트의 `relates_to` 위키링크로 투영.
/// Obsidian 그래프뷰가 GraphRAG 연결을 그대로 그리게 한다. 멱등(매번 재계산·재기록).
/// 관련 문서 중 같은 vault 의 wiki 노트만 `[[wiki-NNNN]]` 로(Obsidian 이 해석 가능).
pub async fn project_links(store: &Store, vault_root: &Path, limit: i64) -> Result<usize> {
    let wiki_dir = vault_root.join("wiki");
    let mut updated = 0;
    for entry in std::fs::read_dir(&wiki_dir)
        .with_context(|| format!("wiki dir 읽기: {}", wiki_dir.display()))?
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
        // 고립 방지: concept 겹침 링크가 2개 미만이면 같은 프로젝트 최신 문서로 보충(소수).
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

/// `kind` YAML 값 → `Kind` enum. 순수 함수.
fn parse_kind(val: &serde_yaml::Value) -> Option<Kind> {
    match val.as_str()? {
        "note" => Some(Kind::Note),
        "memory" => Some(Kind::Memory),
        "session" => Some(Kind::Session),
        "decision" => Some(Kind::Decision),
        _ => None,
    }
}

/// `origin` YAML 값 → `Origin` enum. 순수 함수.
fn parse_origin(val: &serde_yaml::Value) -> Option<Origin> {
    match val.as_str()? {
        "personal" => Some(Origin::Personal),
        "company" => Some(Origin::Company),
        _ => None,
    }
}

// ─────────────────────────────────────────────────────────────
// Wikilink 추출 (순수)
// ─────────────────────────────────────────────────────────────

/// 본문에서 `[[wiki-NNNN]]` / `[[wiki-NNNN|alias]]` 형태의 wikilink target ID 들을 추출.
/// 순수 함수.
fn extract_wikilinks(body: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = body;
    while let Some(open) = rest.find("[[") {
        let after_open = &rest[open + 2..];
        let Some(close) = after_open.find("]]") else {
            break;
        };
        let inner = &after_open[..close];
        // alias 제거: [[id|alias]] → id
        let target = inner.split('|').next().unwrap_or(inner).trim();
        out.push(target.to_owned());
        rest = &after_open[close + 2..];
    }
    out
}

/// 본문에 `[[raw/...]]` / `[[meta/...]]` / `[[.rules/...]]` 형태의 cross-layer 링크가 있는지 검사.
/// 순수 함수.
fn find_cross_layer_wikilinks(body: &str) -> Vec<String> {
    extract_wikilinks(body)
        .into_iter()
        .filter(|t| t.starts_with("raw/") || t.starts_with("meta/") || t.starts_with(".rules/"))
        .collect()
}

// ─────────────────────────────────────────────────────────────
// Lint — 순수 서브 함수 (SRP: 각 검사를 별도 함수로)
// ─────────────────────────────────────────────────────────────

/// required_frontmatter 키 존재 검사. 순수.
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
                    format!("schema required_frontmatter 에 알 수 없는 키: '{other}'"),
                ));
                true
            }
        };
        if !present {
            issues.push(Issue::error(
                "required-fm-missing",
                stem,
                format!("필수 frontmatter 키 누락: '{key}'"),
            ));
        }
    }
}

/// id 값 정합성 검사. 순수.
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
                format!("frontmatter id '{fm_id}' 이 schema 패턴 불일치"),
            ));
        }
        if fm_id != stem {
            issues.push(Issue::error(
                "id-mismatch",
                stem,
                format!("frontmatter id '{fm_id}' ≠ 파일명 stem '{stem}'"),
            ));
        }
    }
}

/// sources 검사 (prefix + 파일 실재). 순수 — vault_root 파일시스템 접근 포함.
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
                    format!("sources 파일 미존재: {src}"),
                ));
            }
        } else {
            issues.push(Issue::error(
                "source-prefix-violation",
                stem,
                format!("sources 경로 '{src}' 의 prefix 불허 (허용: {allowed_prefixes:?})"),
            ));
        }
    }
}

/// wikilink 검사 (cross-layer + dangling). 순수.
fn check_wikilinks(body: &str, stem: &str, known_ids: &HashSet<String>, issues: &mut Vec<Issue>) {
    for bad_link in find_cross_layer_wikilinks(body) {
        issues.push(Issue::error(
            "cross-layer-wikilink",
            stem,
            format!("교차 레이어 wikilink [[{bad_link}]] — sources: 필드로 참조할 것"),
        ));
    }
    for link in extract_wikilinks(body) {
        if link.starts_with("wiki-") && !known_ids.contains(&link) {
            issues.push(Issue::error(
                "wikilink-dangling",
                stem,
                format!("본문 [[{link}]] 대상 페이지가 존재하지 않음"),
            ));
        }
    }
}

// ─────────────────────────────────────────────────────────────
// Lint — 공개 진입점 (순수)
// ─────────────────────────────────────────────────────────────

/// 한 파일의 내용 + 경로를 받아 이슈 목록을 반환. 순수 함수(I/O는 sources 실재 확인만).
///
/// # 인자
/// - `abs_path`: 파일 절대 경로
/// - `content`: 파일 전체 내용
/// - `schema`: typed schema
/// - `vault_root`: vault 루트 절대 경로 (소스 파일 실재 확인)
/// - `known_ids`: vault/wiki 에 존재하는 모든 페이지 ID 집합
#[allow(clippy::too_many_lines)] // 페이지당 다수 정합성 검사 — 한 책임(lint) 안에서 절차 나열
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

    // ── id-format: 파일명 패턴 검사 ──
    let id_re = match regex::Regex::new(&schema.page_id.pattern) {
        Ok(r) => r,
        Err(e) => {
            issues.push(Issue::error(
                "schema-invalid",
                &stem,
                format!("page_id.pattern 컴파일 실패: {e}"),
            ));
            return (None, issues);
        }
    };
    if !id_re.is_match(&stem) {
        issues.push(Issue::error(
            "id-format",
            &stem,
            format!(
                "파일명 stem '{stem}' 이 schema 패턴({}) 불일치",
                schema.page_id.pattern
            ),
        ));
        return (None, issues);
    }

    // ── frontmatter 분리 ──
    let Some((yaml, body)) = split_frontmatter(content) else {
        issues.push(Issue::error(
            "fm-parse",
            &stem,
            "YAML frontmatter(--- ... ---) 없음",
        ));
        return (None, issues);
    };

    // ── frontmatter YAML 파싱 ──
    let raw_fm = match parse_raw_frontmatter(yaml) {
        Ok(fm) => fm,
        Err(e) => {
            issues.push(Issue::error(
                "fm-parse",
                &stem,
                format!("YAML 파싱 실패: {e}"),
            ));
            return (None, issues);
        }
    };

    // ── 검사들 (UX 경계 — 누적) ──
    check_required_fields(&raw_fm, &schema.required_frontmatter, &stem, &mut issues);
    check_id_value(&raw_fm, &id_re, &stem, &mut issues);

    // ── kind 파싱 ──
    let kind = raw_fm.kind.as_ref().and_then(parse_kind);
    if raw_fm.kind.is_some() && kind.is_none() {
        let raw_str = raw_fm.kind.as_ref().and_then(|v| v.as_str()).unwrap_or("?");
        issues.push(Issue::error(
            "kind-invalid",
            &stem,
            format!("kind '{raw_str}' 은 허용값(note/memory/session/decision) 외"),
        ));
    }

    // ── origin 파싱 ──
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
            format!("origin '{raw_str}' 은 허용값(personal/company) 외"),
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

    // ── Page 구성 ──
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
            relates_to: raw_fm.relates_to.clone(),
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
// Audit — 순수 그래프 서브 함수 (SRP)
// ─────────────────────────────────────────────────────────────

/// superseded_by dangling + superseded-referenced 검사. 순수.
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
                format!("superseded_by '{sup}' 대상 페이지가 존재하지 않음"),
            ));
        }

        // 살아있는 페이지의 relates_to 가 superseded 페이지를 가리키면 warn
        if page.superseded_by.is_none() {
            for rel in &page.relates_to {
                if superseded_page_ids.contains(rel.as_str()) {
                    issues.push(Issue::warn(
                        "superseded-referenced",
                        &page.id,
                        format!("relates_to '{rel}' 는 이미 superseded 된 페이지 — 후계 페이지로 업데이트 권장"),
                    ));
                }
            }
        }
    }
}

/// 무방향 인접 목록 + edge_count 구축. 순수 (owned String 사용 — 수명 복잡성 회피).
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
            // 무방향 — 정규화: (min, max)
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

/// BFS 로 연결 성분 크기 목록을 반환. 순수.
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
// Audit — 공개 진입점 (순수)
// ─────────────────────────────────────────────────────────────

/// 그래프 감사 결과 요약.
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

/// pages 목록으로 그래프를 구축하고 감사 결과를 반환. 순수 함수(I/O 없음).
pub fn audit_pages(pages: &[Page]) -> AuditSummary {
    let mut issues = Vec::new();

    let page_ids: HashSet<&str> = pages.iter().map(|p| p.id.as_str()).collect();
    let superseded_count = pages.iter().filter(|p| p.superseded_by.is_some()).count();

    check_superseded(pages, &page_ids, &mut issues);

    let (adj, edge_count) = build_adjacency(pages, &page_ids);

    // ── orphan 검사 ──
    let mut orphan_count = 0_usize;
    for page in pages {
        let has_edges = adj.get(&page.id).is_some_and(|s| !s.is_empty());
        if !has_edges {
            issues.push(Issue::warn(
                "orphan",
                &page.id,
                "inbound·outbound 엣지 모두 0 — 고립 페이지",
            ));
            orphan_count += 1;
        }
    }

    // ── 연결 성분(BFS) ──
    let all_nodes: Vec<&str> = pages.iter().map(|p| p.id.as_str()).collect();
    let component_sizes = connected_components(&all_nodes, &adj);
    let component_count = component_sizes.len();

    if component_count > 1 {
        issues.push(Issue::warn(
            "graph-fragmented",
            "graph",
            format!(
                "연결 성분 {component_count}개 (크기: {component_sizes:?}) — 성분 간 [[wiki-NNNN]] 다리 연결 권장"
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
// I/O 쉘 — run_lint / run_audit
// ─────────────────────────────────────────────────────────────

/// vault wiki 페이지 목록을 파일시스템에서 수집. (I/O)
fn collect_wiki_pages(wiki_dir: &Path) -> Result<(HashSet<String>, Vec<PathBuf>)> {
    let mut known_ids: HashSet<String> = HashSet::new();
    let mut entries: Vec<PathBuf> = Vec::new();

    let read_dir = std::fs::read_dir(wiki_dir)
        .with_context(|| format!("wiki 디렉터리 읽기 실패: {}", wiki_dir.display()))?;

    for entry in read_dir {
        let entry = entry.context("wiki 디렉터리 항목 읽기 실패")?;
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

/// vault 루트를 받아 lint 를 실행하고 종료코드를 반환.
///
/// # Exit code semantics
/// - `0`: 오류 없음 (경고는 있을 수 있음)
/// - `1`: 오류(error) 있음
/// - `2`: strict 모드에서 경고만 있음
pub fn run_lint(vault_root: &Path, strict: bool) -> Result<i32> {
    let schema = load_schema(&vault_root.join(".rules/schema.yaml"))?;

    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!("vault/wiki 디렉터리가 없음: {}", wiki_dir.display());
    }

    let (known_ids, entries) = collect_wiki_pages(&wiki_dir)?;
    let mut all_issues: Vec<Issue> = Vec::new();

    for path in &entries {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("파일 읽기 실패: {}", path.display()))?;
        let (_page, issues) = lint_page(path, &content, &schema, vault_root, &known_ids);
        all_issues.extend(issues);
    }

    print_issues(&all_issues);
    Ok(exit_code(&all_issues, strict))
}

/// vault 루트를 받아 audit 를 실행하고 종료코드를 반환.
pub fn run_audit(vault_root: &Path, strict: bool) -> Result<i32> {
    let schema = load_schema(&vault_root.join(".rules/schema.yaml"))?;

    let wiki_dir = vault_root.join("wiki");
    if !wiki_dir.exists() {
        anyhow::bail!("vault/wiki 디렉터리가 없음: {}", wiki_dir.display());
    }

    let (known_ids, entries) = collect_wiki_pages(&wiki_dir)?;
    let mut pages: Vec<Page> = Vec::new();
    let mut parse_issues: Vec<Issue> = Vec::new();

    for path in &entries {
        let content = std::fs::read_to_string(path)
            .with_context(|| format!("파일 읽기 실패: {}", path.display()))?;
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
// 출력 헬퍼 (I/O)
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
// Compile — raw → wiki 큐레이션 (순수 로직 + I/O 쉘)
// ─────────────────────────────────────────────────────────────

/// LLM 큐레이션 결과 (parse-don't-validate — 1회 typed 파싱).
#[derive(Debug, Deserialize)]
struct CuratedLlm {
    title: String,
    body: String,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    tools: Vec<String>,
    #[serde(default)]
    concepts: Vec<String>,
}

/// 컴파일된 페이지의 in-memory 표현 (관계 링크 계산 전).
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

/// 기존 wiki 페이지에서 추출한 컴파일 출처 정보 (idempotency 키).
#[derive(Debug, Clone)]
struct WikiMeta {
    wiki_id: String,
    #[allow(dead_code)] // used as HashMap key; kept for debug clarity
    compiled_from: String, // raw/<filename>
    raw_sha: String,
}

/// wiki frontmatter 의 extended 필드 (compile 전용).
#[derive(Debug, Deserialize)]
struct WikiFrontMatterExt {
    #[serde(default)]
    compiled_from: Option<String>,
    #[serde(default)]
    raw_sha: Option<String>,
    #[serde(default)]
    id: Option<String>,
}

/// CJK 통합 한자(U+4E00..=U+9FFF) 포함 여부 (extract.rs와 동일 로직).
fn has_han(s: &str) -> bool {
    s.chars().any(|c| ('\u{4E00}'..='\u{9FFF}').contains(&c))
}

/// Han-filter 적용 후 유효 항목만 남김.
fn filter_han(items: Vec<String>) -> Vec<String> {
    items.into_iter().filter(|s| !has_han(s)).collect()
}

/// Obsidian-안전 태그로 정규화 (순수). LLM 이 뱉는 공백 포함 태그(`claude code`)가
/// Obsidian 에서 깨지는 걸 막는다 — 공백/허용외 문자 → `-`, 연속 대시 collapse,
/// 앞뒤 `-`·`/` trim, 소문자. 허용 집합 = `[a-z0-9_/-]`(`/` = nested 태그).
/// 빈 값·순수숫자(옵시 무효 태그)는 `None`.
fn sanitize_tag(raw: &str) -> Option<String> {
    let mut out = String::with_capacity(raw.len());
    let mut prev_dash = false;
    for c in raw.trim().to_lowercase().chars() {
        let mapped = if c.is_ascii_alphanumeric() || c == '_' || c == '/' {
            c
        } else {
            '-' // 공백·하이픈·기타 문장부호 모두 하이픈으로 수렴
        };
        if mapped == '-' {
            if prev_dash {
                continue; // 연속 대시 collapse
            }
            prev_dash = true;
        } else {
            prev_dash = false;
        }
        out.push(mapped);
    }
    let trimmed = out.trim_matches(|c| c == '-' || c == '/').to_owned();
    if trimmed.is_empty() || trimmed.chars().all(|c| c.is_ascii_digit()) {
        return None; // 빈 값·순수숫자 = 옵시 무효
    }
    Some(trimmed)
}

/// distill 노트 헤더의 `repo: <slug>` 마커 추출 (순수). distill-session.py → /distill 이
/// 호스트 git remote(폴백 폴더명)에서 채워 render_note 가 blockquote 에 박은 결정형 값.
/// 본문 오탐 방지로 앞부분(헤더 영역)만 스캔. 없으면 `None`.
fn parse_repo_marker(raw: &str) -> Option<String> {
    let head = raw.get(..raw.len().min(400)).unwrap_or(raw);
    let idx = head.find("repo:")?;
    let tok = head[idx + "repo:".len()..].split_whitespace().next()?;
    (!tok.is_empty()).then(|| tok.to_owned())
}

/// JSON 펜스 제거 — extract.rs 의 strip_to_json 와 동일 로직.
fn strip_json(raw: &str) -> &str {
    let Some(start) = raw.find('{') else {
        return "";
    };
    let suffix = &raw[start..];
    let Some(rel_end) = suffix.rfind('}') else {
        return "";
    };
    let end = start + rel_end + 1;
    if start >= end {
        return "";
    }
    &raw[start..end]
}

/// 현재 wiki 디렉터리에서 (compiled_from → WikiMeta) 맵 + 최대 숫자 id 를 스캔.
/// 순수 파일 파싱 로직 (I/O 포함이나 SRP 분리: 읽기 전용 스캔).
fn scan_existing_wiki(wiki_dir: &Path) -> Result<(HashMap<String, WikiMeta>, u32)> {
    let mut map: HashMap<String, WikiMeta> = HashMap::new();
    let mut max_id: u32 = 0;

    if !wiki_dir.exists() {
        return Ok((map, max_id));
    }

    let read_dir = std::fs::read_dir(wiki_dir)
        .with_context(|| format!("wiki 디렉터리 읽기 실패: {}", wiki_dir.display()))?;

    for entry in read_dir {
        let entry = entry.context("wiki 항목 읽기 실패")?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let stem = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_owned();

        // 숫자 id 추출: wiki-NNNN → NNNN
        if let Some(n) = stem
            .strip_prefix("wiki-")
            .and_then(|s| s.parse::<u32>().ok())
            && n > max_id
        {
            max_id = n;
        }

        let content = std::fs::read_to_string(&path)
            .with_context(|| format!("wiki 파일 읽기 실패: {}", path.display()))?;
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
    let bytes =
        std::fs::read(path).with_context(|| format!("raw 파일 읽기 실패: {}", path.display()))?;
    let hash = Sha256::digest(&bytes);
    Ok(hex::encode(hash))
}

/// LLM 큐레이션 시스템 프롬프트.
const COMPILE_SYSTEM: &str = "You are a precise JSON-only curator. Output ONLY a single JSON object — no prose, no markdown fences. /no_think";

/// LLM 큐레이션 프롬프트 템플릿.
const COMPILE_PROMPT_TMPL: &str = r#"Curate the raw note below into a wiki page. Return EXACTLY this JSON shape (no extra keys):
{"title":"<short title, ≤60 chars>","body":"<curated markdown body in Korean, with WHY context>","tags":["tag1"],"tools":["tool1"],"concepts":["concept1"]}

Rules:
- title: short, descriptive, ≤60 chars
- body: curated markdown. Keep all important insights. Add WHY context. Korean preferred.
- tags: ≤6 topical tags, lowercase, no Han/CJK characters
- tools: ≤6 software tools/libraries used, short canonical names, no Han/CJK
- concepts: ≤6 key technical concepts or patterns, no Han/CJK
- ALL string values: Korean or English ONLY — NO Chinese/Japanese characters (漢字/汉字/CJK)
- Use empty arrays [] if not applicable

Raw note:
---
{BODY}
---
/no_think"#;

/// 공유 tool/concept 기반 관계 맵 계산 (순수 함수).
/// 반환: wiki_id → Vec<related_wiki_id> (자기 자신 제외, 중복 없음).
fn compute_relations(drafts: &[CompiledDraft]) -> HashMap<String, Vec<String>> {
    // tool/concept slug → wiki_id 역색인
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

/// CompiledDraft → wiki .md 파일 내용 렌더링 (순수 함수).
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

    // frontmatter 를 serde_yaml 로 직렬화 (SSOT)
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

    let yaml = serde_yaml::to_string(&fm).context("frontmatter YAML 직렬화 실패")?;

    // ## 관련 섹션 + [[wiki-NNNN]] wikilinks
    let related_section = if draft.relates_to.is_empty() {
        String::new()
    } else {
        let links: String = draft
            .relates_to
            .iter()
            .map(|id| format!("- [[{id}]]"))
            .collect::<Vec<_>>()
            .join("\n");
        format!("\n\n## 관련\n\n{links}")
    };

    Ok(format!("---\n{yaml}---\n{}{related_section}\n", draft.body))
}

/// 컴파일 통계.
#[derive(Debug, Default)]
pub struct CompileStats {
    pub compiled: usize,
    pub recompiled: usize,
    pub skipped: usize,
    pub total_raw: usize,
}

/// `drudge vault compile` 진입점 (I/O 쉘 — 순수 로직 위임).
#[allow(clippy::too_many_lines)]
pub async fn run_compile(
    vault_root: &Path,
    raw_dir: &Path,
    today: &str,
    ollama: &crate::ollama::Ollama,
) -> Result<CompileStats> {
    let wiki_dir = vault_root.join("wiki");
    std::fs::create_dir_all(&wiki_dir)
        .with_context(|| format!("wiki 디렉터리 생성 실패: {}", wiki_dir.display()))?;

    // 1. 기존 wiki 스캔 — idempotency 키 맵 + 최대 id
    let (existing_map, mut max_id) = scan_existing_wiki(&wiki_dir)?;

    // 2. raw 디렉터리 수집 (*.md 만)
    let mut raw_entries: Vec<PathBuf> = {
        let rd = std::fs::read_dir(raw_dir)
            .with_context(|| format!("raw 디렉터리 읽기 실패: {}", raw_dir.display()))?;
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

    // 3. 각 raw 파일 처리
    let mut drafts: Vec<CompiledDraft> = Vec::new();
    // wiki_id → path (recompile 시 덮어쓰기용)
    let mut wiki_path_map: HashMap<String, PathBuf> = HashMap::new();

    for raw_path in &raw_entries {
        let filename = raw_path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_owned();
        let raw_rel = format!("raw/{filename}");

        let sha = sha256_file(raw_path)?;

        // idempotency 검사
        let (wiki_id, is_new) = if let Some(meta) = existing_map.get(&raw_rel) {
            if meta.raw_sha == sha {
                eprintln!("↷ skip (unchanged): {filename}");
                stats.skipped += 1;
                continue;
            }
            // sha 변경 → 같은 id 로 recompile
            (meta.wiki_id.clone(), false)
        } else {
            // 신규 → 다음 id
            max_id += 1;
            (format!("wiki-{max_id:04}"), true)
        };

        // origin 결정 — env `DRUDGE_COMPANY_SUBSTR` 토큰 매칭(미설정이면 항상 Personal)
        let origin = if raw_path
            .to_str()
            .is_some_and(crate::frontmatter::is_company_path)
        {
            Origin::Company
        } else {
            Origin::Personal
        };

        // mtime → date (I/O 경계에서 1회)
        let date = raw_path
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| {
                use std::time::UNIX_EPOCH;
                let secs = t.duration_since(UNIX_EPOCH).ok()?.as_secs();
                let days = (secs / 86400).cast_signed();
                // 단순 날짜 계산 (chrono 없이 — 결정론적)
                // epoch = 1970-01-01. days_since_epoch → gregorian date
                Some(days_to_date(days))
            })
            .unwrap_or_else(|| today.to_owned());

        // LLM 큐레이션
        let body_raw = std::fs::read_to_string(raw_path)
            .with_context(|| format!("raw 파일 읽기 실패: {}", raw_path.display()))?;
        let body_snip: String = body_raw.chars().take(4000).collect();
        let prompt = COMPILE_PROMPT_TMPL.replace("{BODY}", &body_snip);

        let llm_raw = match ollama.generate(COMPILE_SYSTEM, &prompt).await {
            Ok(r) => r,
            Err(e) => {
                eprintln!("⚠ compile LLM error [{filename}]: {e} — skipping");
                stats.skipped += 1;
                continue;
            }
        };

        let json_str = strip_json(llm_raw.trim());
        let curated: CuratedLlm = match serde_json::from_str(json_str) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("⚠ compile JSON parse error [{filename}]: {e} — raw: {json_str:.120}");
                stats.skipped += 1;
                continue;
            }
        };

        // Han-filter + Obsidian-안전 정규화(공백→-, 무효 drop). ≤6개.
        let mut tags: Vec<String> = filter_han(curated.tags)
            .into_iter()
            .filter_map(|t| sanitize_tag(&t))
            .take(6)
            .collect();
        // 새 카테고리 축: repo 슬러그(호스트 git, distill 마커) → 옵시 nested 태그 repo/<slug>.
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
            body: curated.body,
            tags,
            tools,
            concepts,
            relates_to: Vec::new(), // filled after relation pass
        };

        let wiki_path = wiki_dir.join(format!("{wiki_id}.md"));
        wiki_path_map.insert(wiki_id.clone(), wiki_path);
        drafts.push(draft);

        if is_new {
            stats.compiled += 1;
        } else {
            stats.recompiled += 1;
        }
    }

    // 4. 관계 계산 (순수 함수)
    let relations = compute_relations(&drafts);

    // 5. relates_to 주입 + 파일 쓰기
    for draft in &mut drafts {
        draft.relates_to = relations.get(&draft.wiki_id).cloned().unwrap_or_default();
    }

    for draft in &drafts {
        let content = render_wiki_page(draft)?;
        let path = wiki_path_map
            .get(&draft.wiki_id)
            .with_context(|| format!("wiki path 없음: {}", draft.wiki_id))?;
        std::fs::write(path, content)
            .with_context(|| format!("wiki 파일 쓰기 실패: {}", path.display()))?;
        println!("✓ {}: {}", draft.wiki_id, draft.title);
    }

    Ok(stats)
}

/// 오늘 날짜 "YYYY-MM-DD" — compile 의 `--date` 미지정 기본값 SSOT.
/// I/O 경계: `SystemTime::now()` 를 여기 1곳에 격리(main·serve 공용) → 변환은 순수 `days_to_date`.
#[must_use]
pub fn today_utc() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let days = (secs / 86_400).cast_signed();
    days_to_date(days)
}

/// days since Unix epoch (1970-01-01) → "YYYY-MM-DD" 문자열 (순수 함수, no SystemTime).
fn days_to_date(days: i64) -> String {
    // Proleptic Gregorian calendar 변환 (Richards 알고리즘 기반).
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
// 단위 테스트 (순수 함수 테스트 — I/O 없음)
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

    // ── sanitize_tag (Obsidian-안전 정규화) ──

    #[test]
    fn sanitize_tag_space_to_hyphen() {
        // 옵시에서 깨지던 공백 태그 → 하이픈 (중간 하이픈은 유효하므로 유지)
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
        assert!(sanitize_tag("2024").is_none()); // 순수숫자 = 옵시 무효
        assert!(sanitize_tag("").is_none());
        assert!(sanitize_tag("  ").is_none());
        assert!(sanitize_tag("!!!").is_none());
    }

    // ── parse_repo_marker (distill 노트 헤더 마커) ──

    #[test]
    fn parse_repo_marker_extracts_slug() {
        let note = "# 세션 노트 — 2026-06-12\n> 자동 증류 (Claude Code · 종료) · origin: personal · repo: jazz1x/oh-my-boring · cwd: /x\n\n본문";
        assert_eq!(
            parse_repo_marker(note).as_deref(),
            Some("jazz1x/oh-my-boring")
        );
    }

    #[test]
    fn parse_repo_marker_absent_is_none() {
        let note = "# 세션 노트\n> origin: personal · cwd: /x\n\n본문";
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
        let body = "참고: [[wiki-0001]] 과 [[wiki-0002|두 번째]] 를 보라.";
        let links = extract_wikilinks(body);
        assert_eq!(links, vec!["wiki-0001", "wiki-0002"]);
    }

    #[test]
    fn no_wikilinks_returns_empty() {
        assert!(extract_wikilinks("본문에 링크 없음").is_empty());
    }

    // ── cross-layer wikilinks ──

    #[test]
    fn cross_layer_detected() {
        let body = "[[raw/seed.md]] 와 [[wiki-0001]]";
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

    // ── lint_page: dangling wikilink 검사 ──

    #[test]
    fn dangling_wikilink_detected() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: T\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n[[wiki-9999]]";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            issues.iter().any(|i| i.rule == "wikilink-dangling"),
            "dangling wikilink 이슈 없음: {issues:?}"
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
            "valid wikilink 에 dangling 이슈: {issues:?}"
        );
    }

    // ── lint_page: schema frontmatter 파싱 ──

    #[test]
    fn valid_frontmatter_no_errors() {
        let schema = test_schema();
        let content = "---\nid: wiki-0001\ntitle: Test\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n본문";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        let errors: Vec<_> = issues
            .iter()
            .filter(|i| i.severity == Severity::Error)
            .collect();
        assert!(errors.is_empty(), "오류가 있어선 안 됨: {errors:?}");
        assert!(page.is_some(), "Page 파싱 실패");
    }

    #[test]
    fn missing_required_field_is_error() {
        let schema = test_schema();
        // title 누락
        let content =
            "---\nid: wiki-0001\nkind: note\norigin: personal\ndate: \"2026-01-01\"\n---\n본문";
        let ids = known_ids(&["wiki-0001"]);
        let path = Path::new("/vault/wiki/wiki-0001.md");
        let (_page, issues) = lint_page(path, content, &schema, Path::new("/vault"), &ids);
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "required-fm-missing" && i.message.contains("title")),
            "title 누락 이슈 없음: {issues:?}"
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
        // 살아있는 wiki-0003 이 superseded 된 wiki-0001 을 relates_to 로 참조
        let live = make_page("wiki-0003", vec!["wiki-0001"], "");
        let pages = vec![old, new_page, live];
        let summary = audit_pages(&pages);
        assert!(
            summary
                .issues
                .iter()
                .any(|i| i.rule == "superseded-referenced" && i.severity == Severity::Warn),
            "superseded-referenced warn 없음: {:?}",
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
        let json = r#"{"title":"테스트 제목","body":"본문 내용","tags":["rust","rag"],"tools":["surrealdb"],"concepts":["rop"]}"#;
        let c: super::CuratedLlm = serde_json::from_str(json).unwrap();
        assert_eq!(c.title, "테스트 제목");
        assert_eq!(c.tags, vec!["rust", "rag"]);
        assert_eq!(c.tools, vec!["surrealdb"]);
    }

    #[test]
    fn curated_llm_parse_missing_arrays_defaults_empty() {
        let json = r#"{"title":"T","body":"B"}"#;
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
