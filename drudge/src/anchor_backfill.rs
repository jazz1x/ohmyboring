//! `drudge anchor-backfill`: fill `anchor_hash`/`anchor_symbol` of open claims that were written
//! without one (the Python write path always writes NULL), so the scheduler's stale check has rows
//! to look at.
//!
//! A hash taken from today's file says "unchanged" for a claim whose file was edited after the
//! claim was made. So a row is hashed only when the file's last commit (or mtime, outside git) is
//! not later than the claim's `valid_from`, and the file has no uncommitted change. Everything else
//! is counted as "not measurable" with its reason.
//!
//! Layers: `judge` is pure; `gather_evidence` and `run` hold the git/file/DB effects.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;
use std::process::Command;
use std::time::SystemTime;

use anyhow::Result;
use chrono::DateTime;

use crate::anchor::Anchor;
use crate::anchor_hash::{AnchorHash, hash_content, read_under_root, registered_source};
use crate::config::CodeIndexSource;
use crate::store::{Store, UnpinnedAnchorRow};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Unmeasured {
    AnchorUnparsed,
    NoSource,
    FileMissing,
    NameOnly,
    NoParser,
    ChangedAfterClaim,
    Uncommitted,
    GitFailed,
    GoneBeforePin,
}

impl Unmeasured {
    const fn label(self) -> &'static str {
        match self {
            Self::AnchorUnparsed => "anchor 문자열 해석 실패",
            Self::NoSource => "등록 소스 없음",
            Self::FileMissing => "파일 없음",
            Self::NameOnly => "파일 이름만 적힘(범위 밖)",
            Self::NoParser => "언어 파서 없음·해석 실패",
            Self::ChangedAfterClaim => "git/파일 시각이 valid_from 뒤",
            Self::Uncommitted => "커밋 안 된 변경 있음",
            Self::GitFailed => "git 실패",
            Self::GoneBeforePin => "쓰기 전에 대체·stale 됨",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockBasis {
    Git,
    Mtime,
}

/// When the file last changed, as far as git (or, outside git, the filesystem) can tell.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FileClock {
    Committed(SystemTime),
    Outside(SystemTime),
    Uncommitted,
    GitFailed,
}

pub enum Evidence<'a> {
    NoSource,
    FileAbsent,
    Present {
        source: &'a CodeIndexSource,
        content: String,
        clock: FileClock,
    },
}

#[derive(Debug)]
pub enum Judgement {
    Hashed { hash: AnchorHash, basis: ClockBasis },
    Unmeasured(Unmeasured),
}

#[must_use]
pub fn judge(anchor: &Anchor, valid_from: SystemTime, evidence: &Evidence<'_>) -> Judgement {
    let (source, content, clock) = match evidence {
        Evidence::NoSource => return Judgement::Unmeasured(Unmeasured::NoSource),
        Evidence::FileAbsent if anchor.path.contains('/') => {
            return Judgement::Unmeasured(Unmeasured::FileMissing);
        }
        Evidence::FileAbsent => return Judgement::Unmeasured(Unmeasured::NameOnly),
        Evidence::Present {
            source,
            content,
            clock,
        } => (source, content, clock),
    };
    let Some(hash) = hash_content(source, anchor, content) else {
        return Judgement::Unmeasured(Unmeasured::NoParser);
    };
    let (changed_at, basis) = match clock {
        FileClock::GitFailed => return Judgement::Unmeasured(Unmeasured::GitFailed),
        FileClock::Uncommitted => return Judgement::Unmeasured(Unmeasured::Uncommitted),
        FileClock::Committed(at) => (*at, ClockBasis::Git),
        FileClock::Outside(at) => (*at, ClockBasis::Mtime),
    };
    if changed_at > valid_from {
        return Judgement::Unmeasured(Unmeasured::ChangedAfterClaim);
    }
    Judgement::Hashed { hash, basis }
}

