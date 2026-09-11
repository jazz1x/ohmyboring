//! D2 step 2 — the staleness layer on top of the step-1 code anchors (PRD §8).
//!
//! First-pass staleness is a **symbol-span hash, not a line-range hash**: a line-range hash
//! breaks on *moves* too. At ingest, an anchor whose project is registered in `code_index` gets
//! `anchor_hash = sha256(symbol body)` — the symbol whose span covers the anchor's — plus the
//! symbol's qualified name as the re-pin key. During sync the same resolution re-runs: an equal
//! hash is nothing; the same qualified name hashing equal at a different span is a *move* (the
//! anchor is re-pinned, not counted stale); only a genuinely different body marks the claim
//! stale. Anchors outside registered repos get no hash at all — storable without verification.

use anyhow::Result;
use sha2::{Digest, Sha256};

use crate::anchor::{Anchor, LineSpan};
use crate::code_index::{self, ParsedSymbol};
use crate::config::CodeIndexSource;
use crate::store::{AnchorCheckRow, Store};

/// Max anchors re-checked per sync. A full pass over 8,794 hashed claims takes 5 syncs at this
/// batch, so `sync` stays bounded instead of growing with the corpus.
pub const ANCHOR_CHECK_BATCH: usize = 2_000;

/// The hash layer attached to an anchor at ingest: the body hash plus the qualified name of the
/// symbol that produced it (the key the sync-time re-pin looks the hash up by).
#[derive(Debug)]
pub struct AnchorHash {
    pub hash: String,
    pub symbol: String,
}

/// Resolve the hash layer for an anchor. `None` — and nothing else changes — when the project is
/// not registered in `code_index`, the file is not under the registered root, is unreadable, or
/// no symbol covers the span (D2: anchors are storable without verification).
pub fn hash_for_anchor(anchor: &Anchor, sources: &[CodeIndexSource]) -> Option<AnchorHash> {
    let source = registered_source(anchor, sources)?;
    let content = read_under_root(source, &anchor.path)?;
    match anchor.span {
        // Span-less anchor: the file itself is the hashed body.
        None => Some(AnchorHash {
            hash: sha256_hex(content.as_bytes()),
            symbol: String::new(),
        }),
        Some(span) => {
            let symbols = code_index::parse_symbols(source, &anchor.path, &content).ok()?;
            let resolved = resolve_covering(&symbols, span)?;
            Some(AnchorHash {
                hash: sha256_hex(resolved.body.as_bytes()),
                symbol: resolved.qualified_name.clone(),
            })
        }
    }
}

fn registered_source<'a>(
    anchor: &Anchor,
    sources: &'a [CodeIndexSource],
) -> Option<&'a CodeIndexSource> {
    sources
        .iter()
        .find(|source| source.enabled() && source.id() == anchor.project)
}

/// The file `path` names under the registered root, read from disk. `None` when it escapes the
/// root or cannot be read — an anchor outside the registered root gets no hash layer.
fn read_under_root(source: &CodeIndexSource, path: &str) -> Option<String> {
    let file = source.root().join(path);
    if !file.starts_with(source.root()) {
        return None;
    }
    std::fs::read_to_string(file).ok()
}

/// The symbol whose 0-based row span contains the 1-based anchor span, innermost when several
/// nest; when none contains it, the symbol that overlaps it most. `None` when nothing overlaps —
/// the anchor stays unhashed rather than guessing.
fn resolve_covering(symbols: &[ParsedSymbol], span: LineSpan) -> Option<&ParsedSymbol> {
    let start = span.start.saturating_sub(1) as usize;
    let end = span.end.max(span.start).saturating_sub(1) as usize;
    symbols
        .iter()
        .filter(|symbol| symbol.start_row <= start && symbol.end_row >= end)
        .min_by_key(|symbol| symbol.end_row - symbol.start_row)
        .or_else(|| {
            symbols
                .iter()
                // Adjacency is not overlap: a symbol sitting right next to the span (0 shared
                // rows) must not read as covering — after a move it would mask the re-pin.
                .filter(|symbol| {
                    symbol.end_row >= start
                        && symbol.start_row <= end
                        && symbol.end_row.min(end) > symbol.start_row.max(start)
                })
                .max_by_key(|symbol| {
                    symbol
                        .end_row
                        .min(end)
                        .saturating_sub(symbol.start_row.max(start))
                })
        })
}

/// A symbol re-probed at sync time: its current 1-based span and the hash of its current body.
#[derive(Debug)]
pub struct SymbolProbe {
    pub span: LineSpan,
    pub hash: String,
}

