//! C-SURALL2 C0b1 bundle 1: a note's `document` and `chunk` rows are written by the engine straight
//! into Surreal on `backend=surreal`. pg no longer receives them, and `mirror` / `catch_up` no
//! longer copy these two tables (they would delete a row pg lacks).
//!
//! One note is one `BEGIN ... COMMIT` request, the Z1 shape (`claim_tx.rs`): a guard row per note
//! is bumped first, so two writers of one note meet in a write-write conflict and the loser's
//! whole script is sent again (`TX_RETRIES`, same cap as the claim writes). The reads that decide
//! inside the script (`$old` extracted_sha, the chunk ids that are no longer wanted, the dirty
//! count) are `LET`s over Surreal's own rows, so they see a value changed only in Surreal.
//!
//! Chunks of the note go to the global `chunk` table and to the table of the note's project
//! (`chunk_p_<slug>`); every other project table loses this note's chunks (the note changed
//! project). The stale tail of a re-ingested note is deleted by cid, not by a `chunk_idx`: the
//! Surreal chunk row has no such column. `vector_meta:<table>.dirty` is bumped inside the same
//! transaction, so the HNSW swap sees what the write changed.
//!
//! Z4: after B, pg does not receive Surreal writes. The Z4 rehearsal "all-table diff 0/N" is
//! measured after `fill_from` aligns pg and Surreal right before the switch-over; no
//! `backend=surreal` live, Z4 rehearsal or live-copy measurement until every table bundle is
//! closed. Bundle 3 closed them: a note's ingest is Surreal commits only (document+chunk here, the
//! graph in `graph_tx.rs`, each claim in `claim_tx.rs`), and pg's request transaction holds no row.

use std::time::SystemTime;

use anyhow::Result;

use super::claim_tx::{Attempt, TxPlace, delete_claims_sql};
use super::{
    ProjectTable, SurrealClient, chunk_ddl, first_statement, floats, micros_to_time, quote_ident,
    text, texts,
};

/// One chunk of a note, in the terms `Store::upsert_chunk_on` takes.
#[derive(Debug, Clone, Copy)]
pub struct NoteChunk<'a> {
    pub id: &'a str,
    pub content: &'a str,
    pub embedding: &'a [f32],
}

/// A note's document row and the whole set of its chunks.
#[derive(Debug, Clone, Copy)]
pub struct NoteWrite<'a> {
    pub path: &'a str,
    pub origin: &'a str,
    pub project: &'a str,
    pub kind: &'a str,
    pub title: Option<&'a str>,
    pub tags: &'a [String],
    pub sha: &'a str,
    pub author: &'a str,
    pub updated_us: i64,
    pub chunks: &'a [NoteChunk<'a>],
    pub dim: usize,
}

fn chunk_row(w: &NoteWrite<'_>, c: &NoteChunk<'_>) -> String {
    format!(
        "{{cid: {}, source_path: {}, project: {}, doc_updated_us: {}, content: {}, origin: {}, \
         embedding: {}}}",
        text(c.id),
        text(w.path),
        text(w.project),
        w.updated_us,
        text(c.content),
        text(w.origin),
        floats(c.embedding)
    )
}

/// The dirty bump of one table: its stale rows (`$gone<i>`) plus the rows written now.
fn dirty_bump(table: &str, i: usize, written: usize) -> String {
    format!(
        "UPSERT type::record('vector_meta', {t}) SET dirty = (dirty ?? 0) + {written} + array::len($gone{i});",
        t = text(table)
    )
}

/// The statements that bring one chunk table level with the note: keep `$keep`, delete the rest
/// of this note's rows, bump the dirty count.
fn table_sql(table: &str, i: usize, path: &str, written: usize) -> String {
    let q = quote_ident(table);
    format!(
        "LET $gone{i} = (SELECT VALUE cid FROM {q} WHERE source_path = {path} AND !(cid INSIDE $keep));
         DELETE {q} WHERE source_path = {path} AND !(cid INSIDE $keep);
         {bump}",
        bump = dirty_bump(table, i, written)
    )
}

fn note_sql(w: &NoteWrite<'_>, own: Option<&ProjectTable>, tables: &[String]) -> String {
    let p = text(w.path);
    let keep: Vec<String> = w.chunks.iter().map(|c| c.id.to_owned()).collect();
    let rows: Vec<String> = w.chunks.iter().map(|c| chunk_row(w, c)).collect();
    let written = rows.len();
    let title = w
        .title
        .map_or_else(String::new, |t| format!("title: {},", text(t)));
    let own_upsert = own.map_or_else(String::new, |t| {
        format!(
            "UPSERT type::record({}, $r.cid) CONTENT $r;",
            text(t.name())
        )
    });
    let mut sql = format!(
        "UPSERT type::record('doc_guard', {p}) SET n = (n ?? 0) + 1;
         LET $keep = {keep};
         LET $old = (SELECT VALUE extracted_sha FROM document WHERE source_path = {p})[0];
         UPSERT type::record('document', {p}) CONTENT {{cid: {p}, source_path: {p}, origin: {o}, \
         project: {pr}, kind: {k}, {title} tags: {tags}, sha: {sha}, extracted_sha: ($old ?? ''), \
         updated_us: {us}, author: {au}, via: 'surreal'}};
         FOR $r IN [{rows}] {{ UPSERT type::record('chunk', $r.cid) CONTENT $r; {own_upsert} }};
         {chunk_level}",
        keep = texts(&keep),
        o = text(w.origin),
        pr = text(w.project),
        k = text(w.kind),
        tags = texts(w.tags),
        sha = text(w.sha),
        us = w.updated_us,
        au = text(w.author),
        rows = rows.join(","),
        chunk_level = table_sql("chunk", 0, &p, written),
    );
    for (i, table) in tables.iter().enumerate() {
        sql.push('\n');
        if own.is_some_and(|t| t.name() == table) {
            sql.push_str(&table_sql(table, i + 1, &p, written));
        } else {
            let q = quote_ident(table);
            sql = format!(
                "{sql}LET $gone{n} = (SELECT VALUE cid FROM {q} WHERE source_path = {p});
                 DELETE {q} WHERE source_path = {p};
                 {bump}",
                n = i + 1,
                bump = dirty_bump(table, i + 1, 0)
            );
        }
    }
    sql
}

