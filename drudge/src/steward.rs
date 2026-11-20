//! Vault note hygiene — `scripts/data-steward.py --fix` as an engine subcommand.
//!
//! Same rewrite as the Python script (the rollback behind `BORING_DATA_STEWARD_IMPL=python`):
//! a note whose `project` is not its canonical slug (org prefix, `.git`, a boring.json rule name)
//! or whose tags hold a placeholder (`_`, `pr_`, `slack_`, empty) gets its `project:` line and
//! `tags:` block rewritten in place, after the original is copied to `<note>.md.bak`. A note with
//! `author: owner` is only reported, never rewritten. The report-only half of the script
//! (typos, claims, readability, folder-name splits) stays Python.
//!
//! Byte-level behaviour copied on purpose: Python's `read_text` turns `\r\n`/`\r` into `\n`,
//! `str.splitlines()` also splits on `\v \f \x1c-\x1e \x85    `, `str.strip()` also strips
//! `\x1c-\x1f`, and a note whose frontmatter does not parse is skipped with one `[warn]` line.
//! The first note that cannot be read or written aborts the run (Python raised there): notes before
//! it stay fixed, notes after it are not touched, the exit code is non-zero.
use crate::config::BoringConfig;
use anyhow::{Context, Result, bail};
use serde_yaml::Value;
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};

const PLACEHOLDER_TAGS: [&str; 4] = ["_", "pr_", "slack_", ""];
const SEED_NOTE: &str = "wiki-0000.md";
const MAX_TAGS: usize = 6;

#[derive(Debug, Clone, PartialEq)]
enum Tag {
    Text(String),
    /// A YAML tag that is not a string (`tags: [2024]`). Python crashed on these while fixing.
    Other,
}

#[derive(Debug)]
struct Note {
    path: PathBuf,
    yaml_text: String,
    body: String,
    project: String,
    tags: Vec<Tag>,
    owner_written: bool,
}

#[derive(Debug, Default)]
struct Collected {
    notes: Vec<Note>,
    skipped: usize,
}

fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

fn py_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{b}'
            | '\u{c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// `str.splitlines()`: no trailing empty piece, `\r\n` is one break.
fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if !py_line_break(c) {
            continue;
        }
        out.push(&s[start..i]);
        let mut end = i + c.len_utf8();
        if c == '\r' && chars.peek().is_some_and(|&(_, next)| next == '\n') {
            chars.next();
            end += 1;
        }
        start = end;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// `vault_note.split_frontmatter`: (raw frontmatter, body) or None. BOM, CRLF and padded fences
/// are tolerated.
fn split_frontmatter(text: &str) -> Option<(String, String)> {
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let normalised = text.replace("\r\n", "\n");
    let (first, rest) = normalised.split_once('\n')?;
    if py_strip(first) != "---" {
        return None;
    }
    let lines: Vec<&str> = rest.split('\n').collect();
    let close = lines.iter().position(|l| py_strip(l) == "---")?;
    Some((lines[..close].join("\n"), lines[close + 1..].join("\n")))
}

/// `boring_config.canonical_repo`. The config's own version returns "" for `org/`; Python falls
/// back to the whole slug there.
fn canonical(cfg: &BoringConfig, raw: &str) -> String {
    let c = cfg.canonical_repo(raw);
    if !c.is_empty() {
        return c;
    }
    let repo = py_strip(raw);
    repo.strip_suffix(".git").unwrap_or(repo).to_owned()
}

fn is_placeholder(t: &str) -> bool {
    PLACEHOLDER_TAGS.contains(&t)
}

/// A frontmatter mapping with PyYAML's duplicate-key rule (the last `project:` wins): serde_yaml's
/// own `Mapping` refuses duplicates, and such a note must not stop being fixable.
struct Pairs(Vec<(Value, Value)>);

impl Pairs {
    fn get(&self, key: &str) -> Option<&Value> {
        self.0
            .iter()
            .rev()
            .find(|(k, _)| matches!(k, Value::String(s) if s == key))
            .map(|(_, v)| v)
    }
}