#[derive(Debug, Default)]
pub struct Tally {
    pub open_anchors: usize,
    pub hashed_git: usize,
    pub hashed_mtime: usize,
    pub unmeasured: BTreeMap<Unmeasured, usize>,
}

impl Tally {
    fn count(&mut self, judgement: &Judgement) {
        self.open_anchors += 1;
        match judgement {
            Judgement::Hashed {
                basis: ClockBasis::Git,
                ..
            } => self.hashed_git += 1,
            Judgement::Hashed {
                basis: ClockBasis::Mtime,
                ..
            } => self.hashed_mtime += 1,
            Judgement::Unmeasured(reason) => *self.unmeasured.entry(*reason).or_default() += 1,
        }
    }

    fn of(&self, reason: Unmeasured) -> usize {
        self.unmeasured.get(&reason).copied().unwrap_or(0)
    }

    #[must_use]
    pub fn hashed(&self) -> usize {
        self.hashed_git + self.hashed_mtime
    }

    /// The chain: open anchor -> registered source -> file present -> hash n + unmeasured k.
    #[must_use]
    pub fn render(&self, dry_run: bool) -> String {
        let with_source =
            self.open_anchors - self.of(Unmeasured::AnchorUnparsed) - self.of(Unmeasured::NoSource);
        let file_missing = self.of(Unmeasured::FileMissing) + self.of(Unmeasured::NameOnly);
        let file_present = with_source - file_missing;
        // k is summed from its own reason columns, not taken as file_present - n, so the
        // check line below can disagree when a reason is counted on the wrong side.
        let k: usize = [
            Unmeasured::NoParser,
            Unmeasured::ChangedAfterClaim,
            Unmeasured::Uncommitted,
            Unmeasured::GitFailed,
            Unmeasured::GoneBeforePin,
        ]
        .into_iter()
        .map(|reason| self.of(reason))
        .sum();
        let agrees = if self.hashed() + k == file_present {
            "일치"
        } else {
            "어긋남"
        };
        let mut out = String::new();
        let mode = if dry_run {
            " (dry-run: 쓰지 않음)"
        } else {
            ""
        };
        let _ = writeln!(out, "anchor-backfill{mode}");
        let _ = writeln!(
            out,
            "열린 anchor {} → 등록 소스 {} → 파일 있음 {} → 해시 n={} + 못 잼 k={}",
            self.open_anchors,
            with_source,
            file_present,
            self.hashed(),
            k
        );
        let _ = writeln!(
            out,
            "대조 n+k={} 대 파일 있음 {file_present}: {agrees}",
            self.hashed() + k
        );
        for (reason, n) in &self.unmeasured {
            let _ = writeln!(out, "  못 잼 사유: {} = {n}", reason.label());
        }
        let _ = writeln!(out, "시각 기준 mtime n={}", self.hashed_mtime);
        let _ = write!(out, "시각 기준 git n={}", self.hashed_git);
        out
    }
}

fn git_output(root: &Path, args: &[&str]) -> Option<std::process::Output> {
    Command::new("git")
        .env("LC_ALL", "C")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .ok()
}

fn stdout_text(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).trim().to_owned()
}

/// What `git rev-parse --is-inside-work-tree` said about the root.
#[derive(Debug, PartialEq, Eq)]
enum WorkTree {
    Inside,
    NotARepository,
    Failed,
}

/// git exits 128 both outside a repository and for e.g. a root owned by another uid; only the
/// first may fall back to mtime, the rest is "git failed" (`git_output` pins `LC_ALL=C`).
fn read_rev_parse(succeeded: bool, stderr: &str) -> WorkTree {
    match (succeeded, stderr.contains("not a git repository")) {
        (true, _) => WorkTree::Inside,
        (false, true) => WorkTree::NotARepository,
        (false, false) => WorkTree::Failed,
    }
}