fn delete_sql(path: &str, tables: &[String]) -> String {
    let p = text(path);
    let mut sql = format!(
        "UPSERT type::record('doc_guard', {p}) SET n = (n ?? 0) + 1;
         DELETE type::record('document', {p});
         LET $gone0 = (SELECT VALUE cid FROM chunk WHERE source_path = {p});
         DELETE chunk WHERE source_path = {p};
         {bump}",
        bump = dirty_bump("chunk", 0, 0)
    );
    for (i, table) in tables.iter().enumerate() {
        let q = quote_ident(table);
        let n = i + 1;
        sql = format!(
            "{sql}\nLET $gone{n} = (SELECT VALUE cid FROM {q} WHERE source_path = {p});
             DELETE {q} WHERE source_path = {p};
             {bump}",
            bump = dirty_bump(table, n, 0)
        );
    }
    format!("{sql}\n{}", delete_claims_sql(path))
}

impl SurrealClient {
    /// The project's table when its slug can name one. A slug that cannot (empty, a character
    /// outside `[a-z0-9_-]`) keeps the note's chunks in the global table only, and says so.
    fn own_project_table(project: &str) -> Option<ProjectTable> {
        match ProjectTable::parse(project) {
            Ok(table) => Some(table),
            Err(e) => {
                eprintln!("[vector] surreal: {e} — chunk 는 global 테이블에만 씀");
                None
            }
        }
    }

    /// A project table that does not exist yet is made from the chunks Surreal already holds for
    /// that project (the global table is the source), then written like any other.
    async fn create_project_table_from_chunks(
        &self,
        table: &ProjectTable,
        dim: usize,
    ) -> Result<()> {
        self.run(&chunk_ddl(table.name(), dim)).await?;
        self.run(&format!(
            "FOR $r IN (SELECT cid, source_path, project, doc_updated_us, content, origin, embedding \
             FROM chunk WHERE project = {slug}) {{ UPSERT type::record({name}, $r.cid) CONTENT $r; }};",
            slug = text(table.slug()),
            name = text(table.name())
        ))
        .await
        .map(drop)
    }

    /// Write a note's `document` row and its whole chunk set in one Surreal transaction.
    ///
    /// # Errors
    /// A statement failed, or the note stayed in conflict for more than `tx_retries` resends.
    pub async fn write_note(&self, w: &NoteWrite<'_>) -> Result<()> {
        self.ensure_via_field().await?;
        let own = Self::own_project_table(w.project);
        let mut tables = self.project_tables().await?;
        if let Some(t) = &own
            && !tables.iter().any(|n| n == t.name())
        {
            self.create_project_table_from_chunks(t, w.dim).await?;
            tables.push(t.name().to_owned());
        }
        let sql = note_sql(w, own.as_ref(), &tables);
        self.retrying(TxPlace::Doc, || async {
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// Remove a note's `document` row, every chunk of it and every claim row it owns, sealed
    /// history included, in one transaction (`Store::delete_document`'s document, chunk and claim
    /// half; its edges go in `graph_tx.rs`).
    ///
    /// # Errors
    /// A statement failed, or the note stayed in conflict for more than `tx_retries` resends.
    pub async fn delete_note(&self, path: &str) -> Result<()> {
        let tables = self.project_tables().await?;
        let sql = delete_sql(path, &tables);
        self.retrying(TxPlace::Doc, || async {
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// Move only the stored change-detection sha of an existing note (`Store::set_doc_sha`).
    ///
    /// # Errors
    /// A statement failed, or the note stayed in conflict for more than `tx_retries` resends.
    pub async fn set_doc_sha(&self, path: &str, sha: &str) -> Result<()> {
        self.ensure_via_field().await?;
        let p = text(path);
        let sql = format!(
            "UPSERT type::record('doc_guard', {p}) SET n = (n ?? 0) + 1;
             UPDATE document SET sha = {sha}, via = 'surreal' WHERE source_path = {p};",
            sha = text(sha)
        );
        self.retrying(TxPlace::Doc, || async {
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// `Store::doc_updated_at`: the note's mtime as Surreal holds it, `now()` when it has no row
    /// (the same graceful answer pg gives).
    ///
    /// # Errors
    /// The read failed, or the stored time is before 1970.
    pub async fn doc_updated_at(&self, path: &str) -> Result<SystemTime> {
        let sql = format!(
            "SELECT VALUE updated_us FROM document WHERE source_path = {};",
            text(path)
        );
        let found = first_statement(&sql, self.rows::<i64>(&sql).await?)?;
        match found.first() {
            Some(us) => micros_to_time(*us, "document.updated_us"),
            None => Ok(SystemTime::now()),
        }
    }
}
