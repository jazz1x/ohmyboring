//! The one place `vector.backend` is folded: `Store` implements `VectorIndex` by sending each query
//! to the backend the process was started on. `recurrences` too: on Surreal it pairs the claims in
//! the engine (`vector_surreal::recurrences`), since Surreal has no radius join.

use std::collections::HashMap;

use anyhow::Result;

use super::LogCopy;
use super::vector_pg::Pg;
use super::vector_surreal::{SurrealClient, SurrealView, Swap};
use super::{
    AnchorCheckRow, AnchorCheckWatermark, AnchoredClaim, ClaimEraCounts, ConsumptionCounts,
    ContestedBy, EventLogFilter, EventLogRow, GraphStats, Hit, Meta, QueryLogRow, RankFacts,
    RecallLabelRow, RecallLabelStats, RecentDoc, Recurrence, RegisterAsk, RegisterRow,
    RegisterRows, SEMANTIC_EDGE_KINDS, SemanticStats, Store, UnpinnedAnchorRow,
};
use crate::approved::EdgeRow;
use crate::claim_priors::ClaimPrior;
use crate::claim_source::{ClaimRow, ListRow};
use crate::frontmatter::Claim;
use crate::rules::RuleRow;
use crate::split_subjects::Sample;
use crate::vector::VectorIndex;
use std::time::SystemTime;

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
            Self::Surreal(client) => RoutedView::Surreal(SurrealView { client }),
        }
    }
}

impl Store {
    /// Full-text search with optional project and recency filters: pg's `tsv` on pgvector, the
    /// BM25 index on `backend=surreal`. The text stage of `retrieve` calls this.
    ///
    /// # Errors
    /// The backend's query or the pg hydrate failed.
    pub async fn text_search_filtered(
        &self,
        query: &str,
        k: usize,
        project: Option<&str>,
        since_hours: Option<i32>,
    ) -> Result<Vec<Hit>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.pg_text_search_filtered(query, k, project, since_hours)
                    .await
            }
            VectorRoute::Surreal(client) => {
                SurrealView { client }
                    .text_search_filtered(query, k, project, since_hours)
                    .await
            }
        }
    }
}

impl Store {
    /// The Surreal client when this process runs `backend=surreal`; the code-index reader shares it.
    #[must_use]
    pub fn surreal_client(&self) -> Option<&SurrealClient> {
        match &self.vector_route {
            VectorRoute::Pgvector => None,
            VectorRoute::Surreal(client) => Some(client),
        }
    }
}

/// C-SURALL2 Z2: the four log tables are read from Surreal's copy on `backend=surreal` (the copy
/// pass in `log_copy_surreal` fills it) and from pg otherwise. Their writes stay on pg.
impl Store {
    /// Events newest first, filtered; on Surreal the `event_log` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn recent_events(&self, filter: EventLogFilter<'_>) -> Result<Vec<EventLogRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.recent_events_pg(filter).await,
            VectorRoute::Surreal(client) => client.recent_events(&filter).await,
        }
    }

    /// Logged queries newest first; on Surreal the `query_log` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn recent_queries(&self, limit: i64) -> Result<Vec<QueryLogRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.recent_queries_pg(limit).await,
            VectorRoute::Surreal(client) => client.recent_queries(limit).await,
        }
    }

    /// Recall labels newest first; on Surreal the `recall_label` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn recent_recall_labels(&self, limit: i64) -> Result<Vec<RecallLabelRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.recent_recall_labels_pg(limit).await,
            VectorRoute::Surreal(client) => client.recent_recall_labels(limit).await,
        }
    }

    /// Per-judge verdict counts plus the llm/human agreement; on Surreal the `recall_label` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn recall_label_stats(&self) -> Result<(Vec<RecallLabelStats>, i64, i64)> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.recall_label_stats_pg().await,
            VectorRoute::Surreal(client) => client.recall_label_stats().await,
        }
    }

    /// The anchor-check watermark; on Surreal the `sync_state` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn anchor_check_watermark(&self) -> Result<Option<AnchorCheckWatermark>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.anchor_check_watermark_pg().await,
            VectorRoute::Surreal(client) => client.anchor_check_watermark().await,
        }
    }
}

