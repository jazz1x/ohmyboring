//! C-SURALL2 A4: the four log tables (`event_log`, `query_log`, `recall_label`, `sync_state`) copied
//! from pg into Surreal, values included, by a pass that reads pg.
//!
//! Why a pass and not a write-through like `SurrealClient::mirror`: these tables are written by
//! several paths that share no function (Rust and Python both insert `event_log` and `query_log`,
//! the host spool replays through `/events`, `spawn_query_log` is fire-and-forget), and the insert
//! returns no id, so there is no scope to name after a commit. pg stays the original and is read
//! as it is.
//!
//! * `event_log`, `query_log` — rows never change after insert, ids come from a pg sequence.
//!   A sequence id is taken at insert and visible at commit, so a row with a lower id can commit
//!   after a higher one was already copied: a watermark alone would skip it for good. The pass
//!   copies every pg id above `max(surreal id) - WINDOW_IDS` that Surreal lacks.
//! * deletion — below the window the pass only deletes: every Surreal id pg no longer has goes
//!   (the 90-day prune of `query_log`, compared on ids rather than re-implementing the cutoff:
//!   one rule in one place, and a manual delete follows too). `recall_label` rows leave with their
//!   `query_log` row (pg CASCADE) and are found by the whole compare below.
//! * `recall_label`, `sync_state` — a few thousand rows whose values change (a label's verdict
//!   is upserted), so every pass upserts all of pg's rows and deletes the keys pg lacks.
//!
//! Upsert first, delete second, no transaction: a pass that fails half way leaves Surreal with
//! extra rows, never fewer, and the next pass (which re-derives everything from pg) converges.

use std::collections::{HashMap, HashSet};

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::Value;

use super::Store;
use super::vector_surreal::{SurrealClient, first_statement};

/// How far below Surreal's highest copied id the pass still looks for ids Surreal lacks. A row's id
/// is taken when its transaction inserts and shows up when it commits, so a gap needs
/// (ids allocated meanwhile) < window: at the live rate (about 1-2 rows a minute per table,
/// 104,245 / 71,779 rows in 90 days) 2,000 ids cover more than a day of a stuck transaction, and a
/// pass costs one id-list read from each side, not a row read.
/// Chosen over "last N minutes" because `observed_at`/`created_at` can be older than the commit
/// (the host spool replays rows hours later; a client-side timestamp would slip a time window).
const WINDOW_IDS: i64 = 2000;
/// pg rows fetched per round trip while copying, and the bytes of one Surreal request (the server
/// refuses a body over 1 MiB, HTTP 413).
const PG_ROWS_PER_READ: usize = 200;
const REQUEST_BYTES: usize = 384 * 1024;

/// How a table's rows are keyed and re-read.
enum Kind {
    /// Immutable rows with a pg sequence id.
    Sequence,
    /// Rows keyed by `cid`, all of them every pass.
    Whole,
}

/// One table: `row` is a `jsonb_build_object(..)` over the pg columns — `cid` (the Surreal record
/// key), `pid` on the sequence tables, and every column as a value Surreal reads back unchanged
/// (timestamps as epoch microseconds, the convention of the vector tables; jsonb as its text).
struct Spec {
    table: &'static str,
    kind: Kind,
    row: &'static str,
}

impl Spec {
    /// pg ids of a sequence table.
    fn ids_sql(&self) -> String {
        format!("SELECT id::bigint FROM {} ORDER BY id", self.table)
    }

    /// `(id, json)` of the sequence rows whose id is in `$1`.
    fn rows_by_id_sql(&self) -> String {
        format!(
            "SELECT (j->>'pid')::bigint, j::text FROM (SELECT {row} AS j FROM {table} \
             WHERE id = ANY($1::bigint[])) s ORDER BY 1",
            row = self.row,
            table = self.table
        )
    }

    /// `(cid, json)` of every row.
    fn all_rows_sql(&self) -> String {
        format!(
            "SELECT j->>'cid', j::text FROM (SELECT {row} AS j FROM {table}) s ORDER BY 1",
            row = self.row,
            table = self.table
        )
    }
}

