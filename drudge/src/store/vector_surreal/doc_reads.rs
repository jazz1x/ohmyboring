//! C-SURALL2 C0a: the document / graph / consumption reads that still asked pg, answered from the
//! `document`, `chunk`, `node` and `edge` copies on `backend=surreal`. pg is not asked; a Surreal
//! failure is an error, never a pg fallback.
//!
//! Each reader is the SurrealQL of one pg query in `store.rs`, condition for condition. Where pg
//! joins, groups or takes "the first N", the pick is made here in Rust over a fetch ordered by the
//! same keys, so a tie comes out one way. Rows tied on every sort key have no defined order on
//! either side. Where pg orders by the column collation, Surreal's rows are sorted with
//! `en_us_order`.
//!
//! A chunk's place in its note is the number after the last `#` of its key (`path#idx`, the key
//! `ingest` writes), because the `chunk` copy carries no `chunk_idx` column.

use std::cmp::Ordering;
use std::collections::{BTreeSet, HashMap, HashSet};

use anyhow::{Context, Result, anyhow, ensure};
use chrono::{DateTime, NaiveDateTime, Utc};
use serde::Deserialize;

use super::reads::en_us_order;
use super::{
    NOT_USER_MEMORY_RE, SurrealClient, first_statement, micros_to_time, now_micros, text, texts,
};
use crate::approved::EdgeRow;
use crate::store::{
    Authorship, ConsumptionCounts, ContestedBy, GraphStats, Meta, RankFacts, RecentDoc,
    SemanticStats,
};

const HOUR_US: i64 = 3_600_000_000;
const DAY_US: i64 = 86_400_000_000;
/// Values per `IN [...]` list: a request body over 1 MiB is refused (HTTP 413).
const IN_BATCH: usize = 500;
/// The shared prefix of a document's graph node id (`doc:<source_path>`).
const DOC_PREFIX: &str = "doc:";

#[derive(Deserialize)]
struct DocRow {
    source_path: String,
    project: String,
    #[serde(default)]
    tags: Vec<String>,
    updated_us: i64,
}

#[derive(Deserialize)]
struct AuthorRow {
    source_path: String,
    author: String,
    updated_us: i64,
}

#[derive(Deserialize)]
struct ChunkText {
    source_path: String,
    cid: String,
    content: String,
}

#[derive(Deserialize)]
struct ChunkMeta {
    source_path: String,
    origin: String,
    project: String,
}

#[derive(Deserialize)]
struct KindRow {
    source_path: String,
    kind: String,
}

#[derive(Deserialize)]
struct NodeLabel {
    cid: String,
    label: String,
}

#[derive(Deserialize)]
struct SessionNode {
    cid: String,
    label: String,
}

#[derive(Deserialize)]
struct EdgeFull {
    src: String,
    dst: String,
    kind: String,
    #[serde(default)]
    judge: Option<String>,
}

#[derive(Deserialize)]
struct Count {
    n: i64,
}

#[derive(Deserialize)]
struct Project {
    project: String,
}

fn dst_to_path(dst: &str) -> String {
    dst.strip_prefix(DOC_PREFIX).unwrap_or(dst).to_owned()
}

fn index_of(cid: &str) -> Result<i64> {
    cid.rsplit_once('#')
        .and_then(|(_, i)| i.parse::<i64>().ok())
        .ok_or_else(|| anyhow!("chunk key {cid:?} has no `#<index>` tail"))
}

/// pg's `label::timestamptz` for a session label (RFC 3339; a label without an offset is read as
/// UTC, the pg server's zone). A label that is not a timestamp never enters the window.
fn session_time(label: &str) -> Option<i64> {
    DateTime::parse_from_rfc3339(label)
        .map(|t| t.with_timezone(&Utc).timestamp_micros())
        .ok()
        .or_else(|| {
            NaiveDateTime::parse_from_str(label, "%Y-%m-%dT%H:%M:%S%.f")
                .ok()
                .map(|t| t.and_utc().timestamp_micros())
        })
}

