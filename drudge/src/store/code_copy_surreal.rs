//! The Surreal side of the four `code_index` tables (`repository`, `file`, `symbol`, `relation`):
//! table definitions, the `code_file` read the sync uses, and the one-request transaction
//! `drudge code-sync` writes through (`replace_repository_surreal`, one transaction per file, or
//! several when a file does not fit one request, the repository row last).
//!
//! The pg→Surreal copy pass that used to live here (`copy_code_repo`, `check_code`) is gone: on
//! `backend=surreal` the writer is direct, and `code_index::code_check` compares disk against
//! Surreal's sha. Same move as `1f00d7c` for the other tables.

use anyhow::Result;
use serde::Deserialize;

use super::log_copy_surreal::json_batch_stmt;
use super::vector_surreal::{SurrealClient, first_statement};

fn literal(s: &str) -> Result<String> {
    Ok(serde_json::to_string(s)?)
}

/// A `code_file` row as the engine's own sync reads it back.
#[derive(Debug, Deserialize)]
pub struct CodeFileRow {
    pub cid: String,
    pub relative_path: String,
    pub sha256: String,
    pub error_count: i64,
}

/// Largest `BEGIN ... COMMIT` body `code_tx` sends: the server refuses a body over 1 MiB (HTTP
/// 413, see `log_copy_surreal`), and HTTP has no open transaction to spread one over.
pub const CODE_TX_BYTES: usize = 900 * 1024;

/// A `FOR ... UPSERT` statement of json rows (each with its key as `cid`) for `code_tx`.
#[must_use]
pub fn code_upsert_stmt(table: &str, rows: &[String]) -> String {
    json_batch_stmt(table, rows)
}

impl SurrealClient {
    /// Every `code_file` row of one repository.
    ///
    /// # Errors
    /// The request failed.
    pub async fn code_file_rows(&self, repository: &str) -> Result<Vec<CodeFileRow>> {
        self.ensure_code_tables().await?;
        let query = format!(
            "SELECT cid, relative_path, sha256, error_count FROM code_file WHERE repository_id = {};",
            literal(repository)?
        );
        first_statement(&query, self.rows::<CodeFileRow>(&query).await?)
    }

    /// `body` as one `BEGIN; ... COMMIT;` request: all of it lands or none of it.
    ///
    /// # Errors
    /// The body is over `CODE_TX_BYTES`, or a statement failed (the server then cancels the
    /// transaction and nothing of `body` is written).
    pub async fn code_tx(&self, body: &str) -> Result<()> {
        anyhow::ensure!(
            body.len() <= CODE_TX_BYTES,
            "code_index transaction is {} bytes, over the {CODE_TX_BYTES} one request can carry",
            body.len()
        );
        self.run(&format!("BEGIN; {body} COMMIT;")).await?;
        Ok(())
    }

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
}