const SPECS: [Spec; 4] = [
    Spec {
        table: "event_log",
        kind: Kind::Sequence,
        row: "jsonb_build_object(
    'cid', id::text, 'pid', id::bigint,
    'observed_us', (extract(epoch from observed_at) * 1000000)::bigint,
    'time_unix_nano', time_unix_nano, 'severity_text', severity_text,
    'severity_number', severity_number, 'service_name', service_name, 'component', component,
    'event_name', event_name, 'status', status, 'trace_id', trace_id, 'span_id', span_id,
    'run_id', run_id, 'session_id', session_id, 'workflow', workflow,
    'workflow_node', workflow_node, 'workflow_outcome', workflow_outcome,
    'body', body::text, 'attributes', attributes::text, 'resource', resource::text)",
    },
    Spec {
        table: "query_log",
        kind: Kind::Sequence,
        row: "jsonb_build_object(
    'cid', id::text, 'pid', id::bigint,
    'created_us', (extract(epoch from created_at) * 1000000)::bigint,
    'endpoint', endpoint, 'query', query, 'hit_paths', to_jsonb(hit_paths),
    'sources', to_jsonb(sources), 'answer_snippet', answer_snippet, 'latency_ms', latency_ms,
    'hit_dists', to_jsonb(hit_dists), 'hit_dist_kinds', to_jsonb(hit_dist_kinds))",
    },
    Spec {
        table: "recall_label",
        kind: Kind::Whole,
        row: "jsonb_build_object(
    'cid', query_log_id::text || ':' || hit_index::text || ':' || judge,
    'query_log_id', query_log_id, 'hit_index', hit_index, 'judge', judge, 'verdict', verdict,
    'model', model, 'note', note,
    'created_us', (extract(epoch from created_at) * 1000000)::bigint)",
    },
    Spec {
        table: "sync_state",
        kind: Kind::Whole,
        row: "jsonb_build_object(
    'cid', key, 'value_us', (extract(epoch from value) * 1000000)::bigint,
    'value_subject', value_subject, 'value_predicate', value_predicate)",
    },
];

/// What one pass did to one table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogCopy {
    pub table: &'static str,
    pub upserted: usize,
    pub deleted: usize,
}

impl LogCopy {
    #[must_use]
    pub fn line(&self) -> String {
        format!(
            "{}: upserted={} deleted={}",
            self.table, self.upserted, self.deleted
        )
    }
}

/// One table of the full value compare.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogCheck {
    pub table: &'static str,
    pub pg_rows: usize,
    pub surreal_rows: usize,
    /// pg rows Surreal lacks or holds with another value.
    pub differing: usize,
}

impl LogCheck {
    #[must_use]
    pub fn same(&self) -> bool {
        self.pg_rows == self.surreal_rows && self.differing == 0
    }

    #[must_use]
    pub fn line(&self) -> String {
        format!(
            "{}: pg {} | surreal {} | differing {}/{} | {}",
            self.table,
            self.pg_rows,
            self.surreal_rows,
            self.differing,
            self.pg_rows,
            if self.same() { "same" } else { "DIFFERENT" }
        )
    }
}

#[derive(Deserialize)]
struct Count {
    count: usize,
}

impl SurrealClient {
    async fn ensure_log_tables(&self) -> Result<()> {
        self.run(&format!(
            "DEFINE NAMESPACE IF NOT EXISTS {ns}; DEFINE DATABASE IF NOT EXISTS {db};
             DEFINE TABLE IF NOT EXISTS event_log SCHEMALESS;
             DEFINE INDEX IF NOT EXISTS event_log_pid ON event_log FIELDS pid;
             DEFINE TABLE IF NOT EXISTS query_log SCHEMALESS;
             DEFINE INDEX IF NOT EXISTS query_log_pid ON query_log FIELDS pid;
             DEFINE TABLE IF NOT EXISTS recall_label SCHEMALESS;
             DEFINE TABLE IF NOT EXISTS sync_state SCHEMALESS;",
            ns = self.namespace(),
            db = self.database()
        ))
        .await?;
        Ok(())
    }

