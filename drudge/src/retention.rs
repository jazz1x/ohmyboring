//! Raw session retention — archive old transcripts, delete ancient archives and stale markers.
//!
//! Same decisions as `scripts/retention.py` (the rollback behind `BORING_RETENTION_IMPL=python`).
//! A transcript whose `<sid>.ts` marker exists was distilled into the vault; it is archived after
//! `processed_days`. One never distilled is archived after `unprocessed_days` and is NEVER
//! hard-deleted, not even under `delete_only` — its raw file is the only source it can still be
//! distilled from. Archives are deleted only for processed sessions.
//!
//! Like the Python script: one failing file is one stderr line and the run goes on, and the exit
//! code stays 0 (the final line counts only what succeeded). A file that cannot even be `stat`ed
//! while planning aborts the run before anything is touched (Python raised there).
use anyhow::{Context, Result};
use std::fmt::Write as _;
use std::fs;
use std::io::{self, BufRead, Write};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const DAY: f64 = 86400.0;
const PENDING_STALE_DAYS: f64 = 2.0;
const RETRY_STALE_DAYS: f64 = 90.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Policy {
    pub processed_days: f64,
    pub unprocessed_days: f64,
    pub archive_days: f64,
    pub delete_only: bool,
}

impl Policy {
    /// `BORING_RETENTION_{PROCESSED,UNPROCESSED,ARCHIVE}_DAYS` (30/90/180; empty or unparsable
    /// falls back to the default, as Python's `float(env or default)` + `except ValueError`) and
    /// `BORING_RETENTION_DELETE_ONLY` (`1`/`true`/`yes`).
    pub fn from_env(get: &dyn Fn(&str) -> Option<String>) -> Self {
        let days = |name: &str, default: f64| {
            get(name)
                .filter(|s| !s.is_empty())
                .and_then(|s| s.trim().parse::<f64>().ok())
                .unwrap_or(default)
        };
        let delete_only = get("BORING_RETENTION_DELETE_ONLY")
            .is_some_and(|s| matches!(s.trim(), "1" | "true" | "yes"));
        Self {
            processed_days: days("BORING_RETENTION_PROCESSED_DAYS", 30.0),
            unprocessed_days: days("BORING_RETENTION_UNPROCESSED_DAYS", 90.0),
            archive_days: days("BORING_RETENTION_ARCHIVE_DAYS", 180.0),
            delete_only,
        }
    }
}

/// Where things live. `mark_dir` is `~/.cache/boring-distill`; the archive dir sits inside it.
#[derive(Debug, Clone)]
pub struct Places {
    pub source_dirs: Vec<PathBuf>,
    pub mark_dir: PathBuf,
}

impl Places {
    /// `source_dirs` come from boring.json (`session-end` agents); none configured means
    /// `~/.claude/projects`. Everything is under `home`.
    pub fn resolve(configured: &[String], home: &str) -> Self {
        let tidy = |s: &str| -> PathBuf { Path::new(s).components().collect() };
        let source_dirs = if configured.is_empty() {
            vec![tidy(&format!("{home}/.claude/projects"))]
        } else {
            configured.iter().map(|d| tidy(d)).collect()
        };
        Self {
            source_dirs,
            mark_dir: tidy(&format!("{home}/.cache/boring-distill")),
        }
    }

    fn archive_dir(&self) -> PathBuf {
        self.mark_dir.join("archive")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Pending,
    Processed,
    Unprocessed,
}

impl State {
    fn name(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Processed => "processed",
            Self::Unprocessed => "unprocessed",
        }
    }
}

