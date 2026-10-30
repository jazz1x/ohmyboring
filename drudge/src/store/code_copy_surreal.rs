//! C-SURALL2 A5: the four `code_index` tables (`repository`, `file`, `symbol`, `relation`) copied
//! from pg into Surreal, one repository at a time, by a pass that reads pg after the sync commit.
//!
//! The only writer is `replace_repository` (one pg transaction per repository: upsert of the
//! repository row, `DELETE`+`INSERT` per changed file, `DELETE` of vanished files), run by the host
//! binary `drudge code-sync`. Row ids are content hashes and a row's values change under the
//! same id, so the A4 id watermark does not fit; the scope that does fit is the repository, the
//! same shape as `SurrealClient::mirror` (pg's current state of a scope, re-read, then reconciled).
//!
//! A pass fingerprints every row of the repository on both sides (`cid`, `h` = md5 of the row's
//! json) and sends only the rows whose fingerprint differs, so a repository that did not change
//! costs two id+hash reads and no write; keys Surreal holds and pg lacks go. Upsert first, delete
//! second, no transaction: a pass that fails half way leaves extra rows, never fewer, and the next
//! pass (the next `code-sync`, which mirrors every selected repository whether or not it changed)
//! converges.
//!
//! No cross-repository case: `relation` has the composite FK `(repository_id, target_symbol_id)`
//! to `symbol(repository_id, id)` besides the single-column `SET NULL` one (baseline.sql), so a
//! relation never targets another repository's symbol and every row a symbol delete touches is
//! in the repository being copied.

use std::collections::HashMap;

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::Value;

use super::vector_surreal::{SurrealClient, first_statement};
use crate::code_index::CodeIndexStore;

const PG_ROWS_PER_READ: usize = 200;

/// One table: `row` is a `jsonb_build_object(..)` over the pg columns with `cid` (the Surreal
/// record key) first; timestamps as epoch microseconds, the convention of the other copies.
struct Spec {
    /// The pg table in schema `code_index`.
    table: &'static str,
    /// Its Surreal table: prefixed, since `file` and `relation` are SurrealQL words.
    surreal: &'static str,
    /// Where the repository's rows are in pg (`$1` = repository id).
    pg_scope: &'static str,
    /// The same, in Surreal (`{}` = a string literal of the repository id).
    surreal_scope: &'static str,
    row: &'static str,
}

impl Spec {
    /// `(cid, h)` of the repository's rows in pg.
    fn fingerprints_sql(&self) -> String {
        format!(
            "SELECT j->>'cid', j->>'h' FROM {} s",
            self.hashed(&format!("WHERE {}", self.pg_scope))
        )
    }

    /// `(cid, json)` of the rows whose cid is in `$1`.
    fn rows_by_cid_sql(&self) -> String {
        format!(
            "SELECT j->>'cid', j::text FROM {} s ORDER BY 1",
            self.hashed("WHERE id = ANY($1::text[])")
        )
    }

    /// `(cid, json)` of every row of every repository, for the full compare.
    fn all_rows_sql(&self) -> String {
        format!(
            "SELECT j->>'cid', j::text FROM {} s ORDER BY 1",
            self.hashed("")
        )
    }

    /// The row with its fingerprint `h` (md5 of the row without it) added.
    fn hashed(&self, filter: &str) -> String {
        format!(
            "(SELECT b || jsonb_build_object('h', md5(b::text)) AS j \
             FROM (SELECT {row} AS b FROM code_index.{table} {filter}) t)",
            row = self.row,
            table = self.table
        )
    }
}

