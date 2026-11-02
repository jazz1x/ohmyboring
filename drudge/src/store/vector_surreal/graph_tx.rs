//! C-SURALL2 C0b1 bundle 2: the engine's `node` and `edge` writes go straight into Surreal on
//! `backend=surreal`. pg no longer receives them, and `mirror` / `catch_up` no longer copy these
//! two tables (the copy would delete a row pg lacks). `fill_from` still copies them: it is the
//! Z4 tool that levels pg to Surreal or the other way round before the switch-over.
//!
//! One logical write is one `BEGIN ... COMMIT` request (the Z1 shape of `claim_tx.rs`, same
//! resend cap `TX_RETRIES`, counted under `TxPlace::Doc`). A node or edge that two writers
//! upsert (a shared `topic:` node) meets as a write-write conflict and the loser's whole script
//! is sent again.
//!
//! The pg rules are kept row for row: a node upsert rewrites `label` and `outcome` but never
//! `kind`; an edge upsert raises `judge` only (`owner` > `agent:*` > `inferred` > none) and keeps
//! the `first_seen_us` of the first write.
//!
//! Between the bundles a pg transaction (claim rows, owner gap) and these Surreal writes commit
//! apart: a pg rollback after the Surreal commit leaves graph rows without their claim row.
//! Bundle 3 closes that by moving `claim` and `claim_row` too.

use std::collections::HashSet;

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::Value;

use super::claim_tx::{Attempt, TxPlace};
use super::reads::en_us_order;
use super::{SurrealClient, edge_key, first_statement, now_micros, text, texts};

/// Values per `INSIDE [...]` list: a request body over 1 MiB is refused (HTTP 413).
const BATCH: usize = 500;

/// One graph write, in the terms of the pg helpers it replaces (`upsert_node_on`,
/// `upsert_edge_on`, and the three `DELETE FROM edge` statements).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphOp {
    Node {
        id: String,
        kind: String,
        label: String,
        outcome: Option<String>,
    },
    Edge {
        src: String,
        dst: String,
        kind: String,
        judge: Option<String>,
    },
    /// `DELETE FROM edge WHERE src = $1 AND kind = ANY($2)`.
    DeleteEdgesFrom { src: String, kinds: Vec<String> },
    /// `DELETE FROM edge WHERE src = $1 OR dst = $1`.
    DeleteEdgesAt { id: String },
}

impl GraphOp {
    #[must_use]
    pub fn node(id: &str, kind: &str, label: &str, outcome: Option<&str>) -> Self {
        Self::Node {
            id: id.to_owned(),
            kind: kind.to_owned(),
            label: label.to_owned(),
            outcome: outcome.map(str::to_owned),
        }
    }

    #[must_use]
    pub fn edge(src: &str, dst: &str, kind: &str, judge: Option<&str>) -> Self {
        Self::Edge {
            src: src.to_owned(),
            dst: dst.to_owned(),
            kind: kind.to_owned(),
            judge: judge.map(str::to_owned),
        }
    }
}

/// `judge_rank_sql` of `store.rs` for a judge known here: owner 3, `agent:*` 2, inferred 1.
fn judge_rank(judge: Option<&str>) -> u8 {
    match judge {
        Some("owner") => 3,
        Some(j) if j.starts_with("agent:") => 2,
        Some("inferred") => 1,
        _ => 0,
    }
}

/// The same rank over the stored `judge` field of the row being upserted.
const STORED_JUDGE_RANK: &str = "(IF judge = 'owner' THEN 3 ELSE \
     (IF string::starts_with(judge ?? '', 'agent:') THEN 2 ELSE \
     (IF judge = 'inferred' THEN 1 ELSE 0 END) END) END)";

fn optional(value: Option<&str>) -> String {
    value.map_or_else(|| "NONE".to_owned(), text)
}

fn op_sql(op: &GraphOp, now_us: i64) -> String {
    match op {
        GraphOp::Node {
            id,
            kind,
            label,
            outcome,
        } => format!(
            "UPSERT type::record('node', {i}) SET cid = {i}, kind = kind ?? {k}, label = {l}, \
             outcome = {o};",
            i = text(id),
            k = text(kind),
            l = text(label),
            o = optional(outcome.as_deref())
        ),
        GraphOp::Edge {
            src,
            dst,
            kind,
            judge,
        } => {
            let key = edge_key(src, dst, kind);
            let j = optional(judge.as_deref());
            // `judge` is assigned before `first_seen_us`: the row is new while it is still NONE.
            format!(
                "UPSERT type::record('edge', {key}) SET cid = {key}, src = {s}, dst = {d}, \
                 kind = {k}, \
                 judge = (IF first_seen_us = NONE THEN {j} ELSE \
                 (IF {new_rank} > {STORED_JUDGE_RANK} THEN {j} ELSE judge END) END), \
                 first_seen_us = first_seen_us ?? {now_us};",
                key = text(&key),
                s = text(src),
                d = text(dst),
                k = text(kind),
                new_rank = judge_rank(judge.as_deref()),
            )
        }
        GraphOp::DeleteEdgesFrom { src, kinds } => format!(
            "DELETE edge WHERE src = {} AND kind INSIDE {};",
            text(src),
            texts(kinds)
        ),
        GraphOp::DeleteEdgesAt { id } => {
            format!("DELETE edge WHERE src = {i} OR dst = {i};", i = text(id))
        }
    }
}

/// The edge rows `seal_claims_under_supersedes_edges` lists: `(src, dst)` of every `supersedes`.
#[derive(Deserialize)]
struct EdgeEnds {
    src: String,
    dst: String,
}

