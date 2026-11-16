//! C-SURREALCUT2 (B): what the incremental `relates_to` projection reads from Surreal on
//! `backend=surreal`. The ledger (`relates_projection`, the bounds, the watermark) stays in pg;
//! these are the facts only Surreal has: which notes were written since the watermark, which
//! notes a changed note can reach through the graph or through the embeddings, and which notes
//! share a project with a changed one. `Store::projection_scope` folds them with the ledger.
//!
//! One clock: `document.ingested_us` is stamped inside the write transaction with Surreal's
//! `time::now()`, and the watermark of a run is read from the same clock (`clock_micros`).
//! A note stamped after a run started but committed before the run's watermark was written is
//! caught by the overlap the caller subtracts from the watermark, not here.
//!
//! The graph and near reaches are the `shared` and `near` CTEs of pg's `DIRTY_SQL`, condition for
//! condition: a changed note dirties B only where it lands at or inside B's remembered k-th bound.

use std::cmp::Ordering;
use std::collections::{BTreeSet, HashMap};

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::Value;

use super::claim_tx::{Attempt, TxPlace};
use super::reads::en_us_order;
use super::{SurrealClient, first_statement, floats, text, texts};
use crate::store::{ProjectionBound, SEMANTIC_EDGE_KINDS};

/// The shared prefix of a document's graph node id (`doc:<source_path>`).
const DOC_PREFIX: &str = "doc:";
/// Values per `IN [...]` list: a request body over 1 MiB is refused (HTTP 413).
const BATCH: usize = 500;
/// Source chunks per near request: each carries its 1024-float vector twice (~22 KB).
const NEAR_BATCH: usize = 20;

/// The notes written since a moment, or the answer that there are too many to go by.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChangedDocs {
    /// More chunks changed than an incremental run is worth: the caller goes full.
    TooMany,
    Within(Vec<String>),
}

/// How the semantic edges a note is about to have relate to the ones it has.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeChange {
    /// The old edges are deleted and the expected ones written: any difference moves them.
    Replaced,
    /// Missing edges are inserted and none is deleted: only an absent one moves them.
    Added,
}

#[derive(Deserialize)]
struct EdgePair {
    src: String,
    dst: String,
}

#[derive(Deserialize)]
struct KindDst {
    kind: String,
    dst: String,
}

#[derive(Deserialize)]
struct Count {
    n: i64,
}

#[derive(Deserialize)]
struct SourceChunk {
    source_path: String,
    embedding: Vec<f32>,
}

#[derive(Deserialize)]
struct NearHit {
    source_path: String,
    d: f64,
}

fn doc_node(path: &str) -> String {
    format!("{DOC_PREFIX}{path}")
}

fn path_of(node: &str) -> &str {
    node.strip_prefix(DOC_PREFIX).unwrap_or(node)
}

/// pg `shared`: `count(*) > b.graph_kth_shared OR (count(*) = b.graph_kth_shared AND e1.src <=
/// b.graph_kth_src)`; no bound (NULL, or no row) means the plain range rule.
fn lands_in_graph_bound(count: i64, changed_node: &str, bound: Option<&ProjectionBound>) -> bool {
    match bound.and_then(|b| b.graph_kth.as_ref()) {
        None => true,
        Some((kth_count, kth_src)) => {
            let kth_count = i64::from(*kth_count);
            count > kth_count
                || (count == kth_count && en_us_order(changed_node, kth_src) != Ordering::Greater)
        }
    }
}

/// pg `near`: `dist <= COALESCE(b.sem_kth_dist, max_dist)`.
fn lands_in_near_bound(d: f64, max_dist: f64, bound: Option<&ProjectionBound>) -> bool {
    d <= bound.and_then(|b| b.sem_kth_dist).unwrap_or(max_dist)
}

impl SurrealClient {
    /// Surreal's clock in microseconds: the clock `document.ingested_us` is stamped with.
    ///
    /// # Errors
    /// The query failed or did not answer an integer.
    pub async fn clock_micros(&self) -> Result<i64> {
        self.run("RETURN time::micros(time::now());")
            .await?
            .first()
            .and_then(Value::as_i64)
            .context("surreal clock did not answer an integer")
    }