#[derive(Debug)]
pub struct Plan {
    pub sessions: usize,
    pub to_archive: Vec<(PathBuf, PathBuf, &'static str)>,
    pub to_delete: Vec<PathBuf>,
    pub ancient_archives: Vec<PathBuf>,
    pub stale_pending: Vec<PathBuf>,
    pub stale_retry: Vec<PathBuf>,
    pub bytes: u64,
}

impl Plan {
    fn total_actions(&self) -> usize {
        self.to_archive.len()
            + self.to_delete.len()
            + self.ancient_archives.len()
            + self.stale_pending.len()
            + self.stale_retry.len()
    }
}

/// Marker names only keep `[A-Za-z0-9_-]`; an id with none left is `nosession`.
fn safe(sid: &str) -> String {
    let kept: String = sid
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .collect();
    if kept.is_empty() {
        "nosession".to_owned()
    } else {
        kept
    }
}

fn classify(mark_dir: &Path, sid: &str) -> State {
    let marker = |suffix: &str| mark_dir.join(format!("{}{suffix}", safe(sid))).exists();
    if marker(".pending") {
        State::Pending
    } else if marker(".ts") {
        State::Processed
    } else {
        State::Unprocessed
    }
}

/// Python's `st_mtime`: whole seconds plus nanoseconds as a float.
#[allow(clippy::cast_precision_loss)] // the float is the point: same ages as retention.py
fn mtime_f64(meta: &fs::Metadata) -> f64 {
    meta.mtime() as f64 + meta.mtime_nsec() as f64 * 1e-9
}

fn stat(path: &Path) -> Result<fs::Metadata> {
    fs::metadata(path).with_context(|| format!("stat {}", path.display()))
}

/// Entries of `dir` (recursive when `deep`) whose file name ends with `suffix`, sorted by path.
/// An unreadable subdirectory is skipped, as Python's `rglob`/`glob` skip it.
fn find(dir: &Path, suffix: &str, deep: bool) -> Vec<PathBuf> {
    let mut walk = walkdir::WalkDir::new(dir).min_depth(1).sort_by_file_name();
    if !deep {
        walk = walk.max_depth(1);
    }
    walk.into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_string_lossy().ends_with(suffix))
        .map(walkdir::DirEntry::into_path)
        .collect()
}

fn age_days(now: f64, meta: &fs::Metadata) -> f64 {
    (now - mtime_f64(meta)) / DAY
}

/// Decide what would be archived / deleted. Reads the filesystem, writes nothing.
pub fn plan(places: &Places, policy: &Policy, now: f64) -> Result<Plan> {
    let archive_dir = places.archive_dir();
    let mut sessions = Vec::new();
    for dir in places.source_dirs.iter().filter(|d| d.exists()) {
        sessions.extend(find(dir, ".jsonl", true));
    }

    let mut to_archive = Vec::new();
    let mut to_delete = Vec::new();
    let mut bytes = 0_u64;
    for path in &sessions {
        let sid = path.file_stem().map(|s| s.to_string_lossy().into_owned());
        let sid = sid.unwrap_or_default();
        let meta = stat(path)?;
        let state = classify(&places.mark_dir, &sid);
        let threshold = match state {
            State::Pending => continue,
            State::Processed => policy.processed_days,
            State::Unprocessed => policy.unprocessed_days,
        };
        if age_days(now, &meta) < threshold {
            continue;
        }
        bytes += meta.len();
        if policy.delete_only && state == State::Processed {
            to_delete.push(path.clone());
        } else {
            let target = archive_dir.join(format!("{sid}.jsonl.gz"));
            to_archive.push((path.clone(), target, state.name()));
        }
    }

    // Only the archive of a PROCESSED session may go: an undistilled one is its sole re-distill source.
    let mut ancient_archives = Vec::new();
    if archive_dir.exists() {
        for path in find(&archive_dir, ".jsonl.gz", false) {
            let name = path.file_name().map(|s| s.to_string_lossy().into_owned());
            let name = name.unwrap_or_default();
            let sid = name.strip_suffix(".jsonl.gz").unwrap_or(&name);
            let meta = stat(&path)?;
            let processed = classify(&places.mark_dir, sid) == State::Processed;
            if age_days(now, &meta) >= policy.archive_days && processed {
                ancient_archives.push(path);
            }
        }
    }

    let stale = |suffix: &str, limit: f64| -> Result<Vec<PathBuf>> {
        let mut out = Vec::new();
        if places.mark_dir.exists() {
            for path in find(&places.mark_dir, suffix, false) {
                if age_days(now, &stat(&path)?) >= limit {
                    out.push(path);
                }
            }
        }
        Ok(out)
    };
    let stale_pending = stale(".pending", PENDING_STALE_DAYS)?;
    let stale_retry = stale(".retry", RETRY_STALE_DAYS)?;

    Ok(Plan {
        sessions: sessions.len(),
        to_archive,
        to_delete,
        ancient_archives,
        stale_pending,
        stale_retry,
        bytes,
    })
}

#[allow(clippy::cast_precision_loss)] // display only, one decimal
fn format_size(bytes: u64) -> String {
    let mut n = bytes as f64;
    for unit in ["B", "KB", "MB", "GB"] {
        if n < 1024.0 {
            return format!("{n:.1}{unit}");
        }
        n /= 1024.0;
    }
    format!("{n:.1}TB")
}

fn py_list(paths: &[PathBuf]) -> String {
    let items: Vec<String> = paths.iter().map(|p| format!("'{}'", p.display())).collect();
    format!("[{}]", items.join(", "))
}