#[derive(Deserialize)]
struct NodeKind {
    cid: String,
    kind: String,
}

#[derive(Deserialize)]
struct Orphans {
    tool: usize,
    concept: usize,
}

impl SurrealClient {
    /// Apply graph writes in one Surreal transaction, in order.
    ///
    /// # Errors
    /// A statement failed, or the rows stayed in conflict for more than `tx_retries` resends.
    pub async fn graph_write(&self, ops: &[GraphOp]) -> Result<()> {
        if ops.is_empty() {
            return Ok(());
        }
        let now_us = now_micros()?;
        let sql = ops
            .iter()
            .map(|op| op_sql(op, now_us))
            .collect::<Vec<_>>()
            .join("\n");
        self.retrying(TxPlace::Doc, || async {
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// `Store::handed_paths`: the notes a session's `handed` edges reach, distinct, in path order.
    ///
    /// # Errors
    /// The read failed.
    pub async fn handed_paths(&self, session_id: &str) -> Result<Vec<String>> {
        let sql = format!(
            "SELECT VALUE dst FROM edge WHERE src = {} AND kind = 'handed';",
            text(&format!("session:{session_id}"))
        );
        let dsts: Vec<String> = first_statement(&sql, self.rows::<String>(&sql).await?)?;
        let mut paths: Vec<String> = dsts
            .into_iter()
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        paths.sort_by(|a, b| en_us_order(a, b));
        Ok(paths
            .into_iter()
            .map(|dst| dst.strip_prefix("doc:").unwrap_or(&dst).to_owned())
            .collect())
    }

    /// `(src, dst)` of every `supersedes` edge — the sweep's list.
    ///
    /// # Errors
    /// The read failed.
    pub async fn supersedes_edges(&self) -> Result<Vec<(String, String)>> {
        let sql = "SELECT src, dst FROM edge WHERE kind = 'supersedes';";
        let rows: Vec<EdgeEnds> = first_statement(sql, self.rows::<EdgeEnds>(sql).await?)?;
        Ok(rows.into_iter().map(|e| (e.src, e.dst)).collect())
    }

    /// `gc_orphans`' first half: delete the `tool` and `concept` nodes no edge reaches, in one
    /// transaction. Returns `(tools, concepts)` removed.
    ///
    /// # Errors
    /// A statement failed, or the rows stayed in conflict for more than `tx_retries` resends.
    pub async fn gc_orphan_entities(&self) -> Result<(usize, usize)> {
        let sql =
            "LET $used = array::union((SELECT VALUE src FROM edge), (SELECT VALUE dst FROM edge));
             LET $t = (SELECT VALUE cid FROM node WHERE kind = 'tool' AND !(cid INSIDE $used));
             LET $c = (SELECT VALUE cid FROM node WHERE kind = 'concept' AND !(cid INSIDE $used));
             DELETE node WHERE cid INSIDE $t;
             DELETE node WHERE cid INSIDE $c;
             RETURN {tool: array::len($t), concept: array::len($c)};";
        self.retrying(TxPlace::Doc, || async {
            Ok(match self.tx_once(sql).await? {
                Attempt::Done(answers) => {
                    let answer: Orphans = serde_json::from_value(
                        answers
                            .into_iter()
                            .rev()
                            .nth(1)
                            .context("orphan gc answered nothing")?,
                    )
                    .context("orphan gc answer")?;
                    Attempt::Done((answer.tool, answer.concept))
                }
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// The ids and kinds of every node a claim row could own: kind `claim` and the typed kinds.
    ///
    /// # Errors
    /// The read failed.
    pub async fn claim_node_candidates(
        &self,
        typed_kinds: &[String],
    ) -> Result<Vec<(String, String)>> {
        let mut kinds = typed_kinds.to_vec();
        kinds.push("claim".to_owned());
        let sql = format!(
            "SELECT cid, kind FROM node WHERE kind INSIDE {};",
            texts(&kinds)
        );
        let rows: Vec<NodeKind> = first_statement(&sql, self.rows::<NodeKind>(&sql).await?)?;
        Ok(rows.into_iter().map(|n| (n.cid, n.kind)).collect())
    }

    /// `gc_orphans`' second half: delete these nodes and every edge with one of them at an end.
    /// Returns the number of edges deleted.
    ///
    /// # Errors
    /// A statement failed, or the rows stayed in conflict for more than `tx_retries` resends.
    pub async fn delete_nodes_with_edges(&self, ids: &[String]) -> Result<usize> {
        let mut edges = 0;
        for batch in ids.chunks(BATCH) {
            let list = texts(batch);
            let sql = format!(
                "LET $e = (SELECT VALUE cid FROM edge WHERE src INSIDE {list} OR dst INSIDE {list});
                 DELETE edge WHERE src INSIDE {list} OR dst INSIDE {list};
                 DELETE node WHERE cid INSIDE {list};
                 RETURN array::len($e);"
            );
            edges += self
                .retrying(TxPlace::Doc, || async {
                    Ok(match self.tx_once(&sql).await? {
                        Attempt::Done(answers) => {
                            let n = answers
                                .iter()
                                .rev()
                                .nth(1)
                                .and_then(Value::as_u64)
                                .context("node delete answered no edge count")?;
                            Attempt::Done(usize::try_from(n)?)
                        }
                        Attempt::Conflict(m) => Attempt::Conflict(m),
                    })
                })
                .await?;
        }
        Ok(edges)
    }
}