/// C-SURALL2 Z2③b: the claim reads that fed the cards, the registers, the `/claim-*` doors and the
/// audit are read from Surreal's `claim_row` copy (with `edge` and `document` where pg joined
/// them) on `backend=surreal`, and from pg otherwise.
impl Store {
    /// The project's north-star `goal` / `check` claims; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn north_star_claims(
        &self,
        project: &str,
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.north_star_claims_pg(project, exclude_origins).await,
            VectorRoute::Surreal(client) => {
                let predicates: Vec<String> = Self::NORTH_STAR_PREDICATES
                    .iter()
                    .map(|p| (*p).to_owned())
                    .collect();
                client
                    .north_star_claims(
                        &Self::north_star_subject(project),
                        &predicates,
                        exclude_origins,
                    )
                    .await
            }
        }
    }

    /// Stalled claims, oldest first, one per note; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn stalled_claims(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
        older_than_days: i64,
    ) -> Result<Vec<Claim>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.stalled_claims_pg(k, project, kinds, exclude_origins, older_than_days)
                    .await
            }
            VectorRoute::Surreal(client) => {
                let answer = client
                    .stalled_register_rows(
                        &RegisterAsk {
                            k,
                            project,
                            kinds,
                            exclude_origins,
                        },
                        older_than_days,
                    )
                    .await?;
                Ok(answer
                    .rows
                    .into_iter()
                    .map(RegisterRow::into_claim)
                    .collect())
            }
        }
    }

    /// Stalled claims for a register answer with the full match count; on Surreal the
    /// `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn stalled_register_rows(
        &self,
        k: i64,
        project: Option<&str>,
        kinds: Option<&[String]>,
        exclude_origins: &[String],
        older_than_days: i64,
    ) -> Result<RegisterRows> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.stalled_register_rows_pg(k, project, kinds, exclude_origins, older_than_days)
                    .await
            }
            VectorRoute::Surreal(client) => {
                client
                    .stalled_register_rows(
                        &RegisterAsk {
                            k,
                            project,
                            kinds,
                            exclude_origins,
                        },
                        older_than_days,
                    )
                    .await
            }
        }
    }

    /// The claims each note currently declares through its `claims` edges; on Surreal the `edge`
    /// and `claim_row` copies.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn declared_claims(
        &self,
        paths: &[String],
        per_doc: i64,
    ) -> Result<HashMap<String, RegisterRows>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.declared_claims_pg(paths, per_doc).await,
            VectorRoute::Surreal(client) => {
                let asked: Vec<(String, String)> = paths
                    .iter()
                    .map(|p| (super::doc_node_id(p), p.clone()))
                    .collect();
                client.declared_claims(&asked, per_doc).await
            }
        }
    }

    /// Current claims of one subject for `GET /claim-source`; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claim_source_rows(&self, subject: &str) -> Result<Vec<ClaimRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claim_source_rows_pg(subject).await,
            VectorRoute::Surreal(client) => client.claim_source_rows(subject).await,
        }
    }

    /// Current claims of one predicate for `GET /claim-sources`; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claim_source_list_rows(&self, predicate: &str) -> Result<Vec<ListRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claim_source_list_rows_pg(predicate).await,
            VectorRoute::Surreal(client) => client.claim_source_list_rows(predicate).await,
        }
    }

    /// Current claims of one note with the sealed value each replaced, for `GET /claim-priors`; on
    /// Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claim_priors(&self, source: &str) -> Result<Vec<ClaimPrior>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claim_priors_pg(source).await,
            VectorRoute::Surreal(client) => client.claim_priors(source).await,
        }
    }

    /// Current `rule` claims for `GET /rules`; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn rule_rows(&self) -> Result<Vec<RuleRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.rule_rows_pg().await,
            VectorRoute::Surreal(client) => client.rule_rows().await,
        }
    }

    /// Every live claim's `(subject, source_path)` for `GET /repairs/split-subjects`; on Surreal
    /// the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn split_subject_rows(&self) -> Result<Vec<(String, String)>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.split_subject_rows_pg().await,
            VectorRoute::Surreal(client) => client.split_subject_rows().await,
        }
    }

    /// Per-predicate row counts and the longest value of the given spellings, for
    /// `GET /repairs/split-subjects/samples`; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn split_samples(&self, variants: &[String]) -> Result<Vec<Sample>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.split_samples_pg(variants).await,
            VectorRoute::Surreal(client) => client.split_samples(variants).await,
        }
    }

    /// Current claims the owner said, per note; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn said_by_owner_counts(&self, paths: &[String]) -> Result<HashMap<String, i64>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.said_by_owner_counts_pg(paths).await,
            VectorRoute::Surreal(client) => client.said_by_owner_counts(paths).await,
        }
    }

    /// Current claims per era and the label-only count for the audit; on Surreal the `claim_row`
    /// copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claim_era_counts(&self) -> Result<ClaimEraCounts> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claim_era_counts_pg().await,
            VectorRoute::Surreal(client) => client.claim_era_counts().await,
        }
    }

    /// Claims marked stale, for the audit; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claim_stale_count(&self) -> Result<usize> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claim_stale_count_pg().await,
            VectorRoute::Surreal(client) => client.claim_stale_count().await,
        }
    }

    /// Pinned, not-stale claims past the watermark, oldest first; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claims_due_for_anchor_check(
        &self,
        after: Option<&AnchorCheckWatermark>,
        limit: usize,
    ) -> Result<Vec<AnchorCheckRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claims_due_for_anchor_check_pg(after, limit).await,
            VectorRoute::Surreal(client) => client.claims_due_for_anchor_check(after, limit).await,
        }
    }

    /// Open, live claims with an anchor and no `anchor_hash`; on Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claims_without_anchor_hash(&self) -> Result<Vec<UnpinnedAnchorRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.claims_without_anchor_hash_pg().await,
            VectorRoute::Surreal(client) => client.claims_without_anchor_hash().await,
        }
    }

    /// Fill `anchor_hash`/`anchor_symbol` of one open, live row that has none. `valid_from`,
    /// `superseded_at` and the row count stay as they are. Returns the rows changed: 0 when the
    /// row was replaced, marked stale or pinned between the read and this write.
    ///
    /// # Errors
    /// The backend's statement failed.
    pub async fn pin_anchor_hash(
        &self,
        subject: &str,
        predicate: &str,
        valid_from: SystemTime,
        anchor_hash: &str,
        anchor_symbol: &str,
    ) -> Result<u64> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.pin_anchor_hash_pg(subject, predicate, valid_from, anchor_hash, anchor_symbol)
                    .await
            }
            VectorRoute::Surreal(client) => {
                client
                    .pin_anchor_hash(subject, predicate, valid_from, anchor_hash, anchor_symbol)
                    .await
            }
        }
    }

    /// The same list from the start up to and including the watermark (the wrap-around); on
    /// Surreal the `claim_row` copy.
    ///
    /// # Errors
    /// The backend's query failed.
    pub async fn claims_due_for_anchor_check_head(
        &self,
        watermark: &AnchorCheckWatermark,
        limit: usize,
    ) -> Result<Vec<AnchorCheckRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.claims_due_for_anchor_check_head_pg(watermark, limit)
                    .await
            }
            VectorRoute::Surreal(client) => {
                client
                    .claims_due_for_anchor_check_head(watermark, limit)
                    .await
            }
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
    /// On `backend=surreal`: define the HNSW indexes a table lacks (`SurrealClient::catch_up`);
    /// no row is copied from pg (C0b1 bundle 3). Pgvector: nothing to do. Returns
    /// `(written, deleted)`.
    ///
    /// # Errors
    /// A Surreal read or write failed.
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
        self.vector_route
            .view(self)
            .recurrences(days, project, limit)
            .await
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