    /// Upsert the json rows, several to a request while the request stays under the body limit.
    pub(super) async fn upsert_json(&self, table: &str, rows: &[String]) -> Result<()> {
        let mut batch: Vec<&str> = Vec::new();
        let mut bytes = 0;
        for row in rows {
            if !batch.is_empty() && bytes + row.len() > REQUEST_BYTES {
                self.send_json_batch(table, &batch).await?;
                batch.clear();
                bytes = 0;
            }
            bytes += row.len();
            batch.push(row);
        }
        if batch.is_empty() {
            Ok(())
        } else {
            self.send_json_batch(table, &batch).await
        }
    }

    async fn send_json_batch(&self, table: &str, batch: &[&str]) -> Result<()> {
        self.run(&format!(
            "FOR $r IN [{}] {{ UPSERT type::record('{table}', $r.cid) CONTENT $r; }};",
            batch.join(",")
        ))
        .await?;
        Ok(())
    }

    async fn surreal_column<T: serde::de::DeserializeOwned>(
        &self,
        table: &str,
        column: &str,
    ) -> Result<Vec<T>> {
        let query = format!("SELECT VALUE {column} FROM {table};");
        first_statement(&query, self.rows::<T>(&query).await?)
    }

    async fn copy_sequence(&self, store: &Store, spec: &Spec) -> Result<LogCopy> {
        let table = spec.table;
        let pg_ids: Vec<i64> = store
            .db()
            .await?
            .query(&spec.ids_sql(), &[])
            .await
            .with_context(|| format!("read {table} ids from pg"))?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let surreal_ids: Vec<i64> = self.surreal_column(table, "pid").await?;
        let floor = surreal_ids
            .iter()
            .max()
            .map_or(i64::MIN, |top| top.saturating_sub(WINDOW_IDS));
        let have: HashSet<i64> = surreal_ids.iter().copied().collect();
        let missing: Vec<i64> = pg_ids
            .iter()
            .copied()
            .filter(|id| *id > floor && !have.contains(id))
            .collect();
        let rows_sql = spec.rows_by_id_sql();
        for page in missing.chunks(PG_ROWS_PER_READ) {
            let rows: Vec<String> = store
                .db()
                .await?
                .query(&rows_sql, &[&page.to_vec()])
                .await
                .with_context(|| format!("read {table} rows from pg"))?
                .iter()
                .map(|r| r.get::<_, String>(1))
                .collect();
            self.upsert_json(table, &rows).await?;
        }
        let in_pg: HashSet<i64> = pg_ids.into_iter().collect();
        let stale: Vec<String> = surreal_ids
            .into_iter()
            .filter(|id| !in_pg.contains(id))
            .map(|id| id.to_string())
            .collect();
        self.delete_rows(table, &stale).await?;
        Ok(LogCopy {
            table,
            upserted: missing.len(),
            deleted: stale.len(),
        })
    }

    async fn copy_whole(&self, store: &Store, spec: &Spec) -> Result<LogCopy> {
        let table = spec.table;
        let pg_rows: Vec<(String, String)> = store
            .db()
            .await?
            .query(&spec.all_rows_sql(), &[])
            .await
            .with_context(|| format!("read {table} from pg"))?
            .iter()
            .map(|r| (r.get(0), r.get(1)))
            .collect();
        let surreal_keys: Vec<String> = self.surreal_column(table, "cid").await?;
        let literals: Vec<String> = pg_rows.iter().map(|(_, j)| j.clone()).collect();
        self.upsert_json(table, &literals).await?;
        let keep: HashSet<&str> = pg_rows.iter().map(|(k, _)| k.as_str()).collect();
        let stale: Vec<String> = surreal_keys
            .into_iter()
            .filter(|k| !keep.contains(k.as_str()))
            .collect();
        self.delete_rows(table, &stale).await?;
        Ok(LogCopy {
            table,
            upserted: literals.len(),
            deleted: stale.len(),
        })
    }

    /// One pass over the four tables, parents first (`query_log` before `recall_label`).
    ///
    /// # Errors
    /// A pg read or a Surreal request failed; pg is untouched and the next pass converges.
    pub async fn copy_logs(&self, store: &Store) -> Result<Vec<LogCopy>> {
        self.ensure_log_tables().await?;
        let mut done = Vec::with_capacity(SPECS.len());
        for spec in &SPECS {
            done.push(match spec.kind {
                Kind::Sequence => self.copy_sequence(store, spec).await?,
                Kind::Whole => self.copy_whole(store, spec).await?,
            });
        }
        Ok(done)
    }

