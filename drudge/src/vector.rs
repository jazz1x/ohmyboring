//! The vector-index boundary: the six queries that rank rows by embedding distance. Every caller
//! reaches them through this trait; `store::vector_pg` is the only implementation. The signatures are
//! the ones the `Store` methods had — filters stay per-function, nothing is grouped into a "shape".

use anyhow::Result;

use crate::store::{AnchoredClaim, Hit, Recurrence};

// Static dispatch only (callers hold a concrete `Store`), so the missing `Send` bound a `dyn` user
// would need is not in play.
#[allow(async_fn_in_trait)]
pub trait VectorIndex {
    /// Documents semantically nearest to `source_path` (chunk-embedding cosine), nearest first.
    async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>>;

    /// Recurrence register: a newer risk/blocked claim near an older one from a different note.
    async fn recurrences(
        &self,
        days: i64,
        project: Option<&str>,
        limit: i64,
    ) -> Result<Vec<Recurrence>>;

    /// Query embedding → top-k current claims.
    #[allow(clippy::too_many_arguments)]
    async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
        anchor_path: Option<&str>,
        include_stale: bool,
    ) -> Result<Vec<AnchoredClaim>>;

    async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>>;

    /// Vector search with optional project and recency (`since_hours`) filters.
    async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>>;

    /// The single nearest document within `max_dist`, as `(source_path, distance)`.
    async fn nearest_document(&self, vec: &[f32], max_dist: f64) -> Result<Option<(String, f64)>>;
}
