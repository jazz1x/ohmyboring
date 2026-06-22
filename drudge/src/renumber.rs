//! Wiki id renumbering — compact `vault/wiki/wiki-NNNN.md` ids after deletions.
//!
//! The vault is the SSOT; the Postgres store is rebuilt from it by `sync`.  This module therefore
//! only rewrites the markdown files.  After applying a renumber plan the caller should run a
//! vector-mode `sync` so the DB source_paths/chunks/claims/edges catch up.
//!
//! # Safety rules
//! - Dry-run by default; apply requires an explicit flag.
//! - The shipped seed note (`wiki-0000`) is never remapped.
//! - All `wiki-NNNN` tokens (frontmatter id, relates_to, superseded_by, body wikilinks) are
//!   rewritten by a single regex pass using a bijective mapping, so references stay consistent.
//! - Files are staged to a temporary suffix and renamed only after all writes succeed, so a crash
//!   mid-operation leaves at most `*.omb-renumber-tmp` files that are easy to clean up.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use regex::Regex;

/// Suffix for files staged during `apply`. A crash leaves only `*.<this>` files, recoverable by rename.
const TMP_SUFFIX: &str = "md.omb-renumber-tmp";

/// A single planned change.
#[derive(Debug, Clone)]
pub struct Move {
    pub old_path: PathBuf,
    pub new_path: PathBuf,
    pub old_id: String,
    pub new_id: String,
    pub new_content: String,
}

/// Renumber plan: every existing wiki note mapped to its compact id.
#[derive(Debug, Clone)]
pub struct Plan {
    pub moves: Vec<Move>,
}

impl Plan {
    /// True if the plan would actually change anything.
    #[must_use]
    pub fn is_noop(&self) -> bool {
        self.moves.iter().all(|m| m.old_id == m.new_id)
    }
}

/// Collect `wiki-NNNN.md` files in `wiki_dir`, sorted by numeric id.
/// The seed note `wiki-0000.md` is included but will be left untouched by `plan`.
fn collect_wiki_files(wiki_dir: &Path) -> Result<BTreeMap<u32, PathBuf>> {
    let mut files: BTreeMap<u32, PathBuf> = BTreeMap::new();
    if !wiki_dir.exists() {
        return Ok(files);
    }
    for entry in std::fs::read_dir(wiki_dir)
        .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let Some(stem) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        let Some(num_str) = stem.strip_prefix("wiki-") else {
            continue;
        };
        let Ok(num) = num_str.parse::<u32>() else {
            continue;
        };
        files.insert(num, path);
    }
    Ok(files)
}

/// Build a bijective mapping old_id → new_id that compacts the sequence.
/// `wiki-0000` is mapped to itself; every other existing id is mapped to the
/// smallest unused integer ≥ 1 in ascending order.
fn build_mapping(files: &BTreeMap<u32, PathBuf>) -> HashMap<String, String> {
    let mut mapping: HashMap<String, String> = HashMap::new();
    let mut next: u32 = 1;
    for &num in files.keys() {
        if num == 0 {
            mapping.insert("wiki-0000".to_owned(), "wiki-0000".to_owned());
            continue;
        }
        let new_id = format!("wiki-{next:04}");
        mapping.insert(format!("wiki-{num:04}"), new_id);
        next += 1;
    }
    mapping
}

/// Rewrite every `wiki-NNNN` token in `content` according to `mapping`.
/// Tokens not present in `mapping` (e.g. dangling links) are left unchanged.
fn rewrite_content(content: &str, mapping: &HashMap<String, String>) -> Result<String> {
    use std::borrow::Cow;
    let re = Regex::new(r"wiki-\d{4,5}").context("invalid wiki id regex")?;
    let out = re.replace_all(content, |caps: &regex::Captures<'_>| {
        let old = caps.get(0).map_or("", |m| m.as_str());
        match mapping.get(old) {
            Some(new) => Cow::Owned(new.clone()),
            None => Cow::Owned(old.to_owned()),
        }
    });
    Ok(out.into_owned())
}