    /// The notes with `ingested_us` after `since_us`, or with none (a row written before the field
    /// existed counts as changed). Counts their chunks and stops at `max_chunks`: a run that
    /// would embed-compare more than that is cheaper as a full pass.
    ///
    /// # Errors
    /// A query failed.
    pub async fn changed_docs(&self, since_us: i64, max_chunks: usize) -> Result<ChangedDocs> {
        let sql = format!(
            "SELECT VALUE source_path FROM document WHERE ingested_us = NONE OR ingested_us > {since_us};"
        );
        let paths: Vec<String> = first_statement(&sql, self.rows::<String>(&sql).await?)?;
        let mut chunks = 0_usize;
        for batch in paths.chunks(BATCH) {
            let sql = format!(
                "SELECT count() AS n FROM chunk WHERE source_path IN {} GROUP ALL;",
                texts(batch)
            );
            let counted: Vec<Count> = first_statement(&sql, self.rows::<Count>(&sql).await?)?;
            let n = counted.first().map_or(0, |c| c.n);
            chunks += usize::try_from(n).with_context(|| format!("chunk count {n} is negative"))?;
            if chunks > max_chunks {
                return Ok(ChangedDocs::TooMany);
            }
        }
        Ok(ChangedDocs::Within(paths))
    }

    async fn edge_pairs(
        &self,
        field: &str,
        values: &[String],
        extra: &str,
    ) -> Result<Vec<EdgePair>> {
        let mut found = Vec::new();
        for batch in values.chunks(BATCH) {
            let sql = format!(
                "SELECT src, dst FROM edge WHERE {field} IN {} AND ({extra});",
                texts(batch)
            );
            found.extend(first_statement(&sql, self.rows::<EdgePair>(&sql).await?)?);
        }
        Ok(found)
    }

    /// pg `shared`: the notes B that share at least two non-project, non-topic nodes with a
    /// changed note and for which the changed note lands at or inside B's k-th graph bound.
    ///
    /// # Errors
    /// A query failed.
    pub async fn shared_reach(
        &self,
        changed: &[String],
        bounds: &HashMap<String, ProjectionBound>,
    ) -> Result<BTreeSet<String>> {
        let ids: Vec<String> = changed.iter().map(|p| doc_node(p)).collect();
        let mut own: HashMap<String, HashMap<String, i64>> = HashMap::new();
        for e in self.edge_pairs("src", &ids, "true").await? {
            if e.dst.starts_with("project:") || e.dst.starts_with("topic:") {
                continue;
            }
            *own.entry(e.src).or_default().entry(e.dst).or_default() += 1;
        }
        let dsts: BTreeSet<String> = own.values().flat_map(|d| d.keys().cloned()).collect();
        let dsts: Vec<String> = dsts.into_iter().collect();
        let mut holders: HashMap<String, Vec<String>> = HashMap::new();
        let doc_edge = format!("string::starts_with(src, {})", text(DOC_PREFIX));
        for e in self.edge_pairs("dst", &dsts, &doc_edge).await? {
            holders.entry(e.dst).or_default().push(e.src);
        }
        let mut reached = BTreeSet::new();
        for (changed_node, weights) in &own {
            let mut count: HashMap<&str, i64> = HashMap::new();
            for (dst, weight) in weights {
                for b in holders.get(dst).into_iter().flatten() {
                    if b != changed_node {
                        *count.entry(b.as_str()).or_default() += weight;
                    }
                }
            }
            for (b, n) in count {
                let b = path_of(b);
                if n >= 2 && lands_in_graph_bound(n, changed_node, bounds.get(b)) {
                    reached.insert(b.to_owned());
                }
            }
        }
        Ok(reached)
    }