/// pg's `label ~ '^\d{4}-\d{2}-\d{2}T'`.
fn has_date_time_prefix(label: &str) -> bool {
    let b = label.as_bytes();
    b.len() > 10
        && b[..4].iter().all(u8::is_ascii_digit)
        && b[4] == b'-'
        && b[5..7].iter().all(u8::is_ascii_digit)
        && b[7] == b'-'
        && b[8..10].iter().all(u8::is_ascii_digit)
        && b[10] == b'T'
}

fn by_count_then_name(a: &(String, i64), b: &(String, i64)) -> Ordering {
    b.1.cmp(&a.1).then_with(|| en_us_order(&a.0, &b.0))
}

impl SurrealClient {
    async fn doc_rows(&self, sql: &str) -> Result<Vec<DocRow>> {
        first_statement(sql, self.rows::<DocRow>(sql).await?)
    }

    /// The `document` rows of `paths`, keyed by path (a path with no row is absent).
    async fn docs_by_path(&self, paths: &[String]) -> Result<HashMap<String, DocRow>> {
        let mut found = HashMap::new();
        for batch in paths.chunks(IN_BATCH) {
            let sql = format!(
                "SELECT source_path, project, tags, updated_us FROM document WHERE source_path IN {};",
                texts(batch)
            );
            for d in self.doc_rows(&sql).await? {
                found.insert(d.source_path.clone(), d);
            }
        }
        Ok(found)
    }

    /// Each path's chunks joined by newline in chunk order (pg `string_agg(.. ORDER BY chunk_idx)`);
    /// a path with no chunk is absent.
    async fn contents(&self, paths: &[String]) -> Result<HashMap<String, String>> {
        let mut parts: HashMap<String, Vec<(i64, String)>> = HashMap::new();
        for batch in paths.chunks(IN_BATCH) {
            let sql = format!(
                "SELECT source_path, cid, content FROM chunk WHERE source_path IN {};",
                texts(batch)
            );
            for c in first_statement(&sql, self.rows::<ChunkText>(&sql).await?)? {
                parts
                    .entry(c.source_path)
                    .or_default()
                    .push((index_of(&c.cid)?, c.content));
            }
        }
        Ok(parts
            .into_iter()
            .map(|(path, mut chunks)| {
                chunks.sort_by_key(|(i, _)| *i);
                let body: Vec<String> = chunks.into_iter().map(|(_, c)| c).collect();
                (path, body.join("\n"))
            })
            .collect())
    }

    /// The `edge` rows whose `field` (`src` or `dst`) is one of `values` and that satisfy `extra`
    /// (a SurrealQL condition, `true` for none), in key order.
    async fn edges_by(&self, field: &str, values: &[String], extra: &str) -> Result<Vec<EdgeFull>> {
        let mut found = Vec::new();
        for batch in values.chunks(IN_BATCH) {
            let sql = format!(
                "SELECT src, dst, kind, judge FROM edge WHERE {field} IN {} AND ({extra}) ORDER BY cid;",
                texts(batch)
            );
            found.extend(first_statement(&sql, self.rows::<EdgeFull>(&sql).await?)?);
        }
        Ok(found)
    }

    async fn labels_of(&self, ids: &[String]) -> Result<HashMap<String, String>> {
        let mut labels = HashMap::new();
        for batch in ids.chunks(IN_BATCH) {
            let sql = format!("SELECT cid, label FROM node WHERE cid IN {};", texts(batch));
            for n in first_statement(&sql, self.rows::<NodeLabel>(&sql).await?)? {
                labels.insert(n.cid, n.label);
            }
        }
        Ok(labels)
    }

