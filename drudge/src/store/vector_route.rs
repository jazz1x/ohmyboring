//! The one place `vector.backend` is folded: `Store` implements `VectorIndex` by sending each query
//! to the backend the process was started on. `recurrences` (pair distances) has no SurrealDB
//! counterpart — stage 0 measured no radius join there — so it is pinned to pgvector on every
//! backend, and the startup log and `config_get` say so.

use anyhow::{Result, anyhow};

use super::LogCopy;
use super::vector_pg::Pg;
use super::vector_surreal::{SURREAL_BEHIND, Scope, SurrealClient, SurrealView, Swap};
use super::{AnchoredClaim, Hit, Recurrence, Store};
use crate::vector::VectorIndex;

/// Which backend ranks the vectors for this `Store`.
#[derive(Debug, Clone, Default)]
pub enum VectorRoute {
    #[default]
    Pgvector,
    Surreal(SurrealClient),
}

impl VectorRoute {
    fn view<'a>(&'a self, store: &'a Store) -> RoutedView<'a> {
        match self {
            Self::Pgvector => RoutedView::Pg(Pg(store)),
            Self::Surreal(client) => RoutedView::Surreal(SurrealView { client, pg: store }),
        }
    }
}

impl Store {
    /// After pg committed a chunk or claim change: on `backend=surreal`, write the same change
    /// into Surreal (pg's state of `scope`, see `SurrealClient::mirror`); on pgvector there is
    /// nothing to write. A Surreal failure is an error that says pg already has the change.
    pub async fn mirror_surreal(&self, scope: &Scope) -> Result<()> {
        match &self.vector_route {
            VectorRoute::Pgvector => Ok(()),
            VectorRoute::Surreal(client) => client
                .mirror(self, scope)
                .await
                .map_err(|e| anyhow!("{SURREAL_BEHIND} (surreal: {e:#})")),
        }
    }
}

impl Store {
    /// On `backend=surreal`: one copy pass of the four log tables (see `log_copy_surreal`); on
    /// pgvector there is nothing to copy.
    ///
    /// # Errors
    /// A pg read or a Surreal request failed; the next pass converges.
    pub async fn copy_logs_surreal(&self) -> Result<Vec<LogCopy>> {
        match &self.vector_route {
            VectorRoute::Pgvector => Ok(Vec::new()),
            VectorRoute::Surreal(client) => client.copy_logs(self).await,
        }
    }
}

impl Store {
    /// On `backend=surreal`: write the `document`, `node` and `edge` rows pg holds and Surreal
    /// lacks, delete the ones only Surreal holds (`SurrealClient::catch_up`) — the rows the
    /// Python door wrote straight to pg. Pgvector: nothing to do. Returns `(written, deleted)`.
    ///
    /// # Errors
    /// A pg or Surreal read or write failed.
    pub async fn catch_up_surreal(&self) -> Result<(usize, usize)> {
        match &self.vector_route {
            VectorRoute::Pgvector => Ok((0, 0)),
            VectorRoute::Surreal(client) => client.catch_up(self).await,
        }
    }
}

impl Store {
    /// On `backend=surreal`: swap-in rebuild of the HNSW indexes whose rows changed (see
    /// `SurrealClient::swap_if_changed`); on pgvector there is nothing to swap.
    ///
    /// # Errors
    /// The listing of tables failed; a table that failed to swap is a `Swap::Failed` in the result.
    pub async fn swap_surreal_if_changed(&self) -> Result<Vec<Swap>> {
        match &self.vector_route {
            VectorRoute::Pgvector => Ok(Vec::new()),
            VectorRoute::Surreal(client) => client.swap_if_changed().await,
        }
    }
}

enum RoutedView<'a> {
    Pg(Pg<'a>),
    Surreal(SurrealView<'a>),
}

impl VectorIndex for RoutedView<'_> {
    async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>> {
        match self {
            Self::Pg(v) => v.semantic_related_docs(source_path, limit, max_dist).await,
            Self::Surreal(v) => v.semantic_related_docs(source_path, limit, max_dist).await,
        }
    }

    async fn recurrences(
        &self,
        days: i64,
        project: Option<&str>,
        limit: i64,
    ) -> Result<Vec<Recurrence>> {
        match self {
            Self::Pg(v) => v.recurrences(days, project, limit).await,
            Self::Surreal(v) => v.recurrences(days, project, limit).await,
        }
    }

    async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
        anchor_path: Option<&str>,
        include_stale: bool,
    ) -> Result<Vec<AnchoredClaim>> {
        match self {
            Self::Pg(v) => {
                v.current_claims(
                    query_emb,
                    k,
                    exclude_origins,
                    project,
                    kinds,
                    anchor_path,
                    include_stale,
                )
                .await
            }
            Self::Surreal(v) => {
                v.current_claims(
                    query_emb,
                    k,
                    exclude_origins,
                    project,
                    kinds,
                    anchor_path,
                    include_stale,
                )
                .await
            }
        }
    }

    async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        match self {
            Self::Pg(v) => v.vector_search(vec, k).await,
            Self::Surreal(v) => v.vector_search(vec, k).await,
        }
    }

    async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        match self {
            Self::Pg(v) => v.vector_search_filtered(vec, k, project, since_hours).await,
            Self::Surreal(v) => v.vector_search_filtered(vec, k, project, since_hours).await,
        }
    }

    async fn nearest_document(&self, vec: &[f32], max_dist: f64) -> Result<Option<(String, f64)>> {
        match self {
            Self::Pg(v) => v.nearest_document(vec, max_dist).await,
            Self::Surreal(v) => v.nearest_document(vec, max_dist).await,
        }
    }
}

impl VectorIndex for Store {
    async fn semantic_related_docs(
        &self,
        source_path: &str,
        limit: i64,
        max_dist: f64,
    ) -> Result<Vec<String>> {
        self.vector_route
            .view(self)
            .semantic_related_docs(source_path, limit, max_dist)
            .await
    }

    async fn recurrences(
        &self,
        days: i64,
        project: Option<&str>,
        limit: i64,
    ) -> Result<Vec<Recurrence>> {
        Pg(self).recurrences(days, project, limit).await
    }

    async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
        exclude_origins: &[String],
        project: Option<&str>,
        kinds: Option<&[String]>,
        anchor_path: Option<&str>,
        include_stale: bool,
    ) -> Result<Vec<AnchoredClaim>> {
        self.vector_route
            .view(self)
            .current_claims(
                query_emb,
                k,
                exclude_origins,
                project,
                kinds,
                anchor_path,
                include_stale,
            )
            .await
    }

    async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        self.vector_route.view(self).vector_search(vec, k).await
    }

    async fn vector_search_filtered(
        &self,
        vec: &[f32],
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        self.vector_route
            .view(self)
            .vector_search_filtered(vec, k, project, since_hours)
            .await
    }

    async fn nearest_document(&self, vec: &[f32], max_dist: f64) -> Result<Option<(String, f64)>> {
        self.vector_route
            .view(self)
            .nearest_document(vec, max_dist)
            .await
    }
}