    /// Surreal's rows of `table` whose `key` column is in `wanted` (SurrealQL literals), by key.
    pub(super) async fn surreal_rows_by(
        &self,
        table: &str,
        key: &str,
        wanted: &[String],
    ) -> Result<HashMap<String, Value>> {
        let query = format!(
            "SELECT * OMIT id FROM {table} WHERE {key} IN [{}];",
            wanted.join(",")
        );
        let rows: Vec<Value> = first_statement(&query, self.rows::<Value>(&query).await?)?;
        Ok(rows
            .into_iter()
            .map(|row| {
                let k = match row.get(key) {
                    Some(Value::String(s)) => s.clone(),
                    Some(other) => other.to_string(),
                    None => String::new(),
                };
                (k, row)
            })
            .collect())
    }

    async fn surreal_count(&self, table: &str) -> Result<usize> {
        let query = format!("SELECT count() FROM {table} GROUP ALL;");
        let rows: Vec<Count> = first_statement(&query, self.rows::<Count>(&query).await?)?;
        Ok(rows.first().map_or(0, |c| c.count))
    }

    /// Read-only: every pg row against Surreal's row of the same key, value by value.
    ///
    /// # Errors
    /// A read failed.
    pub async fn check_logs(&self, store: &Store) -> Result<Vec<LogCheck>> {
        let mut checks = Vec::with_capacity(SPECS.len());
        for spec in &SPECS {
            let (pg_rows, differing) = match spec.kind {
                Kind::Sequence => self.check_sequence(store, spec).await?,
                Kind::Whole => self.check_whole(store, spec).await?,
            };
            checks.push(LogCheck {
                table: spec.table,
                pg_rows,
                surreal_rows: self.surreal_count(spec.table).await?,
                differing,
            });
        }
        Ok(checks)
    }

    async fn check_sequence(&self, store: &Store, spec: &Spec) -> Result<(usize, usize)> {
        let ids: Vec<i64> = store
            .db()
            .await?
            .query(&spec.ids_sql(), &[])
            .await
            .context("read ids from pg")?
            .iter()
            .map(|r| r.get(0))
            .collect();
        let rows_sql = spec.rows_by_id_sql();
        let mut differing = 0;
        for page in ids.chunks(PG_ROWS_PER_READ) {
            let pg: Vec<(i64, String)> = store
                .db()
                .await?
                .query(&rows_sql, &[&page.to_vec()])
                .await
                .context("read rows from pg")?
                .iter()
                .map(|r| (r.get(0), r.get(1)))
                .collect();
            let wanted: Vec<String> = page.iter().map(ToString::to_string).collect();
            let there = self.surreal_rows_by(spec.table, "pid", &wanted).await?;
            for (id, json) in &pg {
                let want: Value = serde_json::from_str(json)?;
                if there.get(&id.to_string()) != Some(&want) {
                    differing += 1;
                }
            }
        }
        Ok((ids.len(), differing))
    }

    async fn check_whole(&self, store: &Store, spec: &Spec) -> Result<(usize, usize)> {
        let pg: Vec<(String, String)> = store
            .db()
            .await?
            .query(&spec.all_rows_sql(), &[])
            .await
            .context("read rows from pg")?
            .iter()
            .map(|r| (r.get(0), r.get(1)))
            .collect();
        let mut differing = 0;
        for page in pg.chunks(PG_ROWS_PER_READ) {
            let wanted: Vec<String> = page
                .iter()
                .map(|(k, _)| serde_json::to_string(k))
                .collect::<Result<_, _>>()?;
            let there = self.surreal_rows_by(spec.table, "cid", &wanted).await?;
            for (k, json) in page {
                let want: Value = serde_json::from_str(json)?;
                if there.get(k) != Some(&want) {
                    differing += 1;
                }
            }
        }
        Ok((pg.len(), differing))
    }
}