    /// One `count() ... GROUP ALL` statement per entry, in order. A table with no row has no group
    /// row: that is a count of zero (the same reading `claim_reads` and `log_copy` take).
    async fn counts(&self, statements: &[String]) -> Result<Vec<usize>> {
        let sql = statements.join("\n");
        let answers = self.rows::<Count>(&sql).await?;
        ensure!(
            answers.len() == statements.len(),
            "surreal answered {} statements to {}: {sql}",
            answers.len(),
            statements.len()
        );
        answers
            .into_iter()
            .map(|rows| {
                let n = rows.first().map_or(0, |c| c.n);
                usize::try_from(n).with_context(|| format!("count {n} is negative"))
            })
            .collect()
    }

    /// `Store::get_doc_sha`.
    pub async fn get_doc_sha(&self, path: &str) -> Result<Option<String>> {
        let sql = format!(
            "SELECT VALUE sha FROM document WHERE source_path = {};",
            text(path)
        );
        Ok(first_statement(&sql, self.rows::<String>(&sql).await?)?
            .into_iter()
            .next())
    }

    /// `Store::all_doc_paths`.
    pub async fn all_doc_paths(&self) -> Result<Vec<String>> {
        let sql = "SELECT VALUE source_path FROM document;";
        first_statement(sql, self.rows::<String>(sql).await?)
    }

    /// `Store::recent_docs`: newest documents with their chunks joined (a document with no chunk is
    /// not a row, as pg's inner join).
    pub async fn recent_docs(
        &self,
        limit: i64,
        exclude_origins: &[String],
        since_hours: Option<i32>,
        project: Option<&str>,
    ) -> Result<Vec<RecentDoc>> {
        let mut conds = vec![
            format!("!(origin IN {})", texts(exclude_origins)),
            format!(
                "!string::matches(source_path, <regex> {})",
                text(NOT_USER_MEMORY_RE)
            ),
        ];
        if let Some(hours) = since_hours {
            conds.push(format!(
                "updated_us >= {}",
                now_micros()? - i64::from(hours) * HOUR_US
            ));
        }
        if let Some(p) = project {
            conds.push(format!("project = {}", text(p)));
        }
        let sql = format!(
            "SELECT source_path, project, tags, updated_us FROM document WHERE {}
              ORDER BY updated_us DESC, source_path ASC;",
            conds.join(" AND ")
        );
        let docs = self.doc_rows(&sql).await?;
        self.with_content(docs, limit).await
    }

    /// The first `limit` of `docs` (already in answer order) that have chunks, each with its text.
    async fn with_content(&self, docs: Vec<DocRow>, limit: i64) -> Result<Vec<RecentDoc>> {
        let limit = usize::try_from(limit).with_context(|| format!("limit {limit} is negative"))?;
        let mut out = Vec::new();
        if limit == 0 {
            return Ok(out);
        }
        for page in docs.chunks(limit) {
            let paths: Vec<String> = page.iter().map(|d| d.source_path.clone()).collect();
            let mut bodies = self.contents(&paths).await?;
            for d in page {
                if let Some(content) = bodies.remove(&d.source_path) {
                    out.push(RecentDoc {
                        source_path: d.source_path.clone(),
                        project: d.project.clone(),
                        content,
                        tags: d.tags.clone(),
                    });
                }
            }
            if out.len() >= limit {
                out.truncate(limit);
                break;
            }
        }
        Ok(out)
    }

