//! Explicit, syntax-only source-code corpus.
//!
//! This module is isolated from the vault/wiki memory model. It owns only PostgreSQL's
//! `code_index` schema and never creates cross-corpus edges.

mod parser;
mod store;

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use walkdir::WalkDir;

use crate::config::{CodeIndexSource, CodeLanguage};

pub use store::CodeIndexStore;

#[derive(Debug, Error)]
pub enum CodeIndexError {
    #[error("code index root metadata failed for {path}: {source}")]
    RootMetadata { path: PathBuf, source: io::Error },
    #[error("code index root is not a directory: {0}")]
    RootNotDirectory(PathBuf),
    #[error("code index walk failed: {0}")]
    Walk(#[from] walkdir::Error),
    #[error("code index path is outside configured root: {0}")]
    OutsideRoot(PathBuf),
    #[error("code index path is not valid UTF-8: {0}")]
    NonUtf8Path(PathBuf),
    #[error("read code index source {path}: {source}")]
    Read { path: PathBuf, source: io::Error },
    #[error("Tree-sitter language setup failed: {0}")]
    Language(#[from] tree_sitter::LanguageError),
    #[error("Tree-sitter parsing was cancelled")]
    ParserCancelled,
    #[error("numeric value cannot be represented by PostgreSQL bigint")]
    NumericOverflow,
    #[error("PostgreSQL code index operation failed: {0}")]
    Database(#[from] tokio_postgres::Error),
    #[error("code index connection pool failed: {0}")]
    Pool(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ParseStatus {
    Parsed,
    ParsedWithErrors,
}

impl ParseStatus {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Parsed => "parsed",
            Self::ParsedWithErrors => "parsed-with-errors",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SymbolKind {
    Function,
    Struct,
    Enum,
    Union,
    Trait,
    TypeAlias,
    Constant,
    Static,
    Module,
    Macro,
}

impl SymbolKind {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Function => "function",
            Self::Struct => "struct",
            Self::Enum => "enum",
            Self::Union => "union",
            Self::Trait => "trait",
            Self::TypeAlias => "type-alias",
            Self::Constant => "constant",
            Self::Static => "static",
            Self::Module => "module",
            Self::Macro => "macro",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RelationKind {
    Contains,
    Imports,
    Calls,
    References,
}

impl RelationKind {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Contains => "contains",
            Self::Imports => "imports",
            Self::Calls => "calls",
            Self::References => "references",
        }
    }
}

#[derive(Debug)]
struct Symbol {
    id: String,
    kind: SymbolKind,
    name: String,
    qualified_name: String,
    start_byte: usize,
    end_byte: usize,
    start_line: usize,
    end_line: usize,
}

#[derive(Debug)]
struct Relation {
    id: String,
    source_symbol_id: Option<String>,
    kind: RelationKind,
    target_symbol_id: Option<String>,
    target_name: Option<String>,
    start_byte: usize,
    end_byte: usize,
}

#[derive(Debug)]
struct ParsedFile {
    status: ParseStatus,
    error_count: usize,
    symbols: Vec<Symbol>,
    relations: Vec<Relation>,
}

#[derive(Debug)]
struct CollectedFile {
    id: String,
    relative_path: String,
    sha256: String,
    content: String,
}

#[derive(Debug)]
struct PreparedFile {
    collected: CollectedFile,
    parsed: ParsedFile,
}

#[derive(Debug, PartialEq, Eq)]
pub struct SyncReport {
    pub repository_id: String,
    pub scanned: usize,
    pub changed: usize,
    pub unchanged: usize,
    pub deleted: usize,
    pub parse_errors: usize,
}

#[derive(Debug, Serialize)]
pub struct RepositoryStatus {
    pub repository_id: String,
    pub name: String,
    pub root_path: String,
    pub language: String,
    pub last_synced_at: SystemTime,
    pub files: usize,
    pub symbols: usize,
    pub relations: usize,
    pub files_with_errors: usize,
    pub parse_errors: usize,
}

#[derive(Debug, Serialize)]
pub struct CodeSearchHit {
    pub id: String,
    pub repository_id: String,
    pub relative_path: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub start_line: usize,
    pub end_line: usize,
}

#[derive(Debug, Serialize)]
pub struct CodeRelation {
    pub kind: String,
    pub target_symbol_id: Option<String>,
    pub target_name: Option<String>,
    pub start_byte: usize,
    pub end_byte: usize,
}

#[derive(Debug, Serialize)]
pub struct CodeSymbolDetail {
    pub symbol: CodeSearchHit,
    pub relations: Vec<CodeRelation>,
}

/// Synchronize one configured repository. Collection and changed-file parsing finish before the
/// transaction begins; therefore a missing/unreadable root cannot cause replacement or pruning.
pub async fn sync_repository(
    store: &mut CodeIndexStore,
    source: &CodeIndexSource,
) -> Result<SyncReport, CodeIndexError> {
    let collected = collect_repository(source)?;
    let seen_paths: Vec<String> = collected
        .iter()
        .map(|file| file.relative_path.clone())
        .collect();
    let hashes = store.existing_hashes(source.id()).await?;
    let changed = prepare_changed(source, collected, &hashes)?;
    store
        .replace_repository(source, &seen_paths, &changed)
        .await
}

fn collect_repository(source: &CodeIndexSource) -> Result<Vec<CollectedFile>, CodeIndexError> {
    let metadata = fs::metadata(source.root()).map_err(|error| CodeIndexError::RootMetadata {
        path: source.root().to_path_buf(),
        source: error,
    })?;
    if !metadata.is_dir() {
        return Err(CodeIndexError::RootNotDirectory(
            source.root().to_path_buf(),
        ));
    }

    let mut files = Vec::new();
    let walker = WalkDir::new(source.root())
        .follow_links(false)
        .into_iter()
        .filter_entry(|entry| !is_ignored_directory(entry.path(), entry.file_type().is_dir()));
    for entry in walker {
        let entry = entry?;
        if !entry.file_type().is_file() || !matches_language(entry.path(), source.language()) {
            continue;
        }
        let relative = entry
            .path()
            .strip_prefix(source.root())
            .map_err(|_| CodeIndexError::OutsideRoot(entry.path().to_path_buf()))?;
        let relative_path = relative
            .to_str()
            .ok_or_else(|| CodeIndexError::NonUtf8Path(relative.to_path_buf()))?
            .replace('\\', "/");
        let content = fs::read_to_string(entry.path()).map_err(|error| CodeIndexError::Read {
            path: entry.path().to_path_buf(),
            source: error,
        })?;
        files.push(CollectedFile {
            id: stable_id(&["file", source.id(), &relative_path]),
            relative_path,
            sha256: content_sha(&content),
            content,
        });
    }
    files.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
    Ok(files)
}

fn prepare_changed(
    source: &CodeIndexSource,
    collected: Vec<CollectedFile>,
    hashes: &HashMap<String, String>,
) -> Result<Vec<PreparedFile>, CodeIndexError> {
    collected
        .into_iter()
        .filter(|file| hashes.get(&file.relative_path) != Some(&file.sha256))
        .map(|file| {
            let parsed = match source.language() {
                CodeLanguage::Rust => {
                    parser::parse_rust(source.id(), &file.relative_path, &file.id, &file.content)?
                }
                CodeLanguage::Python => {
                    parser::parse_python(source.id(), &file.relative_path, &file.id, &file.content)?
                }
                CodeLanguage::Shell => {
                    parser::parse_shell(source.id(), &file.relative_path, &file.id, &file.content)?
                }
            };
            Ok(PreparedFile {
                collected: file,
                parsed,
            })
        })
        .collect()
}

fn matches_language(path: &Path, language: CodeLanguage) -> bool {
    match language {
        CodeLanguage::Rust => path.extension().is_some_and(|extension| extension == "rs"),
        CodeLanguage::Python => path.extension().is_some_and(|extension| extension == "py"),
        CodeLanguage::Shell => path
            .extension()
            .is_some_and(|extension| extension == "sh" || extension == "bash"),
    }
}

fn is_ignored_directory(path: &Path, is_directory: bool) -> bool {
    is_directory
        && path
            .file_name()
            .is_some_and(|name| name == ".git" || name == "target" || name == "node_modules")
}

fn content_sha(content: &str) -> String {
    hex::encode(Sha256::digest(content.as_bytes()))
}

/// One syntax symbol as the D2 anchor-hash layer needs it: its identity, its line span, and the
/// exact body text. Rows are 0-based (tree-sitter convention), matching the code_index.symbol
/// columns.
#[derive(Debug)]
pub struct ParsedSymbol {
    pub qualified_name: String,
    pub start_row: usize,
    pub end_row: usize,
    pub body: String,
}

/// Parse `content` with the source's own parser — the same one the indexer runs — so the anchor
/// hash covers exactly the symbol spans code_index would store. Used by the hash layer to read a
/// symbol body from disk without a code sync in between; never writes the code_index schema.
pub fn parse_symbols(
    source: &CodeIndexSource,
    path_for_ids: &str,
    content: &str,
) -> Result<Vec<ParsedSymbol>, CodeIndexError> {
    let file_id = stable_id(&["file", source.id(), path_for_ids]);
    let parsed = match source.language() {
        CodeLanguage::Rust => parser::parse_rust(source.id(), path_for_ids, &file_id, content)?,
        CodeLanguage::Python => parser::parse_python(source.id(), path_for_ids, &file_id, content)?,
        CodeLanguage::Shell => parser::parse_shell(source.id(), path_for_ids, &file_id, content)?,
    };
    Ok(parsed
        .symbols
        .into_iter()
        .map(|symbol| ParsedSymbol {
            qualified_name: symbol.qualified_name,
            start_row: symbol.start_line,
            end_row: symbol.end_line,
            // Tree-sitter byte offsets are char boundaries of the same `content` — indexing is exact.
            body: content[symbol.start_byte..symbol.end_byte].to_owned(),
        })
        .collect())
}

fn stable_id(parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part.as_bytes());
        hasher.update([0]);
    }
    hex::encode(hasher.finalize())
}

fn usize_to_i64(value: usize) -> Result<i64, CodeIndexError> {
    i64::try_from(value).map_err(|_| CodeIndexError::NumericOverflow)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use std::collections::HashMap;
    use std::fs;

    use tempfile::tempdir;

    use super::{collect_repository, prepare_changed, stable_id};
    use crate::config::{CodeIndexSource, CodeLanguage};

    #[test]
    fn collection_is_deterministic_and_hash_skip_parses_only_changed_files() {
        let root = tempdir().unwrap();
        fs::create_dir(root.path().join("src")).unwrap();
        fs::write(root.path().join("src/lib.rs"), "fn alpha() {}\n").unwrap();
        fs::write(root.path().join("README.md"), "not code").unwrap();
        let source = CodeIndexSource::new(
            "repo",
            "Repo",
            root.path().to_path_buf(),
            CodeLanguage::Rust,
            true,
        )
        .unwrap();
        let collected = collect_repository(&source).unwrap();
        assert_eq!(collected.len(), 1);
        assert_eq!(collected[0].relative_path, "src/lib.rs");
        assert_eq!(collected[0].id, stable_id(&["file", "repo", "src/lib.rs"]));

        let hashes = HashMap::from([(
            collected[0].relative_path.clone(),
            collected[0].sha256.clone(),
        )]);
        assert!(
            prepare_changed(&source, collected, &hashes)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn missing_root_is_a_collection_error() {
        let source = CodeIndexSource::new(
            "missing",
            "Missing",
            PathBuf::from("/definitely/missing/code-index-root"),
            CodeLanguage::Rust,
            true,
        )
        .unwrap();
        assert!(collect_repository(&source).is_err());
    }

    use std::path::PathBuf;
}