impl<'de> serde::Deserialize<'de> for Pairs {
    fn deserialize<D: serde::Deserializer<'de>>(de: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = Pairs;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a frontmatter mapping")
            }
            // `yaml.safe_load(x) or {}`: an empty document is an empty mapping.
            fn visit_unit<E>(self) -> std::result::Result<Pairs, E> {
                Ok(Pairs(Vec::new()))
            }
            fn visit_none<E>(self) -> std::result::Result<Pairs, E> {
                Ok(Pairs(Vec::new()))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Pairs, A::Error> {
                let mut pairs = Vec::new();
                while let Some(pair) = map.next_entry::<Value, Value>()? {
                    pairs.push(pair);
                }
                Ok(Pairs(pairs))
            }
        }
        de.deserialize_any(V)
    }
}

fn parse_note(path: PathBuf, text: &str) -> Result<Option<std::result::Result<Note, String>>> {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let Some((yaml_text, body)) = split_frontmatter(text) else {
        return Ok(None);
    };
    let map: Pairs = match serde_yaml::from_str(&yaml_text) {
        Ok(v) => v,
        Err(e) => {
            return Ok(Some(Err(format!(
                "[warn] skipping {name}: frontmatter parse error: {e}"
            ))));
        }
    };
    let project = match map.get("project") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => bail!("{name}: project is not a string"),
    };
    let tags = match map.get("tags") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Sequence(items)) => items
            .iter()
            .map(|t| match t {
                Value::String(s) => Tag::Text(s.clone()),
                _ => Tag::Other,
            })
            .collect(),
        Some(_) => bail!("{name}: tags is not a list"),
    };
    let owner_written = matches!(map.get("author"), Some(Value::String(a)) if a == "owner");
    Ok(Some(Ok(Note {
        path,
        yaml_text,
        body,
        project,
        tags,
        owner_written,
    })))
}

fn collect(wiki_dir: &Path) -> Result<Collected> {
    let mut names: Vec<String> = fs::read_dir(wiki_dir)
        .with_context(|| format!("read {}", wiki_dir.display()))?
        .filter_map(Result::ok)
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| {
            n.starts_with("wiki-")
                && Path::new(n).extension().is_some_and(|e| e == "md")
                && n != SEED_NOTE
        })
        .collect();
    names.sort();
    let mut out = Collected::default();
    for name in names {
        let path = wiki_dir.join(&name);
        let bytes = fs::read(&path).with_context(|| format!("read {}", path.display()))?;
        let text = String::from_utf8(bytes)
            .with_context(|| format!("{} is not valid UTF-8", path.display()))?;
        let text = text.replace("\r\n", "\n").replace('\r', "\n");
        match parse_note(path, &text)? {
            None => {}
            Some(Ok(note)) => out.notes.push(note),
            Some(Err(warning)) => {
                eprintln!("{warning}");
                out.skipped += 1;
            }
        }
    }
    Ok(out)
}

fn needs_fix(cfg: &BoringConfig, n: &Note) -> bool {
    canonical(cfg, &n.project) != n.project
        || n.tags
            .iter()
            .any(|t| matches!(t, Tag::Text(s) if is_placeholder(s)))
}

/// (notes the fix rewrites, owner-written notes it only reports).
fn fix_plan<'a>(cfg: &BoringConfig, notes: &'a [Note]) -> (Vec<&'a Note>, Vec<&'a Note>) {
    notes
        .iter()
        .filter(|n| needs_fix(cfg, n))
        .partition(|n| !n.owner_written)
}