    /// The documents sharing at least two non-project, non-topic nodes with `doc_id`, most shared
    /// first (ties by node id): pg's `related_docs` ranking before its `LIMIT`.
    async fn shared_nodes(&self, doc_id: &str) -> Result<Vec<(String, i64)>> {
        let own = self
            .edges_by("src", &[doc_id.to_owned()], "true")
            .await?
            .into_iter()
            .filter(|e| !e.dst.starts_with("project:") && !e.dst.starts_with("topic:"));
        let mut weight: HashMap<String, i64> = HashMap::new();
        for e in own {
            *weight.entry(e.dst).or_default() += 1;
        }
        let dsts: Vec<String> = weight.keys().cloned().collect();
        let extra = format!(
            "src != {} AND string::starts_with(src, {})",
            text(doc_id),
            text(DOC_PREFIX)
        );
        let mut shared: HashMap<String, i64> = HashMap::new();
        for e in self.edges_by("dst", &dsts, &extra).await? {
            *shared.entry(e.src).or_default() += weight.get(&e.dst).copied().unwrap_or(0);
        }
        let mut ranked: Vec<(String, i64)> = shared.into_iter().filter(|(_, n)| *n >= 2).collect();
        ranked.sort_by(by_count_then_name);
        Ok(ranked)
    }