const SPECS: [Spec; 4] = [
    Spec {
        table: "repository",
        surreal: "code_repository",
        pg_scope: "id = $1",
        surreal_scope: "cid = {}",
        row: "jsonb_build_object('cid', id, 'name', name, 'root_path', root_path,
    'language', language,
    'last_synced_us', (extract(epoch from last_synced_at) * 1000000)::bigint)",
    },
    Spec {
        table: "file",
        surreal: "code_file",
        pg_scope: "repository_id = $1",
        surreal_scope: "repository_id = {}",
        row: "jsonb_build_object('cid', id, 'repository_id', repository_id,
    'relative_path', relative_path, 'sha256', sha256, 'parse_status', parse_status,
    'error_count', error_count,
    'updated_us', (extract(epoch from updated_at) * 1000000)::bigint)",
    },
    Spec {
        table: "symbol",
        surreal: "code_symbol",
        pg_scope: "repository_id = $1",
        surreal_scope: "repository_id = {}",
        row: "jsonb_build_object('cid', id, 'repository_id', repository_id, 'file_id', file_id,
    'kind', kind, 'name', name, 'qualified_name', qualified_name, 'start_byte', start_byte,
    'end_byte', end_byte, 'start_line', start_line, 'end_line', end_line)",
    },
    Spec {
        table: "relation",
        surreal: "code_relation",
        pg_scope: "repository_id = $1",
        surreal_scope: "repository_id = {}",
        row: "jsonb_build_object('cid', id, 'repository_id', repository_id, 'file_id', file_id,
    'source_symbol_id', source_symbol_id, 'kind', kind, 'target_symbol_id', target_symbol_id,
    'target_name', target_name, 'start_byte', start_byte, 'end_byte', end_byte)",
    },
];

/// What one pass did to one table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodeCopy {
    pub table: &'static str,
    pub upserted: usize,
    pub deleted: usize,
}

impl CodeCopy {
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
pub struct CodeCheck {
    pub table: &'static str,
    pub pg_rows: usize,
    pub surreal_rows: usize,
    /// pg rows Surreal lacks or holds with another value.
    pub differing: usize,
}

impl CodeCheck {
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
struct Fingerprint {
    cid: String,
    h: String,
}

#[derive(Deserialize)]
struct Count {
    count: usize,
}

fn literal(s: &str) -> Result<String> {
    Ok(serde_json::to_string(s)?)
}

impl SurrealClient {
    async fn ensure_code_tables(&self) -> Result<()> {
        self.run(&format!(
            "DEFINE NAMESPACE IF NOT EXISTS {ns}; DEFINE DATABASE IF NOT EXISTS {db};
             DEFINE TABLE IF NOT EXISTS code_repository SCHEMALESS;
             DEFINE TABLE IF NOT EXISTS code_file SCHEMALESS;
             DEFINE INDEX IF NOT EXISTS code_file_repo ON code_file FIELDS repository_id;
             DEFINE TABLE IF NOT EXISTS code_symbol SCHEMALESS;
             DEFINE INDEX IF NOT EXISTS code_symbol_repo ON code_symbol FIELDS repository_id;
             DEFINE TABLE IF NOT EXISTS code_relation SCHEMALESS;
             DEFINE INDEX IF NOT EXISTS code_relation_repo ON code_relation FIELDS repository_id;
             DEFINE INDEX IF NOT EXISTS code_relation_target
                 ON code_relation FIELDS target_symbol_id;",
            ns = self.namespace(),
            db = self.database()
        ))
        .await?;
        Ok(())
    }

    async fn surreal_fingerprints(&self, query: &str) -> Result<HashMap<String, String>> {
        let rows: Vec<Fingerprint> =
            first_statement(query, self.rows::<Fingerprint>(query).await?)?;
        Ok(rows.into_iter().map(|f| (f.cid, f.h)).collect())
    }

    async fn pg_fingerprints(
        pg: &CodeIndexStore,
        sql: &str,
        arg: &(dyn tokio_postgres::types::ToSql + Sync),
    ) -> Result<HashMap<String, String>> {
        Ok(pg
            .db()
            .await?
            .query(sql, &[arg])
            .await
            .context("read code_index fingerprints from pg")?
            .iter()
            .map(|r| (r.get(0), r.get(1)))
            .collect())
    }

