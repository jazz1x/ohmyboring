//! Graph → Obsidian `relates_to` projection.
//!
//! Cross-reference: design decision D7 (vault/wiki SSOT, DB rebuildable).
use std::collections::HashSet;
use std::path::Path;

use anyhow::{Context, Result};

use crate::store::{ProjectionScope, ProjectionVia, Store};
use crate::vault::{is_seed_note, set_relates_to, split_frontmatter, wiki_stem};
use crate::vector::VectorIndex;

const PROGRESS_EVERY: usize = 1_000;

/// Project the Postgres graph (`related_docs`) into each wiki note's `relates_to` wikilinks.
/// Makes the Obsidian graph view draw the GraphRAG connections directly. Idempotent (recomputed and rewritten every time).
/// Among related documents, only wiki notes in the same vault become `[[wiki-NNNN]]` (so Obsidian can resolve them).
/// The shipped seed note (`wiki-0000`) is skipped so private note ids never leak into the public sample.
///
/// By default only the notes whose `relates_to` can have changed since the last run are visited
/// (`Store::projection_scope`); the output is the same as a full pass. `force_full` visits all.
pub async fn project_links(
    store: &Store,
    vault_root: &Path,
    limit: i64,
    force_full: bool,
) -> Result<usize> {
    let run_started_at = store.projection_run_clock().await?;
    let wiki_dir = vault_root.join("wiki");
    let entries = std::fs::read_dir(&wiki_dir)
        .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?
        .collect::<std::io::Result<Vec<_>>>()?;
    let scope = store.projection_scope(force_full).await?;
    let paths: Vec<std::path::PathBuf> = match &scope {
        ProjectionScope::Full(reason) => {
            eprintln!(
                "[sync-stage] project_links scope=full reason={} files={}",
                reason.label(),
                entries.len()
            );
            entries.iter().map(std::fs::DirEntry::path).collect()
        }
        ProjectionScope::Changed(dirty) => {
            let dirty_set: HashSet<&str> = dirty.iter().map(String::as_str).collect();
            let visit: Vec<std::path::PathBuf> = entries
                .iter()
                .map(std::fs::DirEntry::path)
                .filter(|p| dirty_set.contains(p.to_string_lossy().as_ref()))
                .collect();
            eprintln!(
                "[sync-stage] project_links scope=changed dirty={} files={} visiting={}",
                dirty.len(),
                entries.len(),
                visit.len()
            );
            visit
        }
    };
    let total = paths.len();
    let started = std::time::Instant::now();
    let mut updated = 0;
    for (done, path) in paths.iter().enumerate() {
        if project_note(store, path, limit).await? {
            updated += 1;
        }
        // The loop issues 2–3 store queries per file; at 17k files a stall here was silent (c4, 11-05).
        if (done + 1) % PROGRESS_EVERY == 0 {
            eprintln!(
                "[sync-stage] project_links {}/{total} files, {:.1}s",
                done + 1,
                started.elapsed().as_secs_f64()
            );
        }
    }
    store.finish_projection_run(run_started_at).await?;
    Ok(updated)
}

/// A neighbour of one note, where it came from.
struct Neighbour {
    path: String,
    stem: String,
    via: ProjectionVia,
}

fn push_new(out: &mut Vec<Neighbour>, path: &str, via: ProjectionVia) {
    if let Some(stem) = wiki_stem(path)
        && !out.iter().any(|n| n.stem == stem)
    {
        out.push(Neighbour {
            path: path.to_owned(),
            stem,
            via,
        });
    }
}

/// Project ONE wiki note's `relates_to` from the graph + semantic + project-recency fallbacks.
/// `Ok(true)` when the file was rewritten. The single-note unit shared by the full pass
/// (`project_links`) and the `remember` fast path — so the logic (incl. the seed-note id-leak guard
/// and the "don't wipe to []" rule) lives in exactly one place (SSOT).
///
/// remember projects only the new note with this; the *backlinks* from its neighbors are reconciled by
/// the next periodic `project_links` (which visits them because the new note changed). That lag is invisible to retrieval (recall is
/// embedding-based, not relates_to-based) and only delays an Obsidian graph edge — eventually
/// consistent, never a stale lie.
pub async fn project_note(store: &Store, path: &Path, limit: i64) -> Result<bool> {
    let stem = path.file_stem().and_then(|s| s.to_str());
    let stem_ok = stem.is_some_and(|n| n.starts_with("wiki-"));
    let ext_ok = path
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case("md"));
    if !(stem_ok && ext_ok) {
        return Ok(false);
    }
    // Never rewrite the tracked seed note: a graph projection would fill its
    // shipped-empty relates_to with the user's PRIVATE note ids (id leak).
    if stem.is_some_and(is_seed_note) {
        return Ok(false);
    }
    let content = std::fs::read_to_string(path)?;
    let Some((yaml, body)) = split_frontmatter(&content) else {
        return Ok(false);
    };
    let src_path = path.to_string_lossy().into_owned();
    let mut found: Vec<Neighbour> = Vec::new();
    for p in store.related_docs(&src_path, limit).await? {
        push_new(&mut found, &p, ProjectionVia::Graph);
    }
    // Semantic blend: the graph above only links docs sharing >=2 EXACT concept/tool slugs, so it
    // misses notes about the same thing in DIFFERENT words (and older / cross-project notes). Add the
    // meaning-nearest docs by chunk-embedding cosine (>=~0.6 → distance <= 0.40), deduped with the above.
    for p in store.semantic_related_docs(&src_path, 4, 0.40).await? {
        push_new(&mut found, &p, ProjectionVia::Semantic);
    }
    // isolation prevention: STILL fewer than 2 links → supplement with the same project's latest docs.
    if found.len() < 2 {
        for p in store.recent_project_docs(&src_path, 2).await? {
            push_new(&mut found, &p, ProjectionVia::Recent);
        }
    }
    found.truncate(8); // cap relates_to (graph + semantic) so a hub note doesn't explode into a mesh
    let remembered: Vec<(String, ProjectionVia)> =
        found.iter().map(|n| (n.path.clone(), n.via)).collect();
    let links: Vec<String> = found
        .iter()
        .map(|n| format!("\"[[{}]]\"", n.stem))
        .collect();
    // Don't wipe: if the graph projection found nothing, preserve whatever relates_to the compile
    // relation-pass (shared tools/concepts) already set — an empty graph must not clobber it to [].
    if links.is_empty() {
        store
            .replace_projection_links(&src_path, &remembered)
            .await?;
        return Ok(false);
    }
    let new_content = format!("---\n{}\n---\n{body}", set_relates_to(yaml, &links));
    let rewritten = new_content != content;
    if rewritten {
        std::fs::write(path, new_content)?;
    }
    store
        .replace_projection_links(&src_path, &remembered)
        .await?;
    Ok(rewritten)
}