/// Outcome of re-checking one hashed anchor: the code moved, changed, or neither.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StaleVerdict {
    /// The symbol covering the anchor span still hashes equal.
    Unchanged,
    /// The same qualified name still hashes equal, but at a different span — a move, not a
    /// change. The anchor re-pins here; it is not counted stale.
    Repin(LineSpan),
    /// No symbol under this identity hashes equal anymore. The `&str` is the `stale_reason`
    /// (`symbol body changed` / `symbol missing` / `file missing`).
    Stale(&'static str),
}

/// The "moved, not changed" decision, pure (PRD §8 D2): the stored hash, the re-probe of the
/// symbol currently covering the anchor span (when one does), and the re-probes of every symbol
/// sharing the stored qualified name. A line-range hash cannot make this distinction — only a
/// symbol-span hash can: the body survives a move, the lines do not.
pub fn stale_verdict(
    stored_hash: &str,
    covering: Option<&SymbolProbe>,
    same_name: &[SymbolProbe],
) -> StaleVerdict {
    if covering.is_some_and(|probe| probe.hash == stored_hash) {
        return StaleVerdict::Unchanged;
    }
    if let Some(probe) = same_name.iter().find(|probe| probe.hash == stored_hash) {
        return StaleVerdict::Repin(probe.span);
    }
    match covering {
        Some(_) => StaleVerdict::Stale("symbol body changed"),
        None => StaleVerdict::Stale("symbol missing"),
    }
}

/// Per-sync outcome of the anchor sweep, for the one-line log.
#[derive(Debug, Default)]
pub struct AnchorCheckStats {
    pub checked: usize,
    pub repinned: usize,
    pub stale: usize,
}

/// Re-check up to [`ANCHOR_CHECK_BATCH`] hashed anchors, oldest `valid_from` first, continuing
/// where the last sweep stopped (the `sync_state` watermark wraps to the corpus head, so every
/// hashed claim is re-checked on a ceil(n / batch) sync cadence and one pass never makes sync
/// minutes longer). Already-stale claims are facts about a moment and are not re-checked.
pub async fn check_stale_anchors(
    store: &Store,
    sources: &[CodeIndexSource],
) -> Result<AnchorCheckStats> {
    check_stale_anchors_limited(store, sources, ANCHOR_CHECK_BATCH).await
}

/// [`check_stale_anchors`] with an explicit batch size — the seam that makes the bound itself
/// testable without seeding `ANCHOR_CHECK_BATCH + 1` claims.
pub async fn check_stale_anchors_limited(
    store: &Store,
    sources: &[CodeIndexSource],
    batch: usize,
) -> Result<AnchorCheckStats> {
    let mut stats = AnchorCheckStats::default();
    if sources.is_empty() {
        return Ok(stats); // nothing registered — every anchor_hash is NULL anyway.
    }
    let watermark = store.anchor_check_watermark().await?;
    let mut rows = store.claims_due_for_anchor_check(watermark, batch).await?;
    if let Some(watermark) = watermark
        && rows.len() < batch
    {
        rows.extend(
            store
                .claims_due_for_anchor_check_head(watermark, batch - rows.len())
                .await?,
        );
    }

    for row in &rows {
        let Some(verdict) = recheck_anchor(sources, row) else {
            continue; // no evidence (unparseable, unregistered) — not staleness; retried next pass.
        };
        stats.checked += 1;
        match verdict {
            StaleVerdict::Unchanged => {}
            StaleVerdict::Repin(span) => {
                let anchor = repinned_anchor(&row.anchor, span);
                store
                    .repin_claim(&row.subject, &row.predicate, row.valid_from, &anchor)
                    .await?;
                stats.repinned += 1;
            }
            StaleVerdict::Stale(reason) => {
                store
                    .mark_claim_stale(&row.subject, &row.predicate, row.valid_from, reason)
                    .await?;
                stats.stale += 1;
            }
        }
    }
    // The watermark is the cyclic position — the LAST row taken this sweep, wrap included. It
    // may regress (head rows fill the tail of a batch); that is the round in round-robin, and
    // it is what keeps every claim re-checked on a ceil(n / batch) cadence instead of starving
    // the tail. Rows we skipped for lack of evidence still advance it — they had their turn.
    if let Some(last) = rows.last() {
        store.set_anchor_check_watermark(last.valid_from).await?;
    }
    Ok(stats)
}

/// The anchor string with its span replaced by the moved one (path and project are untouched).
fn repinned_anchor(db: &str, span: LineSpan) -> String {
    match db.rsplit_once(':') {
        // A stored span always renders `L…`; a path never contains `:`, so this is exactly it.
        Some((head, suffix)) if suffix.starts_with('L') => format!("{head}:{span}"),
        _ => db.to_owned(),
    }
}