fn file_clock(root: &Path, path: &str) -> FileClock {
    let Some(inside) = git_output(root, &["rev-parse", "--is-inside-work-tree"]) else {
        return FileClock::GitFailed;
    };
    match read_rev_parse(
        inside.status.success(),
        &String::from_utf8_lossy(&inside.stderr),
    ) {
        WorkTree::Inside => {}
        WorkTree::Failed => return FileClock::GitFailed,
        WorkTree::NotARepository => {
            return std::fs::metadata(root.join(path))
                .and_then(|meta| meta.modified())
                .map_or(FileClock::GitFailed, FileClock::Outside);
        }
    }
    let Some(status) = git_output(root, &["status", "--porcelain", "--", path]) else {
        return FileClock::GitFailed;
    };
    if !status.status.success() {
        return FileClock::GitFailed;
    }
    if !stdout_text(&status).is_empty() {
        return FileClock::Uncommitted;
    }
    let Some(log) = git_output(root, &["log", "-1", "--format=%cI", "--", path]) else {
        return FileClock::GitFailed;
    };
    if !log.status.success() {
        return FileClock::GitFailed;
    }
    let committed = stdout_text(&log);
    if committed.is_empty() {
        // Clean and unlogged: git ignores the file, so only the filesystem knows.
        return std::fs::metadata(root.join(path))
            .and_then(|meta| meta.modified())
            .map_or(FileClock::GitFailed, FileClock::Outside);
    }
    DateTime::parse_from_rfc3339(&committed)
        .map_or(FileClock::GitFailed, |at| FileClock::Committed(at.into()))
}

fn gather_evidence<'a>(anchor: &Anchor, sources: &'a [CodeIndexSource]) -> Evidence<'a> {
    let Some(source) = registered_source(anchor, sources) else {
        return Evidence::NoSource;
    };
    let Some(content) = read_under_root(source, &anchor.path) else {
        return Evidence::FileAbsent;
    };
    let clock = file_clock(source.root(), &anchor.path);
    Evidence::Present {
        source,
        content,
        clock,
    }
}

/// One line per distinct source root: where the hashes and git times were read from.
#[must_use]
pub fn root_line(sources: &[CodeIndexSource]) -> String {
    let mut roots: Vec<&Path> = sources
        .iter()
        .filter(|source| source.enabled())
        .map(CodeIndexSource::root)
        .collect();
    roots.sort();
    roots.dedup();
    let describe = |root: &Path| {
        let head = |args: &[&str]| {
            git_output(root, args)
                .filter(|output| output.status.success())
                .map_or_else(|| "?".to_owned(), |output| stdout_text(&output))
        };
        format!(
            "{} branch={} sha={}",
            root.display(),
            head(&["rev-parse", "--abbrev-ref", "HEAD"]),
            head(&["rev-parse", "--short", "HEAD"])
        )
    };
    let described: Vec<String> = roots.into_iter().map(describe).collect();
    format!("소스 루트: {}", described.join(" | "))
}

/// Judge every open anchored claim without a hash and, unless `dry_run`, pin the hashes.
///
/// # Errors
/// Reading the rows or one pin statement failed.
pub async fn run(store: &Store, sources: &[CodeIndexSource], dry_run: bool) -> Result<Tally> {
    let rows = store.claims_without_anchor_hash().await?;
    let mut tally = Tally::default();
    for row in &rows {
        let judgement = match (dry_run, judge_row(row, sources)) {
            (false, Judgement::Hashed { hash, basis }) => {
                let pinned = store
                    .pin_anchor_hash(
                        &row.subject,
                        &row.predicate,
                        row.valid_from,
                        &hash.hash,
                        &hash.symbol,
                    )
                    .await?;
                settle_pin(Judgement::Hashed { hash, basis }, pinned)
            }
            (_, judgement) => judgement,
        };
        tally.count(&judgement);
    }
    Ok(tally)
}

/// A hashed row counts toward n only when the pin statement changed a row: one replaced or
/// marked stale between the read and the write is left as it was.
fn settle_pin(judgement: Judgement, pinned_rows: u64) -> Judgement {
    match (judgement, pinned_rows) {
        (Judgement::Hashed { .. }, 0) => Judgement::Unmeasured(Unmeasured::GoneBeforePin),
        (judgement, _) => judgement,
    }
}