    /// pg `near`: the notes B with a chunk within B's k-th semantic bound (`max_dist` when B has
    /// none) of a chunk of a changed note. The candidates come from the same HNSW scan, over the
    /// same global `chunk` table, as `semantic_related_docs_scored`, and the distance is the same
    /// exact `1 - cosine`.
    ///
    /// # Errors
    /// A query failed.
    pub async fn near_reach(
        &self,
        changed: &[String],
        bounds: &HashMap<String, ProjectionBound>,
        max_dist: f64,
    ) -> Result<BTreeSet<String>> {
        let mut sources: Vec<SourceChunk> = Vec::new();
        for batch in changed.chunks(BATCH) {
            let sql = format!(
                "SELECT source_path, embedding FROM chunk WHERE source_path IN {};",
                texts(batch)
            );
            sources.extend(first_statement(
                &sql,
                self.rows::<SourceChunk>(&sql).await?,
            )?);
        }
        let ef = self.ef();
        let mut reached = BTreeSet::new();
        for batch in sources.chunks(NEAR_BATCH) {
            let sql = batch
                .iter()
                .map(|s| {
                    let v = floats(&s.embedding);
                    format!(
                        "SELECT source_path, 1 - vector::similarity::cosine(embedding, {v}) AS d \
                         FROM chunk WHERE embedding <|{ef},{ef}|> {v} AND source_path != {};",
                        text(&s.source_path)
                    )
                })
                .collect::<Vec<_>>()
                .concat();
            for hits in self.rows::<NearHit>(&sql).await? {
                for hit in hits {
                    if lands_in_near_bound(hit.d, max_dist, bounds.get(&hit.source_path)) {
                        reached.insert(hit.source_path);
                    }
                }
            }
        }
        Ok(reached)
    }

    /// pg `touched` joined to `document`: every note whose project is one of the projects of
    /// the changed notes (a note with no project touches none).
    ///
    /// # Errors
    /// A query failed.
    pub async fn docs_in_projects_of(&self, changed: &[String]) -> Result<Vec<String>> {
        let mut projects = BTreeSet::new();
        for batch in changed.chunks(BATCH) {
            let sql = format!(
                "SELECT VALUE project FROM document WHERE source_path IN {};",
                texts(batch)
            );
            projects.extend(first_statement(&sql, self.rows::<String>(&sql).await?)?);
        }
        projects.remove("");
        let projects: Vec<String> = projects.into_iter().collect();
        let mut paths = Vec::new();
        for batch in projects.chunks(BATCH) {
            let sql = format!(
                "SELECT VALUE source_path FROM document WHERE project IN {};",
                texts(batch)
            );
            paths.extend(first_statement(&sql, self.rows::<String>(&sql).await?)?);
        }
        Ok(paths)
    }

    /// Raise a note's `ingested_us` when its semantic edges (`SEMANTIC_EDGE_KINDS`, as
    /// `(kind, dst)`) are about to differ from `expected` in the way `change` says. The edge
    /// writes that follow are separate commits and Surreal's graph write cannot say whether it
    /// inserted anything, so the comparison is made here, before the first delete, and a crash
    /// between the two leaves the note already marked.
    ///
    /// # Errors
    /// A statement failed, or the note stayed in conflict for more than `tx_retries` resends.
    pub async fn bump_ingested_if_edges_changed(
        &self,
        path: &str,
        expected: &[(String, String)],
        change: EdgeChange,
    ) -> Result<()> {
        let kinds: Vec<String> = SEMANTIC_EDGE_KINDS
            .iter()
            .map(|k| (*k).to_owned())
            .collect();
        let read = format!(
            "SELECT kind, dst FROM edge WHERE src = {} AND kind INSIDE {};",
            text(&doc_node(path)),
            texts(&kinds)
        );
        let held: BTreeSet<(String, String)> =
            first_statement(&read, self.rows::<KindDst>(&read).await?)?
                .into_iter()
                .map(|e| (e.kind, e.dst))
                .collect();
        let wanted: BTreeSet<(String, String)> = expected.iter().cloned().collect();
        let moves = match change {
            EdgeChange::Replaced => held != wanted,
            EdgeChange::Added => !wanted.is_subset(&held),
        };
        if !moves {
            return Ok(());
        }
        self.ensure_via_field().await?;
        let p = text(path);
        let sql = format!(
            "UPSERT type::record('doc_guard', {p}) SET n = (n ?? 0) + 1;
             UPDATE document SET ingested_us = time::micros(time::now()) WHERE source_path = {p};"
        );
        self.retrying(TxPlace::Doc, || async {
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }
}