fn fixed_tags(
    name: &str,
    old_project: &str,
    old_tags: &[Tag],
    target: &str,
) -> Result<Vec<String>> {
    let target_repo = (!target.is_empty()).then(|| format!("repo/{target}"));
    let old_last = old_project
        .rsplit('/')
        .next()
        .unwrap_or(old_project)
        .to_lowercase();
    let old_full = old_project.to_lowercase();
    let mut out: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for tag in old_tags {
        let Tag::Text(t) = tag else {
            bail!("{name}: a tag is not a string; fix this note by hand");
        };
        if is_placeholder(t) {
            continue;
        }
        let mut t = t.clone();
        if let (Some(repo), Some(suffix)) = (&target_repo, t.strip_prefix("repo/")) {
            let suffix = suffix.to_lowercase();
            if suffix == old_full || suffix == old_last {
                t.clone_from(repo);
            }
        }
        let lowered = t.to_lowercase();
        if seen.contains(&lowered) {
            continue;
        }
        seen.push(lowered);
        out.push(t);
    }
    if let Some(repo) = target_repo
        && !seen.contains(&repo.to_lowercase())
    {
        out.insert(0, repo);
    }
    out.truncate(MAX_TAGS);
    Ok(out)
}

/// `_rewrite_yaml_block`: the first `project:` line and the `tags:` line with its `-` entries.
fn rewrite_yaml_block(yaml_text: &str, project: &str, tags: &[String]) -> String {
    let mut lines: Vec<String> = py_splitlines(yaml_text)
        .into_iter()
        .map(str::to_owned)
        .collect();
    if let Some(i) = lines.iter().position(|l| l.starts_with("project:")) {
        lines[i] = format!("project: {project}");
    }
    if let Some(start) = lines.iter().position(|l| l.starts_with("tags:")) {
        let mut end = start + 1;
        while end < lines.len() && py_strip(&lines[end]).starts_with('-') {
            end += 1;
        }
        let block: Vec<String> = if tags.is_empty() {
            vec!["tags: []".to_owned()]
        } else {
            std::iter::once("tags:".to_owned())
                .chain(tags.iter().map(|t| format!("- {t}")))
                .collect()
        };
        lines.splice(start..end, block);
    }
    lines.join("\n")
}

/// `shutil.copy2`: content, permission bits and both timestamps.
fn copy2(from: &Path, to: &Path) -> Result<()> {
    let meta = fs::metadata(from)?;
    fs::copy(from, to)?;
    let times = fs::FileTimes::new()
        .set_accessed(meta.accessed()?)
        .set_modified(meta.modified()?);
    fs::File::open(to)?.set_times(times)?;
    Ok(())
}

fn fix_note(cfg: &BoringConfig, n: &Note) -> Result<()> {
    let name = n
        .path
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let target = canonical(cfg, &n.project);
    let tags = fixed_tags(&name, &n.project, &n.tags, &target)?;
    let mut bak = n.path.clone().into_os_string();
    bak.push(".bak");
    copy2(&n.path, Path::new(&bak)).with_context(|| format!("back up {}", n.path.display()))?;
    let rewritten = rewrite_yaml_block(&n.yaml_text, &target, &tags);
    fs::write(&n.path, format!("---\n{rewritten}\n---\n{}", n.body))
        .with_context(|| format!("write {}", n.path.display()))
}