/// Re-run the hash layer for one stored claim and decide. `None` when there is no evidence to
/// decide from: the anchor string does not parse, the project left the registry, the file
/// unparseable, or a spanned hash has no stored symbol key.
fn recheck_anchor(sources: &[CodeIndexSource], row: &AnchorCheckRow) -> Option<StaleVerdict> {
    let anchor = Anchor::from_db_string(&row.anchor)?;
    let source = registered_source(&anchor, sources)?; // unregistered since hashing — a config change, not code change.
    // A hashed anchor whose file can no longer be read under the registered root is stale:
    // the code it points at is gone.
    let Some(content) = read_under_root(source, &anchor.path) else {
        return Some(StaleVerdict::Stale("file missing"));
    };
    let symbols = code_index::parse_symbols(source, &anchor.path, &content).ok()?;
    let probe = |symbol: &ParsedSymbol| SymbolProbe {
        span: LineSpan {
            start: row_to_line(symbol.start_row),
            end: row_to_line(symbol.end_row),
        },
        hash: sha256_hex(symbol.body.as_bytes()),
    };
    match anchor.span {
        // Span-less anchor: the file itself is the hashed body.
        None => {
            if sha256_hex(content.as_bytes()) == row.anchor_hash {
                Some(StaleVerdict::Unchanged)
            } else {
                Some(StaleVerdict::Stale("symbol body changed"))
            }
        }
        Some(span) => {
            let stored_symbol = row.anchor_symbol.as_deref()?;
            let covering = resolve_covering(&symbols, span).map(&probe);
            let same_name: Vec<SymbolProbe> = symbols
                .iter()
                .filter(|symbol| symbol.qualified_name == stored_symbol)
                .map(probe)
                .collect();
            Some(stale_verdict(
                &row.anchor_hash,
                covering.as_ref(),
                &same_name,
            ))
        }
    }
}

/// 0-based parser row → 1-based line.
fn row_to_line(row: usize) -> u32 {
    u32::try_from(row.saturating_add(1)).unwrap_or(u32::MAX)
}

fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use super::{StaleVerdict, SymbolProbe, stale_verdict};
    use crate::anchor::LineSpan;

    fn ls(start: u32, end: u32) -> LineSpan {
        LineSpan { start, end }
    }

    fn probe(start: u32, end: u32, hash: &str) -> SymbolProbe {
        SymbolProbe {
            span: ls(start, end),
            hash: hash.to_owned(),
        }
    }

    #[test]
    fn unchanged_when_the_covering_symbol_still_hashes_equal() {
        let covering = probe(3, 5, "h");
        let by_name = [probe(3, 5, "h")];
        assert_eq!(
            stale_verdict("h", Some(&covering), &by_name),
            StaleVerdict::Unchanged
        );
    }

    #[test]
    fn repins_when_the_same_name_hashes_equal_at_a_different_span() {
        // The span moved (insertions above); the body under the stored qualified name is
        // identical at L7-L9 — a move, not a change.
        let covering = probe(7, 9, "different-now");
        let by_name = [probe(7, 9, "different-now"), probe(3, 5, "h")];
        assert_eq!(
            stale_verdict("h", Some(&covering), &by_name),
            StaleVerdict::Repin(ls(3, 5))
        );
    }

    #[test]
    fn repins_when_nothing_covers_the_old_span_anymore() {
        // The old span is blank lines after the move — no symbol covers it, so the decision
        // rests entirely on the stored qualified name.
        let by_name = [probe(6, 8, "h")];
        assert_eq!(
            stale_verdict("h", None, &by_name),
            StaleVerdict::Repin(ls(6, 8))
        );
    }

    #[test]
    fn stale_symbol_body_changed_when_the_name_survives_but_the_hash_does_not() {
        let covering = probe(3, 5, "edited-body");
        let by_name = [probe(3, 5, "edited-body")];
        assert_eq!(
            stale_verdict("h", Some(&covering), &by_name),
            StaleVerdict::Stale("symbol body changed")
        );
    }

    #[test]
    fn stale_symbol_missing_when_neither_covering_nor_name_survives() {
        assert_eq!(
            stale_verdict("h", None, &[]),
            StaleVerdict::Stale("symbol missing")
        );
    }

    #[test]
    fn stale_body_changed_when_a_different_name_covers_the_span() {
        // A rename (or replacement) put a differently-named symbol over the span: no same-name
        // probe matches, so the default reason is a changed body — `symbol missing` is reserved
        // for when no symbol answers at all.
        let covering = probe(3, 5, "some-other-body");
        assert_eq!(
            stale_verdict("h", Some(&covering), &[]),
            StaleVerdict::Stale("symbol body changed")
        );
    }
}