/// Build a renumber plan for `wiki_dir` without mutating disk.
pub fn plan(wiki_dir: &Path) -> Result<Plan> {
    let files = collect_wiki_files(wiki_dir)?;
    let mapping = build_mapping(&files);
    let mut moves = Vec::with_capacity(files.len());

    for (num, old_path) in files {
        let old_id = format!("wiki-{num:04}");
        let new_id = mapping
            .get(&old_id)
            .cloned()
            .unwrap_or_else(|| old_id.clone());

        let content = std::fs::read_to_string(&old_path)
            .with_context(|| format!("failed to read {}", old_path.display()))?;
        let new_content = rewrite_content(&content, &mapping)?;

        let new_path = old_path.with_file_name(format!("{new_id}.md"));
        moves.push(Move {
            old_path,
            new_path,
            old_id,
            new_id,
            new_content,
        });
    }

    Ok(Plan { moves })
}

/// Apply a plan to disk, crash-safely.
///
/// The ordering guarantees no note content can be lost: every note's new content is staged to a
/// temp file *before* any original is touched, so once Phase 1 completes the full corpus is durable
/// on disk under `<final>.omb-renumber-tmp`. The originals are only deleted in the final phase, by
/// which point every surviving note already lives at its final path.
///
/// - Phase 1 — stage: write every new content to `<final>.omb-renumber-tmp`. No original is modified.
///   Aborts if a temp from a previous interrupted run is present (rather than clobbering it).
/// - Phase 2 — publish: `rename` each temp onto its final path. `rename` is atomic and replaces any
///   existing file; an original overwritten here already has its content captured in another temp.
/// - Phase 3 — sweep: delete originals vacated by the renumber (old ids with no file at their new path).
///
/// A crash in any phase loses nothing: each note's content is present in at least one of
/// {temp, final, unchanged-original}. Recovery from leftover `*.omb-renumber-tmp` is a plain rename.
/// Idempotent-ish: a second clean run sees an already-compact vault and produces a no-op plan.
pub fn apply(plan: &Plan) -> Result<()> {
    if plan.is_noop() {
        return Ok(());
    }

    let final_path = |m: &Move| -> PathBuf {
        if m.old_id == m.new_id {
            m.old_path.clone()
        } else {
            m.new_path.clone()
        }
    };

    // Phase 1 — stage. Write new content for every note (moving or in-place) to a temp beside its
    // final path. Nothing destructive happens here.
    let mut renames: Vec<(PathBuf, PathBuf)> = Vec::with_capacity(plan.moves.len());
    for m in &plan.moves {
        let dst = final_path(m);
        let tmp = dst.with_extension(TMP_SUFFIX);
        if tmp.exists() {
            anyhow::bail!(
                "stale temp file {} from a previously interrupted renumber; \
                 inspect and remove `*.omb-renumber-tmp` files before retrying",
                tmp.display()
            );
        }
        std::fs::write(&tmp, &m.new_content)
            .with_context(|| format!("failed to write temp {}", tmp.display()))?;
        renames.push((tmp, dst));
    }

    // Phase 2 — publish. Atomically move each staged file onto its final path. Any original
    // overwritten here had its content saved to a temp in Phase 1, so this cannot lose data.
    for (tmp, dst) in &renames {
        std::fs::rename(tmp, dst)
            .with_context(|| format!("failed to rename {} → {}", tmp.display(), dst.display()))?;
    }

    // Phase 3 — sweep. Remove originals whose path is not also a final path (the high-numbered ids
    // vacated by compaction). Every surviving note already lives at its final path.
    let final_paths: std::collections::HashSet<PathBuf> =
        plan.moves.iter().map(final_path).collect();
    for m in &plan.moves {
        if m.old_id != m.new_id && !final_paths.contains(&m.old_path) {
            std::fs::remove_file(&m.old_path)
                .with_context(|| format!("failed to remove vacated {}", m.old_path.display()))?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(
        clippy::unwrap_used,
        clippy::expect_used,
        clippy::panic,
        clippy::map_unwrap_or
    )]

    use super::*;
    use std::io::Write;

    #[test]
    fn mapping_keeps_seed_and_compacts_others() {
        let mut files = BTreeMap::new();
        files.insert(0, PathBuf::from("wiki-0000.md"));
        files.insert(2, PathBuf::from("wiki-0002.md"));
        files.insert(5, PathBuf::from("wiki-0005.md"));
        let mapping = build_mapping(&files);
        assert_eq!(mapping["wiki-0000"], "wiki-0000");
        assert_eq!(mapping["wiki-0002"], "wiki-0001");
        assert_eq!(mapping["wiki-0005"], "wiki-0002");
    }

    #[test]
    fn rewrite_updates_all_id_occurrences() {
        let mut mapping = HashMap::new();
        mapping.insert("wiki-0002".to_owned(), "wiki-0001".to_owned());
        mapping.insert("wiki-0005".to_owned(), "wiki-0002".to_owned());

        let input = "---\nid: wiki-0005\nrelates_to:\n  - wiki-0002\nsuperseded_by: wiki-0002\n---\nSee [[wiki-0002|note]] and [[wiki-0005]].";
        let out = rewrite_content(input, &mapping).unwrap();
        assert!(out.contains("id: wiki-0002"), "{out}");
        assert!(out.contains("- wiki-0001"), "{out}");
        assert!(out.contains("superseded_by: wiki-0001"), "{out}");
        assert!(out.contains("[[wiki-0001|note]]"), "{out}");
        assert!(out.contains("[[wiki-0002]]"), "{out}");
    }

    #[test]
    fn plan_and_apply_round_trip() {
        let tmp = tempfile::tempdir().unwrap();
        let wiki = tmp.path().join("wiki");
        std::fs::create_dir(&wiki).unwrap();

        // gap: 0000, 0002, 0005
        for (name, content) in [
            (
                "wiki-0000.md",
                "---\nid: wiki-0000\ntitle: seed\n---\nseed.",
            ),
            (
                "wiki-0002.md",
                "---\nid: wiki-0002\ntitle: two\nrelates_to:\n  - wiki-0005\n---\n[[wiki-0005]]",
            ),
            ("wiki-0005.md", "---\nid: wiki-0005\ntitle: five\n---\nbody"),
        ] {
            let mut f = std::fs::File::create(wiki.join(name)).unwrap();
            f.write_all(content.as_bytes()).unwrap();
        }

        let plan = plan(&wiki).unwrap();
        assert_eq!(plan.moves.len(), 3);
        apply(&plan).unwrap();

        assert!(wiki.join("wiki-0000.md").exists());
        assert!(wiki.join("wiki-0001.md").exists());
        assert!(wiki.join("wiki-0002.md").exists());
        assert!(!wiki.join("wiki-0005.md").exists());

        let one = std::fs::read_to_string(wiki.join("wiki-0001.md")).unwrap();
        assert!(one.contains("id: wiki-0001"));
        assert!(one.contains("- wiki-0002"));
        assert!(one.contains("[[wiki-0002]]"));

        let two = std::fs::read_to_string(wiki.join("wiki-0002.md")).unwrap();
        assert!(two.contains("id: wiki-0002"));
    }

    #[test]
    fn apply_aborts_on_stale_temp_instead_of_clobbering() {
        let tmp = tempfile::tempdir().unwrap();
        let wiki = tmp.path().join("wiki");
        std::fs::create_dir(&wiki).unwrap();
        for (name, content) in [
            ("wiki-0000.md", "---\nid: wiki-0000\n---\nseed."),
            ("wiki-0002.md", "---\nid: wiki-0002\n---\ntwo"),
        ] {
            std::fs::write(wiki.join(name), content).unwrap();
        }
        // Simulate a previous interrupted run leaving the staged temp for wiki-0002 → wiki-0001.
        let stale = wiki.join("wiki-0001.md.omb-renumber-tmp");
        std::fs::write(&stale, "PRECIOUS recovery data").unwrap();

        let plan = plan(&wiki).unwrap();
        let err = apply(&plan).unwrap_err();
        assert!(err.to_string().contains("stale temp file"), "{err}");
        // The stale temp must be preserved untouched for manual recovery, not overwritten.
        assert_eq!(
            std::fs::read_to_string(&stale).unwrap(),
            "PRECIOUS recovery data"
        );
        // Originals must be untouched (no deletes happened).
        assert!(wiki.join("wiki-0002.md").exists());
    }
}
