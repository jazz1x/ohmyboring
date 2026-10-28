//! Notes a `remember` saved while a sync held `sync_lock`: their indexing waits in one ordered
//! queue that one worker drains (mcp.rs `drain_deferred`). Until a note's indexing commits it is
//! in `waiting`, so the embedding duplicate check — which reads the `chunk` table — still sees it.

use std::sync::Mutex;
use std::sync::OnceLock;

use tokio::sync::mpsc::UnboundedSender;

use crate::frontmatter::FrontMatter;

/// What the worker needs to index one saved note the way the locked path would have.
pub(crate) struct IndexJob {
    pub(crate) path: std::path::PathBuf,
    pub(crate) label: String,
    pub(crate) front: FrontMatter,
    /// As the caller named them: resolved again when the worker runs, because the note they
    /// point at may be a deferred one that only has a `document` row by then.
    pub(crate) supersedes: Vec<String>,
    pub(crate) edge_judge: Option<String>,
}

struct Waiting {
    path: String,
    embedding: Option<Vec<f32>>,
}

#[derive(Default)]
pub(crate) struct DeferredNotes {
    pub(super) queue: OnceLock<UnboundedSender<IndexJob>>,
    waiting: Mutex<Vec<Waiting>>,
}

impl DeferredNotes {
    fn waiting(&self) -> std::sync::MutexGuard<'_, Vec<Waiting>> {
        self.waiting
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// While anything waits, a new note queues behind it even if the lock is free right now:
    /// taking the lock first would index it before an older note it may point at.
    pub(crate) fn has_backlog(&self) -> bool {
        !self.waiting().is_empty()
    }

    /// `embedding` is `None` for a correction, which skips the duplicate gate and never
    /// computed one.
    pub(crate) fn add(&self, path: String, embedding: Option<Vec<f32>>) {
        self.waiting().push(Waiting { path, embedding });
    }

    pub(crate) fn remove(&self, path: &str) {
        self.waiting().retain(|w| w.path != path);
    }

    /// The waiting note nearest to `embedding` within `max_dist` (cosine distance, the metric
    /// `Store::nearest_document` uses).
    pub(crate) fn nearest(&self, embedding: &[f32], max_dist: f64) -> Option<String> {
        self.waiting()
            .iter()
            .filter_map(|w| {
                let dist = cosine_distance(embedding, w.embedding.as_deref()?);
                (dist <= max_dist).then(|| (w.path.clone(), dist))
            })
            .min_by(|a, b| a.1.total_cmp(&b.1))
            .map(|(path, _)| path)
    }
}

/// A zero vector gives NaN, which is never within a distance bound.
fn cosine_distance(a: &[f32], b: &[f32]) -> f64 {
    let dot: f64 = a
        .iter()
        .zip(b)
        .map(|(x, y)| f64::from(*x) * f64::from(*y))
        .sum();
    let norm = |v: &[f32]| v.iter().map(|x| f64::from(*x).powi(2)).sum::<f64>().sqrt();
    1.0 - dot / (norm(a) * norm(b))
}