    /// `Store::related_docs`.
    pub async fn related_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let doc_id = format!("{DOC_PREFIX}{source_path}");
        let take = usize::try_from(limit).unwrap_or(0);
        Ok(self
            .shared_nodes(&doc_id)
            .await?
            .into_iter()
            .take(take)
            .map(|(src, _)| dst_to_path(&src))
            .collect())
    }

    /// `Store::related_doc_content`: the same ranking, each document with its chunks joined.
    pub async fn related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
    ) -> Result<Vec<RecentDoc>> {
        let doc_id = format!("{DOC_PREFIX}{source_path}");
        let take = usize::try_from(limit).unwrap_or(0);
        let ranked: Vec<(String, i64)> = self
            .shared_nodes(&doc_id)
            .await?
            .into_iter()
            .take(take)
            .collect();
        self.joined_docs(&ranked).await
    }

    /// Ranked `(doc node id, weight)` entries turned into documents with their text, in rank order;
    /// an entry whose document or chunks are missing is not a row (pg's inner joins).
    async fn joined_docs(&self, ranked: &[(String, i64)]) -> Result<Vec<RecentDoc>> {
        let paths: Vec<String> = ranked.iter().map(|(id, _)| dst_to_path(id)).collect();
        let docs = self.docs_by_path(&paths).await?;
        let mut bodies = self.contents(&paths).await?;
        Ok(paths
            .into_iter()
            .filter_map(|path| {
                let d = docs.get(&path)?;
                let content = bodies.remove(&path)?;
                Some(RecentDoc {
                    source_path: path,
                    project: d.project.clone(),
                    content,
                    tags: d.tags.clone(),
                })
            })
            .collect())
    }

    /// `Store::related_by_shared_ground`: older notes asserting the same claim (weight 2) or about
    /// the same concept (weight 1), at least `min_shared` of weight, best first.
    pub async fn related_by_shared_ground(
        &self,
        source_path: &str,
        limit: i64,
        min_shared: i64,
    ) -> Result<Vec<RecentDoc>> {
        let doc_id = format!("{DOC_PREFIX}{source_path}");
        let take = usize::try_from(limit).unwrap_or(0);
        let Some(this) = self
            .docs_by_path(&[source_path.to_owned()])
            .await?
            .remove(source_path)
        else {
            // pg compares against a NULL `updated_at`: no row is older than nothing.
            return Ok(Vec::new());
        };
        let own = self
            .edges_by("src", std::slice::from_ref(&doc_id), "true")
            .await?;
        let concepts: Vec<String> = own
            .iter()
            .filter(|e| e.kind == "about" && e.dst.starts_with("concept:"))
            .map(|e| e.dst.clone())
            .collect();
        let claims: Vec<String> = own
            .iter()
            .filter(|e| e.kind == "claims")
            .map(|e| e.dst.clone())
            .collect();
        let other = format!("src != {}", text(&doc_id));
        let mut weight: HashMap<String, i64> = HashMap::new();
        for e in self
            .edges_by("dst", &claims, &format!("{other} AND kind = 'claims'"))
            .await?
        {
            *weight.entry(e.src).or_default() += 2;
        }
        for e in self
            .edges_by("dst", &concepts, &format!("{other} AND kind = 'about'"))
            .await?
        {
            *weight.entry(e.src).or_default() += 1;
        }
        let nodes: Vec<String> = weight.keys().cloned().collect();
        let paths: Vec<String> = nodes.iter().map(|n| dst_to_path(n)).collect();
        let older: HashSet<String> = self
            .docs_by_path(&paths)
            .await?
            .into_values()
            .filter(|d| d.updated_us < this.updated_us)
            .map(|d| format!("{DOC_PREFIX}{}", d.source_path))
            .collect();
        let mut ranked: Vec<(String, i64)> = weight
            .into_iter()
            .filter(|(node, n)| older.contains(node) && *n >= min_shared)
            .collect();
        ranked.sort_by(by_count_then_name);
        ranked.truncate(take);
        self.joined_docs(&ranked).await
    }

    /// `Store::recent_project_docs`.
    pub async fn recent_project_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let Some(this) = self
            .docs_by_path(&[source_path.to_owned()])
            .await?
            .remove(source_path)
        else {
            return Ok(Vec::new());
        };
        if this.project.is_empty() {
            return Ok(Vec::new());
        }
        let take = usize::try_from(limit).unwrap_or(0);
        let sql = format!(
            "SELECT VALUE source_path FROM document WHERE project = {} AND source_path != {}
              ORDER BY updated_us DESC, source_path ASC LIMIT {take};",
            text(&this.project),
            text(source_path)
        );
        first_statement(&sql, self.rows::<String>(&sql).await?)
    }

    /// `Store::all_meta`: one row per chunk; `kind` is the note's (pg stores it on the chunk too).
    pub async fn all_meta(&self) -> Result<Vec<Meta>> {
        let chunks_sql = "SELECT source_path, origin, project FROM chunk;";
        let chunks: Vec<ChunkMeta> =
            first_statement(chunks_sql, self.rows::<ChunkMeta>(chunks_sql).await?)?;
        let kinds_sql = "SELECT source_path, kind FROM document;";
        let kinds: HashMap<String, String> =
            first_statement(kinds_sql, self.rows::<KindRow>(kinds_sql).await?)?
                .into_iter()
                .map(|k| (k.source_path, k.kind))
                .collect();
        chunks
            .into_iter()
            .map(|c| {
                let kind = kinds.get(&c.source_path).cloned().ok_or_else(|| {
                    anyhow!("chunk of {} has no document row in surreal", c.source_path)
                })?;
                Ok(Meta {
                    origin: c.origin,
                    project: c.project,
                    kind,
                    source_path: c.source_path,
                })
            })
            .collect()
    }

    /// `Store::project_names`: distinct non-empty project names, in the collation's order.
    pub async fn project_names(&self) -> Result<Vec<String>> {
        let sql = "SELECT project FROM document WHERE project != '';";
        let rows: Vec<Project> = first_statement(sql, self.rows::<Project>(sql).await?)?;
        let mut names: Vec<String> = rows
            .into_iter()
            .map(|p| p.project)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        names.sort_by(|a, b| en_us_order(a, b));
        Ok(names)
    }

    /// `Store::active_project_counts`: projects with a document updated within `days`, busiest first.
    pub async fn active_project_counts(&self, days: i32) -> Result<Vec<(String, i64)>> {
        let sql = format!(
            "SELECT project FROM document WHERE project != '' AND updated_us > {};",
            now_micros()? - i64::from(days) * DAY_US
        );
        let rows: Vec<Project> = first_statement(&sql, self.rows::<Project>(&sql).await?)?;
        let mut per: HashMap<String, i64> = HashMap::new();
        for p in rows {
            *per.entry(p.project).or_default() += 1;
        }
        let mut counts: Vec<(String, i64)> = per.into_iter().collect();
        counts.sort_by(by_count_then_name);
        Ok(counts)
    }

    /// `Store::approved_edges`.
    pub async fn approved_edges(&self) -> Result<Vec<EdgeRow>> {
        let sql = "SELECT src, dst, kind, judge FROM edge
                    WHERE string::starts_with(src, 'session:slack:') AND kind IN ['used', 'contested']
                    ORDER BY cid;";
        Ok(first_statement(sql, self.rows::<EdgeFull>(sql).await?)?
            .into_iter()
            .map(|e| EdgeRow {
                src: e.src,
                dst: e.dst,
                kind: e.kind,
            })
            .collect())
    }

    /// `Store::counts_by`: `used` / `contested` edges per document node; on an owner note the
    /// owner's own objection is the only one counted when `contested_by` says so.
    pub(in crate::store) async fn consumption_counts_by(
        &self,
        paths: &[String],
        contested_by: ContestedBy,
    ) -> Result<HashMap<String, ConsumptionCounts>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node(p)).collect();
        let edges = self
            .edges_by("dst", &doc_ids, "kind IN ['used', 'contested']")
            .await?;
        let owner_notes = match contested_by {
            ContestedBy::AnyJudge => HashSet::new(),
            ContestedBy::OwnerOnOwnerNotes => self.owner_notes(&edges).await?,
        };
        let mut counts: HashMap<String, ConsumptionCounts> = HashMap::new();
        for e in edges {
            let objection_of_other = e.kind == "contested"
                && owner_notes.contains(&e.dst)
                && e.judge.as_deref() != Some("owner");
            if objection_of_other {
                continue;
            }
            let entry = counts.entry(dst_to_path(&e.dst)).or_default();
            if e.kind == "used" {
                entry.used += 1;
            } else {
                entry.contested += 1;
            }
        }
        Ok(counts)
    }

    /// The doc node ids among the `dst` of `edges` whose note the owner wrote.
    async fn owner_notes(&self, edges: &[EdgeFull]) -> Result<HashSet<String>> {
        let paths: Vec<String> = edges
            .iter()
            .map(|e| dst_to_path(&e.dst))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let authors = self.authors_of(&paths).await?;
        Ok(authors
            .into_iter()
            .filter(|(_, a)| a.author == "owner")
            .map(|(path, _)| doc_node(&path))
            .collect())
    }

    /// `Store::owner_authored`: which of `paths` the owner wrote, in path order.
    pub async fn owner_authored(&self, paths: &[String]) -> Result<Vec<String>> {
        let mut owned: Vec<String> = self
            .authors_of(paths)
            .await?
            .into_iter()
            .filter(|(_, a)| a.author == "owner")
            .map(|(path, _)| path)
            .collect();
        owned.sort_by(|a, b| en_us_order(a, b));
        Ok(owned)
    }

    /// The paths among `paths` that have a `document` row (`record_gap_in_tx`'s handed notes).
    pub async fn known_paths(&self, paths: &[String]) -> Result<HashSet<String>> {
        Ok(self.authors_of(paths).await?.into_keys().collect())
    }

    async fn authors_of(&self, paths: &[String]) -> Result<HashMap<String, AuthorRow>> {
        let mut found = HashMap::new();
        for batch in paths.chunks(IN_BATCH) {
            let sql = format!(
                "SELECT source_path, author, updated_us FROM document WHERE source_path IN {};",
                texts(batch)
            );
            for a in first_statement(&sql, self.rows::<AuthorRow>(&sql).await?)? {
                found.insert(a.source_path.clone(), a);
            }
        }
        Ok(found)
    }

    /// `Store::concepts_of`: the `concept` labels each note is `about`, in label order.
    pub async fn concepts_of(&self, paths: &[String]) -> Result<HashMap<String, Vec<String>>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node(p)).collect();
        let edges = self.edges_by("src", &doc_ids, "kind = 'about'").await?;
        let dsts: Vec<String> = edges.iter().map(|e| e.dst.clone()).collect();
        let concept_labels = self.concept_labels(&dsts).await?;
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        for e in edges {
            if let Some(label) = concept_labels.get(&e.dst) {
                map.entry(dst_to_path(&e.src))
                    .or_default()
                    .push(label.clone());
            }
        }
        for labels in map.values_mut() {
            labels.sort_by(|a, b| en_us_order(a, b));
        }
        Ok(map)
    }

    async fn concept_labels(&self, ids: &[String]) -> Result<HashMap<String, String>> {
        let mut labels = HashMap::new();
        for batch in ids.chunks(IN_BATCH) {
            let sql = format!(
                "SELECT cid, label FROM node WHERE kind = 'concept' AND cid IN {};",
                texts(batch)
            );
            for n in first_statement(&sql, self.rows::<NodeLabel>(&sql).await?)? {
                labels.insert(n.cid, n.label);
            }
        }
        Ok(labels)
    }

    /// `Store::superseded_by`: the newer notes whose `supersedes` edge points at each path.
    pub async fn superseded_by(&self, paths: &[String]) -> Result<HashMap<String, Vec<String>>> {
        let doc_ids: Vec<String> = paths.iter().map(|p| doc_node(p)).collect();
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        for e in self
            .edges_by("dst", &doc_ids, "kind = 'supersedes'")
            .await?
        {
            map.entry(dst_to_path(&e.dst))
                .or_default()
                .push(dst_to_path(&e.src));
        }
        Ok(map)
    }

    /// `Store::rank_facts`.
    pub async fn rank_facts(&self, paths: &[String]) -> Result<HashMap<String, RankFacts>> {
        let authors = self.authors_of(paths).await?;
        let doc_ids: Vec<String> = authors.keys().map(|p| doc_node(p)).collect();
        let replaced: HashSet<String> = self
            .edges_by("dst", &doc_ids, "kind = 'supersedes'")
            .await?
            .into_iter()
            .map(|e| e.dst)
            .collect();
        authors
            .into_iter()
            .map(|(path, a)| {
                let authorship = if a.author == "owner" {
                    Authorship::Owner {
                        updated_at: micros_to_time(a.updated_us, "document updated_us")?,
                    }
                } else {
                    Authorship::Other
                };
                let facts = RankFacts {
                    superseded: replaced.contains(&doc_node(&path)),
                    authorship,
                };
                Ok((path, facts))
            })
            .collect()
    }

    /// `Store::reused_recently`: documents with the most `used` edges from sessions observed within
    /// `days`; the `limit` cuts the per-document tally before the document join, as pg.
    pub async fn reused_recently(
        &self,
        days: i64,
        limit: i64,
    ) -> Result<Vec<(RecentDoc, i64, i64)>> {
        let since = now_micros()? - days * DAY_US;
        let sessions_sql = "SELECT cid, label FROM node WHERE kind = 'session';";
        let in_window: Vec<String> =
            first_statement(sessions_sql, self.rows::<SessionNode>(sessions_sql).await?)?
                .into_iter()
                .filter(|s| has_date_time_prefix(&s.label))
                .filter(|s| session_time(&s.label).is_some_and(|t| t >= since))
                .map(|s| s.cid)
                .collect();
        let mut tally: HashMap<String, (i64, i64)> = HashMap::new();
        for e in self
            .edges_by("src", &in_window, "kind IN ['used', 'contested']")
            .await?
        {
            let entry = tally.entry(e.dst).or_default();
            if e.kind == "used" {
                entry.0 += 1;
            } else {
                entry.1 += 1;
            }
        }
        let mut ranked: Vec<(String, (i64, i64))> = tally.into_iter().collect();
        ranked.sort_by(|a, b| {
            b.1.0
                .cmp(&a.1.0)
                .then_with(|| b.1.1.cmp(&a.1.1))
                .then_with(|| en_us_order(&a.0, &b.0))
        });
        ranked.truncate(usize::try_from(limit).unwrap_or(0));
        let paths: Vec<String> = ranked.iter().map(|(n, _)| dst_to_path(n)).collect();
        let docs = self.docs_by_path(&paths).await?;
        let mut bodies = self.contents(&paths).await?;
        let mut rows: Vec<(RecentDoc, i64, i64)> = ranked
            .into_iter()
            .filter_map(|(node, (used, contested))| {
                let path = dst_to_path(&node);
                let d = docs.get(&path)?;
                let doc = RecentDoc {
                    content: bodies.remove(&path).unwrap_or_default(),
                    project: d.project.clone(),
                    tags: d.tags.clone(),
                    source_path: path,
                };
                Some((doc, used, contested))
            })
            .collect();
        rows.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then_with(|| b.2.cmp(&a.2))
                .then_with(|| en_us_order(&a.0.source_path, &b.0.source_path))
        });
        Ok(rows)
    }

    /// `Store::graph_neighbors` / `Store::semantic_neighbors`: the labels of the nodes the note's
    /// edges of these `kinds` reach.
    pub async fn neighbor_labels(&self, chunk_id: &str, kinds: &[&str]) -> Result<Vec<String>> {
        let path = chunk_id.rsplit_once('#').map_or(chunk_id, |(p, _)| p);
        let doc_id = format!("{DOC_PREFIX}{path}");
        let kind_list: Vec<String> = kinds.iter().map(|k| (*k).to_owned()).collect();
        let edges = self
            .edges_by("src", &[doc_id], &format!("kind IN {}", texts(&kind_list)))
            .await?;
        let dsts: Vec<String> = edges.iter().map(|e| e.dst.clone()).collect();
        let labels = self.labels_of(&dsts).await?;
        Ok(edges
            .into_iter()
            .filter_map(|e| labels.get(&e.dst).cloned())
            .collect())
    }

    /// `Store::count`: chunks.
    pub async fn chunk_count(&self) -> Result<usize> {
        let counts = self
            .counts(&["SELECT count() AS n FROM chunk GROUP ALL;".to_owned()])
            .await?;
        Ok(counts[0])
    }

    /// `Store::graph_stats`.
    pub async fn graph_stats(&self) -> Result<GraphStats> {
        let node =
            |kind: &str| format!("SELECT count() AS n FROM node WHERE kind = '{kind}' GROUP ALL;");
        let n = self
            .counts(&[
                "SELECT count() AS n FROM document GROUP ALL;".to_owned(),
                "SELECT count() AS n FROM chunk GROUP ALL;".to_owned(),
                node("project"),
                node("topic"),
                node("claim"),
                "SELECT count() AS n FROM (SELECT dst FROM edge WHERE kind = 'claims' GROUP BY dst) GROUP ALL;"
                    .to_owned(),
                node("decision"),
                node("risk"),
                "SELECT count() AS n FROM edge GROUP ALL;".to_owned(),
            ])
            .await?;
        Ok(GraphStats {
            documents: n[0],
            chunks: n[1],
            projects: n[2],
            topics: n[3],
            claims: n[4],
            claims_with_doc: n[5],
            decisions: n[6],
            risks: n[7],
            edges: n[8],
        })
    }

    /// `Store::semantic_stats`.
    pub async fn semantic_stats(&self) -> Result<SemanticStats> {
        let node =
            |kind: &str| format!("SELECT count() AS n FROM node WHERE kind = '{kind}' GROUP ALL;");
        let edge =
            |kind: &str| format!("SELECT count() AS n FROM edge WHERE kind = '{kind}' GROUP ALL;");
        let n = self
            .counts(&[node("tool"), node("concept"), edge("uses"), edge("about")])
            .await?;
        Ok(SemanticStats {
            tools: n[0],
            concepts: n[1],
            uses: n[2],
            about: n[3],
        })
    }
}

fn doc_node(path: &str) -> String {
    format!("{DOC_PREFIX}{path}")
}