    /// Send the pg rows Surreal lacks or holds with another fingerprint, then delete the Surreal
    /// keys pg lacks.
    async fn apply_diff(
        &self,
        pg: &CodeIndexStore,
        spec: &Spec,
        pg_fp: &HashMap<String, String>,
        surreal_fp: &HashMap<String, String>,
    ) -> Result<CodeCopy> {
        let mut to_write: Vec<String> = pg_fp
            .iter()
            .filter(|(cid, h)| surreal_fp.get(*cid) != Some(*h))
            .map(|(cid, _)| cid.clone())
            .collect();
        to_write.sort();
        let sql = spec.rows_by_cid_sql();
        for page in to_write.chunks(PG_ROWS_PER_READ) {
            let rows: Vec<String> = pg
                .db()
                .await?
                .query(&sql, &[&page.to_vec()])
                .await
                .with_context(|| format!("read code_index.{} rows from pg", spec.table))?
                .iter()
                .map(|r| r.get::<_, String>(1))
                .collect();
            self.upsert_json(spec.surreal, &rows).await?;
        }
        let mut stale: Vec<String> = surreal_fp
            .keys()
            .filter(|cid| !pg_fp.contains_key(*cid))
            .cloned()
            .collect();
        stale.sort();
        self.delete_rows(spec.surreal, &stale).await?;
        Ok(CodeCopy {
            table: spec.table,
            upserted: to_write.len(),
            deleted: stale.len(),
        })
    }

    /// One repository: make Surreal's four tables equal pg's rows of `repository`. Safe to repeat.
    ///
    /// # Errors
    /// A pg read or a Surreal request failed; pg is untouched and the next pass converges.
    pub async fn copy_code_repo(
        &self,
        pg: &CodeIndexStore,
        repository: &str,
    ) -> Result<Vec<CodeCopy>> {
        self.ensure_code_tables().await?;
        let repo = literal(repository)?;
        let repo_arg = repository.to_owned();
        let mut done = Vec::with_capacity(SPECS.len());
        for spec in &SPECS {
            let pg_fp = Self::pg_fingerprints(pg, &spec.fingerprints_sql(), &repo_arg).await?;
            let query = format!(
                "SELECT cid, h FROM {} WHERE {};",
                spec.surreal,
                spec.surreal_scope.replace("{}", &repo)
            );
            let surreal_fp = self.surreal_fingerprints(&query).await?;
            done.push(self.apply_diff(pg, spec, &pg_fp, &surreal_fp).await?);
        }
        Ok(done)
    }

    /// Read-only: every pg row of the four tables against Surreal's row of the same key, value
    /// by value, and the row counts.
    ///
    /// # Errors
    /// A read failed.
    pub async fn check_code(&self, pg: &CodeIndexStore) -> Result<Vec<CodeCheck>> {
        let mut checks = Vec::with_capacity(SPECS.len());
        for spec in &SPECS {
            let rows: Vec<(String, String)> = pg
                .db()
                .await?
                .query(&spec.all_rows_sql(), &[])
                .await
                .context("read code_index rows from pg")?
                .iter()
                .map(|r| (r.get(0), r.get(1)))
                .collect();
            let mut differing = 0;
            for page in rows.chunks(PG_ROWS_PER_READ) {
                let wanted: Vec<String> = page
                    .iter()
                    .map(|(k, _)| literal(k))
                    .collect::<Result<_>>()?;
                let there = self.surreal_rows_by(spec.surreal, "cid", &wanted).await?;
                for (k, json) in page {
                    let want: Value = serde_json::from_str(json)?;
                    if there.get(k) != Some(&want) {
                        differing += 1;
                    }
                }
            }
            let query = format!("SELECT count() FROM {} GROUP ALL;", spec.surreal);
            let counted: Vec<Count> = first_statement(&query, self.rows::<Count>(&query).await?)?;
            checks.push(CodeCheck {
                table: spec.table,
                pg_rows: rows.len(),
                surreal_rows: counted.first().map_or(0, |c| c.count),
                differing,
            });
        }
        Ok(checks)
    }
}
