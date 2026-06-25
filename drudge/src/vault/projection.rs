//! Graph → Obsidian `relates_to` projection.
//!
//! Cross-reference: design decision D7 (vault/wiki SSOT, DB rebuildable).
use std::path::Path;

use anyhow::{Context, Result};

use crate::store::Store;
use crate::vault::{is_seed_note, set_relates_to, split_frontmatter, wiki_stem};

/// Project the Postgres graph (`related_docs`) into each wiki note's `relates_to` wikilinks.
/// Makes the Obsidian graph view draw the GraphRAG connections directly. Idempotent (recomputed and rewritten every time).
/// Among related documents, only wiki notes in the same vault become `[[wiki-NNNN]]` (so Obsidian can resolve them).
/// The shipped seed note (`wiki-0000`) is skipped so private note ids never leak into the public sample.
pub async fn project_links(store: &Store, vault_root: &Path, limit: i64) -> Result<usize> {
    let wiki_dir = vault_root.join("wiki");
    let mut updated = 0;
    for entry in std::fs::read_dir(&wiki_dir)
        .with_context(|| format!("failed to read wiki dir: {}", wiki_dir.display()))?
    {
        if project_note(store, &entry?.path(), limit).await? {
            updated += 1;
        }
    }
    Ok(updated)
}

/// Project ONE wiki note's `relates_to` from the graph + semantic + project-recency fallbacks.
/// `Ok(true)` when the file was rewritten. The single-note unit shared by the full pass
/// (`project_links`) and the `remember` fast path — so the logic (incl. the seed-note id-leak guard
/// and the "don't wipe to []" rule) lives in exactly one place (SSOT).
///
/// remember projects only the new note with this; the *backlinks* from its neighbors are reconciled by
/// the next periodic full `project_links`. That lag is invisible to retrieval (recall is
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
    let mut stems: Vec<String> = store
        .related_docs(&src_path, limit)
        .await?
        .iter()
        .filter_map(|p| wiki_stem(p))
        .collect();
    // Semantic blend: the graph above only links docs sharing >=2 EXACT concept/tool slugs, so it
    // misses notes about the same thing in DIFFERENT words (and older / cross-project notes). Add the
    // meaning-nearest docs by chunk-embedding cosine (>=~0.6 → distance <= 0.40), deduped with the above.
    for p in store.semantic_related_docs(&src_path, 4, 0.40).await? {
        if let Some(s) = wiki_stem(&p)
            && !stems.contains(&s)
        {
            stems.push(s);
        }
    }
    // isolation prevention: STILL fewer than 2 links → supplement with the same project's latest docs.
    if stems.len() < 2 {
        for p in store.recent_project_docs(&src_path, 2).await? {
            if let Some(s) = wiki_stem(&p)
                && !stems.contains(&s)
            {
                stems.push(s);
            }
        }
    }
    stems.truncate(8); // cap relates_to (graph + semantic) so a hub note doesn't explode into a mesh
    let links: Vec<String> = stems.iter().map(|s| format!("\"[[{s}]]\"")).collect();
    // Don't wipe: if the graph projection found nothing, preserve whatever relates_to the compile
    // relation-pass (shared tools/concepts) already set — an empty graph must not clobber it to [].
    if links.is_empty() {
        return Ok(false);
    }
    let new_content = format!("---\n{}\n---\n{body}", set_relates_to(yaml, &links));
    if new_content != content {
        std::fs::write(path, new_content)?;
        return Ok(true);
    }
    Ok(false)
}