/// `--vault`, then `BORING_VAULT_DIR`, then `BORING_HOME/vault`, then `~/oh-my-boring/vault`; `/wiki` under it.
pub fn wiki_dir(flag: Option<&str>, get: &dyn Fn(&str) -> Option<String>) -> Result<PathBuf> {
    let home = get("HOME");
    let tilde = |p: &str| match (p.strip_prefix("~/"), &home) {
        (Some(rest), Some(h)) => format!("{h}/{rest}"),
        _ => p.to_owned(),
    };
    let set = |name: &str| get(name).filter(|s| !s.is_empty());
    let vault = if let Some(v) = flag.filter(|s| !s.is_empty()) {
        tilde(v)
    } else if let Some(v) = set("BORING_VAULT_DIR") {
        tilde(&v)
    } else if let Some(h) = set("BORING_HOME") {
        format!("{}/vault", tilde(&h))
    } else {
        let h = home
            .context("BORING_VAULT_DIR, BORING_HOME and HOME are unset; cannot locate the vault")?;
        format!("{h}/oh-my-boring/vault")
    };
    Ok(Path::new(&vault).join("wiki"))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mode {
    pub fix: bool,
    pub yes: bool,
}

/// `drudge steward [--fix [--yes]]`: print what would be rewritten; with `--fix` (and a "y" at the
/// prompt unless `--yes`) rewrite it. Dry-run is the default.
pub fn run(cfg: &BoringConfig, wiki_dir: &Path, mode: Mode) -> Result<()> {
    anyhow::ensure!(
        wiki_dir.exists(),
        "wiki directory not found: {}",
        wiki_dir.display()
    );
    let collected = collect(wiki_dir)?;
    println!("📂 vault/wiki: {}", wiki_dir.display());
    println!("📝 notes scanned: {}\n", collected.notes.len());
    let (to_fix, owner_held) = fix_plan(cfg, &collected.notes);
    if !mode.fix {
        println!(
            "{} note(s) would be fixed, {} owner-written note(s) reported only.",
            to_fix.len(),
            owner_held.len()
        );
        for n in &to_fix {
            println!("   {}", n.path.display());
        }
        if !to_fix.is_empty() {
            println!("💡 This is a dry-run. Pass --fix to rewrite.");
        }
        return Ok(());
    }
    if !mode.yes && !to_fix.is_empty() {
        print!("Apply fixes? [y/N] ");
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
    for n in &to_fix {
        fix_note(cfg, n)?;
    }
    println!(
        "\n✅ Fixed {} note(s). vault/wiki is gitignored, so `git diff` won't show changes — review the `*.md.bak` files written next to each fixed note, then delete them once satisfied.",
        to_fix.len()
    );
    if !owner_held.is_empty() {
        println!(
            "🔒 Left {} owner-written note(s) as they are (author: owner) — fix by hand:",
            owner_held.len()
        );
        for n in &owner_held {
            println!(
                "   {}",
                n.path
                    .file_name()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default()
            );
        }
    }
    println!(
        "ⓘ fixed {} / reported {} / skipped {}",
        to_fix.len(),
        owner_held.len(),
        collected.skipped
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn cfg() -> BoringConfig {
        BoringConfig::default()
    }

    // The data-loss line: a note the owner wrote is reported, never rewritten. A placeholder-tag
    // note by someone else is rewritten with its original kept byte for byte in `.bak`.
    #[test]
    fn owner_written_notes_are_reported_and_never_rewritten() {
        let dir = tempfile::tempdir().unwrap();
        let wiki = dir.path().join("wiki");
        fs::create_dir_all(&wiki).unwrap();
        let theirs = "---\nid: wiki-1\nproject: marketboro/foodspring-front\ntags:\n- _\n- repo/marketboro/foodspring-front\n- keep\n---\nbody\n";
        let mine = theirs
            .replace("wiki-1", "wiki-2")
            .replace("id:", "author: owner\nid:");
        fs::write(wiki.join("wiki-1.md"), theirs).unwrap();
        fs::write(wiki.join("wiki-2.md"), &mine).unwrap();

        run(
            &cfg(),
            &wiki,
            Mode {
                fix: true,
                yes: true,
            },
        )
        .unwrap();

        assert_eq!(fs::read_to_string(wiki.join("wiki-2.md")).unwrap(), mine);
        assert!(!wiki.join("wiki-2.md.bak").exists());
        assert_eq!(
            fs::read_to_string(wiki.join("wiki-1.md.bak")).unwrap(),
            theirs
        );
        assert_eq!(
            fs::read_to_string(wiki.join("wiki-1.md")).unwrap(),
            "---\nid: wiki-1\nproject: foodspring-front\ntags:\n- repo/foodspring-front\n- keep\n---\nbody\n"
        );
    }
}
