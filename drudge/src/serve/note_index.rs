//! In-process index of wiki notes for the remember duplicate gate: path -> (change marker,
//! parsed front matter + body). A write lists the directory and stats every file, and re-reads
//! and parses only notes whose marker (mtime ns, size) changed or that are new; vanished notes
//! drop out. The full scan this replaces read and YAML-parsed all notes on every remember
//! (10,398 notes, 45 MB, ~1.37 s of a 1.53 s p50 — port3-latency.tsv).
//!
//! The index never changes a decision: it hands the gate the same (path, front matter, body)
//! triples a full scan would parse. Until `prefill` succeeds `with_synced` answers `None` and
//! the caller scans the disk as before. Its own lock exists because the background prefill and
//! a remember touch the table at the same time; `remember_gate` (mcp.rs) orders remembers.

use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::UNIX_EPOCH;

use crate::frontmatter::FrontMatter;

/// mtime in ns and size — two notes with the same pair are not read again.
type Marker = (u128, u64);

pub(crate) struct IndexedNote {
    pub(crate) front: FrontMatter,
    pub(crate) body: String,
}

struct Entry {
    marker: Marker,
    note: IndexedNote,
}

#[derive(Default)]
pub(crate) struct NoteIndex {
    ready: AtomicBool,
    entries: Mutex<BTreeMap<PathBuf, Entry>>,
    parses: AtomicUsize,
}

/// The one parse the duplicate gate uses, for the full scan and the index alike.
pub(crate) fn parse_note(content: &str) -> IndexedNote {
    let (yaml, body) = crate::vault::split_frontmatter(content).unwrap_or(("", ""));
    IndexedNote {
        front: serde_yaml::from_str(yaml).unwrap_or_default(),
        body: body.to_owned(),
    }
}

fn marker_of(path: &Path) -> Option<Marker> {
    let meta = std::fs::metadata(path).ok()?;
    let nanos = meta
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_nanos();
    Some((nanos, meta.len()))
}

fn note_paths(wiki_dir: &Path) -> io::Result<Vec<PathBuf>> {
    let mut paths = Vec::new();
    for entry in std::fs::read_dir(wiki_dir)? {
        let path = entry?.path();
        if path.extension().and_then(|e| e.to_str()) == Some("md") {
            paths.push(path);
        }
    }
    Ok(paths)
}

impl NoteIndex {
    /// How many notes were read and parsed so far (test seam).
    #[cfg(test)]
    pub(crate) fn parse_count(&self) -> usize {
        self.parses.load(Ordering::SeqCst)
    }

    pub(crate) fn is_ready(&self) -> bool {
        self.ready.load(Ordering::SeqCst)
    }

    /// Fill the table from the whole directory; only now do requests start using it. The scan
    /// runs outside the table lock, so a remember arriving meanwhile takes the disk path.
    pub(crate) fn prefill(&self, wiki_dir: &Path) -> io::Result<usize> {
        let paths = note_paths(wiki_dir)?;
        let fresh = self.collect(paths, BTreeMap::new());
        let count = fresh.len();
        *self
            .entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = fresh;
        self.ready.store(true, Ordering::SeqCst);
        Ok(count)
    }

    /// Bring the table in line with the directory (list + stat; changed or new notes re-read)
    /// and run `f` on it. `None` while the table is not ready: the caller scans the disk.
    pub(crate) fn with_synced<R>(
        &self,
        wiki_dir: &Path,
        f: impl FnOnce(&mut dyn Iterator<Item = (&PathBuf, &IndexedNote)>) -> R,
    ) -> io::Result<Option<R>> {
        if !self.is_ready() {
            return Ok(None);
        }
        let paths = note_paths(wiki_dir)?;
        let mut guard = self
            .entries
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let next = self.collect(paths, std::mem::take(&mut *guard));
        *guard = next;
        let mut iter = guard.iter().map(|(p, e)| (p, &e.note));
        Ok(Some(f(&mut iter)))
    }

    fn collect(
        &self,
        paths: Vec<PathBuf>,
        mut old: BTreeMap<PathBuf, Entry>,
    ) -> BTreeMap<PathBuf, Entry> {
        let mut live = BTreeMap::new();
        for path in paths {
            let Some(marker) = marker_of(&path) else {
                continue;
            };
            match old.remove(&path) {
                Some(entry) if entry.marker == marker => {
                    live.insert(path, entry);
                }
                _ => {
                    let Ok(content) = std::fs::read_to_string(&path) else {
                        continue;
                    };
                    self.parses.fetch_add(1, Ordering::SeqCst);
                    live.insert(
                        path,
                        Entry {
                            marker,
                            note: parse_note(&content),
                        },
                    );
                }
            }
        }
        live
    }
}