fn judge_row(row: &UnpinnedAnchorRow, sources: &[CodeIndexSource]) -> Judgement {
    match Anchor::from_db_string(&row.anchor) {
        None => Judgement::Unmeasured(Unmeasured::AnchorUnparsed),
        Some(anchor) => judge(&anchor, row.valid_from, &gather_evidence(&anchor, sources)),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use std::path::PathBuf;
    use std::time::Duration;

    use super::{
        ClockBasis, Evidence, FileClock, Judgement, Unmeasured, WorkTree, judge, read_rev_parse,
        settle_pin,
    };
    use crate::anchor::Anchor;
    use crate::anchor_hash::{AnchorHash, registered_source};
    use crate::config::{CodeIndexSource, CodeLanguage};

    fn source(id: &str, language: CodeLanguage) -> CodeIndexSource {
        CodeIndexSource::new(id, id, PathBuf::from("/repo"), language, true).unwrap()
    }

    #[test]
    fn a_py_span_anchor_is_judged_by_the_python_source_and_a_later_file_change_is_unmeasured() {
        let sources = [
            source("oh-my-boring", CodeLanguage::Rust),
            source("oh-my-boring-python", CodeLanguage::Python),
        ];
        let anchor = Anchor::from_db_string("ohmyboring:scripts/x.py:L1-L2").unwrap();
        let claimed_at = std::time::UNIX_EPOCH + Duration::from_secs(1_000);
        let present = |clock| Evidence::Present {
            source: registered_source(&anchor, &sources).unwrap(),
            content: "def f():\n    return 1\n".to_owned(),
            clock,
        };

        let before = judge(
            &anchor,
            claimed_at,
            &present(FileClock::Committed(claimed_at - Duration::from_secs(1))),
        );
        let after = judge(
            &anchor,
            claimed_at,
            &present(FileClock::Committed(claimed_at + Duration::from_secs(1))),
        );

        assert!(matches!(before, Judgement::Hashed { hash, .. } if hash.symbol == "f"));
        assert!(matches!(
            after,
            Judgement::Unmeasured(Unmeasured::ChangedAfterClaim)
        ));
    }

    #[test]
    fn the_source_named_exactly_like_the_project_wins_over_a_longer_name_listed_first() {
        let sources = [
            source("oh-my-boring-site", CodeLanguage::Rust),
            source("oh-my-boring", CodeLanguage::Rust),
        ];
        let anchor = Anchor::from_db_string("ohmyboring:drudge/src/main.rs:L1-L2").unwrap();

        let picked = registered_source(&anchor, &sources).unwrap();

        assert_eq!(picked.id(), "oh-my-boring");
    }

    #[test]
    fn only_not_a_repository_falls_back_to_mtime_and_any_other_rev_parse_failure_is_git_failed() {
        let not_a_repo = read_rev_parse(
            false,
            "fatal: not a git repository (or any of the parent directories): .git",
        );
        let dubious = read_rev_parse(
            false,
            "fatal: detected dubious ownership in repository at '/repo'",
        );

        assert_eq!(not_a_repo, WorkTree::NotARepository);
        assert_eq!(dubious, WorkTree::Failed);
    }

    #[test]
    fn a_pin_that_changed_no_row_is_not_counted_as_hashed() {
        let hashed = || Judgement::Hashed {
            hash: AnchorHash {
                hash: "h".to_owned(),
                symbol: String::new(),
            },
            basis: ClockBasis::Git,
        };

        let written = settle_pin(hashed(), 1);
        let missed = settle_pin(hashed(), 0);

        assert!(matches!(written, Judgement::Hashed { .. }));
        assert!(matches!(
            missed,
            Judgement::Unmeasured(Unmeasured::GoneBeforePin)
        ));
    }
}