/// The report `retention.py` prints before it asks or applies. Plain text, no I/O.
pub fn render(plan: &Plan, places: &Places, policy: &Policy) -> String {
    let mut o = String::new();
    let _ = writeln!(o, "📂 source dirs: {}", py_list(&places.source_dirs));
    let _ = writeln!(o, "🗑  mark dir:   {}", places.mark_dir.display());
    let _ = writeln!(o, "📦 archive dir: {}\n", places.archive_dir().display());
    let _ = writeln!(o, "scanned sessions: {}", plan.sessions);
    let _ = writeln!(
        o,
        "policy: processed≥{:?}d, unprocessed≥{:?}d, archive≥{:?}d, delete_only={}\n",
        policy.processed_days,
        policy.unprocessed_days,
        policy.archive_days,
        if policy.delete_only { "True" } else { "False" }
    );
    if plan.total_actions() == 0 {
        o.push_str("✅ Nothing to clean up.\n");
        return o;
    }
    let size = format_size(plan.bytes);
    if !plan.to_archive.is_empty() {
        let _ = writeln!(
            o,
            "📦 Archive {} session(s) ({size}):",
            plan.to_archive.len()
        );
        for (src, dst, state) in &plan.to_archive {
            let _ = writeln!(o, "   [{state}] {} → {}", src.display(), dst.display());
        }
        o.push('\n');
    }
    if !plan.to_delete.is_empty() {
        let _ = writeln!(o, "🗑  Delete {} session(s) ({size}):", plan.to_delete.len());
        for p in &plan.to_delete {
            let _ = writeln!(o, "   {}", p.display());
        }
        o.push('\n');
    }
    if !plan.ancient_archives.is_empty() {
        let _ = writeln!(
            o,
            "🗑  Delete {} ancient archive(s):",
            plan.ancient_archives.len()
        );
        for p in &plan.ancient_archives {
            let _ = writeln!(o, "   {}", p.display());
        }
        o.push('\n');
    }
    if !plan.stale_pending.is_empty() {
        let _ = writeln!(
            o,
            "🧹 Remove {} stale pending marker(s)\n",
            plan.stale_pending.len()
        );
    }
    if !plan.stale_retry.is_empty() {
        let _ = writeln!(
            o,
            "🧹 Remove {} stale retry marker(s)\n",
            plan.stale_retry.len()
        );
    }
    o
}

/// Python's `os.utime(path, (t, t))` for a float `t`: the fraction is floored to nanoseconds.
#[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)] // mtimes are positive and < 2^63 s
fn py_utime(t: f64) -> SystemTime {
    let whole = t.floor();
    let nanos = ((t - whole) * 1e9).floor();
    let (secs, nanos) = if nanos >= 1e9 {
        (whole + 1.0, 0.0)
    } else {
        (whole, nanos)
    };
    if secs >= 0.0 {
        UNIX_EPOCH + Duration::new(secs as u64, nanos as u32)
    } else {
        UNIX_EPOCH - Duration::from_secs_f64(-t)
    }
}