/// C-SURALL2 C0a: the document, graph and consumption reads. Each sends the question to the
/// backend the process was started on: pg's SQL (`*_pg` in `store.rs`) on pgvector, the
/// `document` / `chunk` / `node` / `edge` copies (`vector_surreal/doc_reads.rs`) on Surreal.
/// Every method below fails with the backend's error.
impl Store {
    /// The stored change-detection sha of a note.
    pub async fn get_doc_sha(&self, path: &str) -> Result<Option<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.get_doc_sha_pg(path).await,
            VectorRoute::Surreal(client) => client.get_doc_sha(path).await,
        }
    }

    /// Every note path with a `document` row.
    pub async fn all_doc_paths(&self) -> Result<Vec<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.all_doc_paths_pg().await,
            VectorRoute::Surreal(client) => client.all_doc_paths().await,
        }
    }

    /// Newest notes with their chunks joined.
    pub async fn recent_docs(
        &self,
        limit: i64,
        exclude_origins: &[String],
        since_hours: Option<i32>,
        project: Option<&str>,
    ) -> Result<Vec<RecentDoc>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.recent_docs_pg(limit, exclude_origins, since_hours, project)
                    .await
            }
            VectorRoute::Surreal(client) => {
                client
                    .recent_docs(limit, exclude_origins, since_hours, project)
                    .await
            }
        }
    }

    /// Notes sharing at least two concrete graph nodes with this one.
    pub async fn related_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.related_docs_pg(source_path, limit).await,
            VectorRoute::Surreal(client) => client.related_docs(source_path, limit).await,
        }
    }

    /// The older notes this one shares a claim or a concept with.
    pub async fn related_by_shared_ground(
        &self,
        source_path: &str,
        limit: i64,
        min_shared: i64,
    ) -> Result<Vec<RecentDoc>> {
        match &self.vector_route {
            VectorRoute::Pgvector => {
                self.related_by_shared_ground_pg(source_path, limit, min_shared)
                    .await
            }
            VectorRoute::Surreal(client) => {
                client
                    .related_by_shared_ground(source_path, limit, min_shared)
                    .await
            }
        }
    }

    /// The related notes (`related_docs`) with their chunks joined.
    pub async fn related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
    ) -> Result<Vec<RecentDoc>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.related_doc_content_pg(source_path, limit).await,
            VectorRoute::Surreal(client) => client.related_doc_content(source_path, limit).await,
        }
    }

    /// The most recent other notes of the same project.
    pub async fn recent_project_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.recent_project_docs_pg(source_path, limit).await,
            VectorRoute::Surreal(client) => client.recent_project_docs(source_path, limit).await,
        }
    }

    /// Full-text search without filters: pg's `tsv`, or the BM25 index on Surreal.
    pub async fn text_search(&self, query: &str, k: usize) -> Result<Vec<Hit>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.text_search_pg(query, k).await,
            VectorRoute::Surreal(client) => {
                SurrealView { client }
                    .text_search_filtered(query, k, None, None)
                    .await
            }
        }
    }

    /// Chunk count.
    pub async fn count(&self) -> Result<usize> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.count_pg().await,
            VectorRoute::Surreal(client) => client.chunk_count().await,
        }
    }

    /// Origin / project / kind / path of every chunk, for the audit.
    pub async fn all_meta(&self) -> Result<Vec<Meta>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.all_meta_pg().await,
            VectorRoute::Surreal(client) => client.all_meta().await,
        }
    }

    /// Every project name the corpus knows.
    pub async fn project_names(&self) -> Result<Vec<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.project_names_pg().await,
            VectorRoute::Surreal(client) => client.project_names().await,
        }
    }

    /// Projects with a note touched within `days`, busiest first.
    pub async fn active_project_counts(&self, days: i32) -> Result<Vec<(String, i64)>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.active_project_counts_pg(days).await,
            VectorRoute::Surreal(client) => client.active_project_counts(days).await,
        }
    }

    /// Card-session edges for `GET /approved`.
    pub async fn approved_edges(&self) -> Result<Vec<EdgeRow>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.approved_edges_pg().await,
            VectorRoute::Surreal(client) => client.approved_edges().await,
        }
    }

    /// `used` / `contested` edge counts per note.
    pub(super) async fn counts_by(
        &self,
        paths: &[String],
        contested_by: ContestedBy,
    ) -> Result<HashMap<String, ConsumptionCounts>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.counts_by_pg(paths, contested_by).await,
            VectorRoute::Surreal(client) => client.consumption_counts_by(paths, contested_by).await,
        }
    }

    /// The concept names written on each note.
    pub async fn concepts_of(&self, paths: &[String]) -> Result<HashMap<String, Vec<String>>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.concepts_of_pg(paths).await,
            VectorRoute::Surreal(client) => client.concepts_of(paths).await,
        }
    }

    /// The newer notes whose `supersedes` edge points at each path.
    pub async fn superseded_by(&self, paths: &[String]) -> Result<HashMap<String, Vec<String>>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.superseded_by_pg(paths).await,
            VectorRoute::Surreal(client) => client.superseded_by(paths).await,
        }
    }

    /// Replaced / owner-written / updated facts that order a returned set.
    pub async fn rank_facts(&self, paths: &[String]) -> Result<HashMap<String, RankFacts>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.rank_facts_pg(paths).await,
            VectorRoute::Surreal(client) => client.rank_facts(paths).await,
        }
    }

    /// Notes with the most `used` edges from recent sessions.
    pub async fn reused_recently(
        &self,
        days: i64,
        limit: i64,
    ) -> Result<Vec<(RecentDoc, i64, i64)>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.reused_recently_pg(days, limit).await,
            VectorRoute::Surreal(client) => client.reused_recently(days, limit).await,
        }
    }

    /// Structural neighbours (project / topic / claim labels) of a chunk's note.
    pub async fn graph_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.graph_neighbors_pg(chunk_id).await,
            VectorRoute::Surreal(client) => {
                client
                    .neighbor_labels(chunk_id, &["in_project", "tagged", "claims"])
                    .await
            }
        }
    }

    /// Semantic neighbours (`uses` / `about` / `claims` labels) of a chunk's note.
    pub async fn semantic_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.semantic_neighbors_pg(chunk_id).await,
            VectorRoute::Surreal(client) => {
                client.neighbor_labels(chunk_id, &SEMANTIC_EDGE_KINDS).await
            }
        }
    }

    /// Graph size summary for the audit.
    pub async fn graph_stats(&self) -> Result<GraphStats> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.graph_stats_pg().await,
            VectorRoute::Surreal(client) => client.graph_stats().await,
        }
    }

    /// Semantic node and edge counts for the audit.
    pub async fn semantic_stats(&self) -> Result<SemanticStats> {
        match &self.vector_route {
            VectorRoute::Pgvector => self.semantic_stats_pg().await,
            VectorRoute::Surreal(client) => client.semantic_stats().await,
        }
    }
}
