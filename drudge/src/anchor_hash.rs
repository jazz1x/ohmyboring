use anyhow::Result;
use sha2::{Digest, Sha256};

use crate::anchor::{Anchor, LineSpan};
use crate::code_index::{self, ParsedSymbol};
use crate::config::CodeIndexSource;
use crate::store::{AnchorCheckRow, AnchorCheckWatermark, Store};

pub const ANCHOR_CHECK_BATCH: usize = 2_000;

#[derive(Debug)]
pub struct AnchorHash {
    pub hash: String,
    pub symbol: String,
}

pub fn hash_for_anchor(anchor: &Anchor, sources: &[CodeIndexSource]) -> Option<AnchorHash> {
    let source = registered_source(anchor, sources)?;
    let content = read_under_root(source, &anchor.path)?;
    match anchor.span {
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

fn read_under_root(source: &CodeIndexSource, path: &str) -> Option<String> {
    let file = source.root().join(path);
    if !file.starts_with(source.root()) {
        return None;
    }
    std::fs::read_to_string(file).ok()
}

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

#[derive(Debug)]
pub struct SymbolProbe {
    pub span: LineSpan,
    pub hash: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StaleVerdict {
    Unchanged,
    Repin(LineSpan),
    Stale(&'static str),
}

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

#[derive(Debug, Default)]
pub struct AnchorCheckStats {
    pub checked: usize,
    pub repinned: usize,
    pub stale: usize,
    pub unreadable: usize,
}

pub async fn check_stale_anchors(
    store: &Store,
    sources: &[CodeIndexSource],
) -> Result<AnchorCheckStats> {
    check_stale_anchors_limited(store, sources, ANCHOR_CHECK_BATCH).await
}

pub async fn check_stale_anchors_limited(
    store: &Store,
    sources: &[CodeIndexSource],
    batch: usize,
) -> Result<AnchorCheckStats> {
    let mut stats = AnchorCheckStats::default();
    if sources.is_empty() {
        return Ok(stats);
    }
    let watermark = store.anchor_check_watermark().await?;
    let mut rows = store
        .claims_due_for_anchor_check(watermark.as_ref(), batch)
        .await?;
    if let Some(watermark) = watermark.as_ref()
        && rows.len() < batch
    {
        rows.extend(
            store
                .claims_due_for_anchor_check_head(watermark, batch - rows.len())
                .await?,
        );
    }

    for row in &rows {
        match recheck_anchor(sources, row) {
            Recheck::Unreadable => stats.unreadable += 1,
            Recheck::NoEvidence => {}
            Recheck::Verdict(verdict) => {
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
        }
    }
    if let Some(last) = rows.last() {
        store
            .set_anchor_check_watermark(&AnchorCheckWatermark {
                valid_from: last.valid_from,
                subject: last.subject.clone(),
                predicate: last.predicate.clone(),
            })
            .await?;
    }
    Ok(stats)
}

fn repinned_anchor(db: &str, span: LineSpan) -> String {
    match db.rsplit_once(':') {
        Some((head, suffix)) if suffix.starts_with('L') => format!("{head}:{span}"),
        _ => db.to_owned(),
    }
}

enum Recheck {
    Verdict(StaleVerdict),
    NoEvidence,
    Unreadable,
}

fn recheck_anchor(sources: &[CodeIndexSource], row: &AnchorCheckRow) -> Recheck {
    let Some(anchor) = Anchor::from_db_string(&row.anchor) else {
        return Recheck::Unreadable;
    };
    let Some(source) = registered_source(&anchor, sources) else {
        return Recheck::NoEvidence;
    };
    let Some(content) = read_under_root(source, &anchor.path) else {
        return Recheck::Verdict(StaleVerdict::Stale("file missing"));
    };
    let Ok(symbols) = code_index::parse_symbols(source, &anchor.path, &content) else {
        return Recheck::NoEvidence;
    };
    let probe = |symbol: &ParsedSymbol| SymbolProbe {
        span: LineSpan {
            start: row_to_line(symbol.start_row),
            end: row_to_line(symbol.end_row),
        },
        hash: sha256_hex(symbol.body.as_bytes()),
    };
    match anchor.span {
        None => {
            if sha256_hex(content.as_bytes()) == row.anchor_hash {
                Recheck::Verdict(StaleVerdict::Unchanged)
            } else {
                Recheck::Verdict(StaleVerdict::Stale("symbol body changed"))
            }
        }
        Some(span) => {
            let Some(stored_symbol) = row.anchor_symbol.as_deref() else {
                return Recheck::NoEvidence;
            };
            let covering = resolve_covering(&symbols, span).map(&probe);
            let same_name: Vec<SymbolProbe> = symbols
                .iter()
                .filter(|symbol| symbol.qualified_name == stored_symbol)
                .map(probe)
                .collect();
            Recheck::Verdict(stale_verdict(
                &row.anchor_hash,
                covering.as_ref(),
                &same_name,
            ))
        }
    }
}

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
        let covering = probe(7, 9, "different-now");
        let by_name = [probe(7, 9, "different-now"), probe(3, 5, "h")];
        assert_eq!(
            stale_verdict("h", Some(&covering), &by_name),
            StaleVerdict::Repin(ls(3, 5))
        );
    }

    #[test]
    fn repins_when_nothing_covers_the_old_span_anymore() {
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
        let covering = probe(3, 5, "some-other-body");
        assert_eq!(
            stale_verdict("h", Some(&covering), &[]),
            StaleVerdict::Stale("symbol body changed")
        );
    }
}