/// gzip `src` to `dst` through `dst.tmp`, give it the transcript's mtime (the archive's age is
/// the session's age), then rename into place. A failure part-way leaves the `.tmp` behind.
fn archive(src: &Path, dst: &Path) -> Result<()> {
    let parent = dst.parent().context("archive path has no parent")?;
    fs::create_dir_all(parent)?;
    let mut tmp_name = dst.as_os_str().to_owned();
    tmp_name.push(".tmp");
    let tmp = PathBuf::from(tmp_name);
    let mut input = fs::File::open(src)?;
    let output = fs::File::create(&tmp)?;
    let mut gz = flate2::write::GzEncoder::new(output, flate2::Compression::best());
    io::copy(&mut input, &mut gz)?;
    let output = gz.finish()?;
    let mtime = mtime_f64(&stat(src)?);
    output.set_modified(py_utime(mtime))?;
    drop(output);
    fs::rename(&tmp, dst)?;
    Ok(())
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Outcome {
    pub archived: usize,
    pub deleted: usize,
    /// One line per file that failed; the run went on past each.
    pub errors: Vec<String>,
}

/// Do what the plan says. Archive-then-unlink, so a session is never removed unless its archive
/// was renamed into place.
pub fn apply(plan: &Plan) -> Outcome {
    let mut out = Outcome::default();
    for (src, dst, _state) in &plan.to_archive {
        match archive(src, dst).and_then(|()| Ok(fs::remove_file(src)?)) {
            Ok(()) => out.archived += 1,
            Err(e) => out
                .errors
                .push(format!("[error] failed to archive {}: {e}", src.display())),
        }
    }
    for p in &plan.to_delete {
        match fs::remove_file(p) {
            Ok(()) => out.deleted += 1,
            Err(e) => out
                .errors
                .push(format!("[error] failed to delete {}: {e}", p.display())),
        }
    }
    for p in &plan.ancient_archives {
        if let Err(e) = fs::remove_file(p) {
            out.errors.push(format!(
                "[error] failed to delete archive {}: {e}",
                p.display()
            ));
        }
    }
    for p in plan.stale_pending.iter().chain(&plan.stale_retry) {
        if let Err(e) = fs::remove_file(p) {
            out.errors.push(format!(
                "[error] failed to delete marker {}: {e}",
                p.display()
            ));
        }
    }
    out
}

pub fn summary(plan: &Plan, out: &Outcome) -> String {
    format!(
        "\n✅ Done: archived {}, deleted {} sessions, removed {} ancient archives, {} pending / {} retry markers.",
        out.archived,
        out.deleted,
        plan.ancient_archives.len(),
        plan.stale_pending.len(),
        plan.stale_retry.len()
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mode {
    pub apply: bool,
    pub yes: bool,
}

/// `drudge retention [--apply [--yes]]`: print the plan; with `--apply` (and a "y" at the prompt
/// unless `--yes`) carry it out. Dry-run is the default.
pub fn run(places: &Places, policy: &Policy, mode: Mode) -> Result<()> {
    let now = SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs_f64();
    let plan = plan(places, policy, now)?;
    print!("{}", render(&plan, places, policy));
    if plan.total_actions() == 0 {
        return Ok(());
    }
    if !mode.apply {
        println!("💡 This is a dry-run. Pass --apply to execute.");
        return Ok(());
    }
    if !mode.yes {
        print!("Apply retention? [y/N] ");
        io::stdout().flush()?;
        let mut line = String::new();
        let read = io::stdin().lock().read_line(&mut line)?;
        anyhow::ensure!(
            read > 0,
            "no answer on stdin; pass --yes to skip the prompt"
        );
        let answer = line.trim_end_matches(['\n', '\r']).to_lowercase();
        if answer != "y" && answer != "yes" {
            println!("aborted.");
            return Ok(());
        }
    }
    let outcome = apply(&plan);
    for line in &outcome.errors {
        eprintln!("{line}");
    }
    println!("{}", summary(&plan, &outcome));
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use std::io::Read;

    fn aged(path: &Path, days: u64) {
        let when = SystemTime::now() - Duration::from_secs(days * 86400);
        fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_modified(when)
            .unwrap();
    }

    // The data-loss line: under delete_only a PROCESSED session is deleted, but a session that was
    // never distilled (no .ts) is archived — its raw file is the only source it can still be
    // distilled from — and its original goes only after the archive is in place.
    #[test]
    fn delete_only_never_deletes_an_undistilled_session() {
        let home = tempfile::tempdir().unwrap();
        let projects = home.path().join(".claude/projects/p");
        let mark = home.path().join(".cache/boring-distill");
        fs::create_dir_all(&projects).unwrap();
        fs::create_dir_all(&mark).unwrap();
        for sid in ["done", "never"] {
            let file = projects.join(format!("{sid}.jsonl"));
            fs::write(&file, format!("{{\"sid\":\"{sid}\"}}\n")).unwrap();
            aged(&file, 200);
        }
        fs::write(mark.join("done.ts"), "").unwrap();

        let places = Places::resolve(&[], home.path().to_str().unwrap());
        let policy = Policy {
            processed_days: 30.0,
            unprocessed_days: 90.0,
            archive_days: 180.0,
            delete_only: true,
        };
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        let plan = plan(&places, &policy, now).unwrap();
        assert_eq!(plan.to_delete, vec![projects.join("done.jsonl")]);
        assert_eq!(plan.to_archive.len(), 1);
        assert_eq!(plan.to_archive[0].0, projects.join("never.jsonl"));
        assert_eq!(plan.to_archive[0].2, "unprocessed");

        let outcome = apply(&plan);
        assert_eq!(
            outcome,
            Outcome {
                archived: 1,
                deleted: 1,
                errors: vec![]
            }
        );
        assert!(!projects.join("never.jsonl").exists());
        let mut text = String::new();
        flate2::read::GzDecoder::new(fs::File::open(mark.join("archive/never.jsonl.gz")).unwrap())
            .read_to_string(&mut text)
            .unwrap();
        assert_eq!(text, "{\"sid\":\"never\"}\n");
    }
}
