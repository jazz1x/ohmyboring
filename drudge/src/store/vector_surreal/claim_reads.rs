//! C-SURALL2 Z2③b: the claim reads that still asked pg — the stalled lists, the north star, the
//! per-note declared claims, the `/claim-source*` and `/claim-priors` doors, the owner-said counts,
//! the audit's era and stale counts and the anchor check's work list — answered from the
//! `claim_row` copy (and `edge` / `document` where pg joined them) on `backend=surreal`. pg is not
//! asked; a Surreal failure is an error, never a pg fallback.
//!
//! Each reader is the SurrealQL of one pg query in `store.rs`, condition for condition. Where pg
//! picks "the first row per group" or orders by the column collation, the pick is made here in
//! Rust over a fully ordered fetch, so ties come out one way. Rows tied on every sort key have no
//! defined order on either side.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::time::SystemTime;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::Deserialize;

use super::reads::en_us_order;
use super::{
    Count, INFORMATIVE_VALUE_CHARS, RegisterAsk, SurrealClient, TAUTOLOGICAL_PREDICATES,
    first_statement, micros_to_time, now_micros, register_where, text, texts,
};
use crate::claim_priors::{ClaimPrior, prior_of};
use crate::claim_source::{ClaimRow, ListRow};
use crate::frontmatter::Claim;
use crate::rules::RuleRow;
use crate::split_subjects::{SAMPLE_LIMIT, SAMPLE_VALUE_CHARS, Sample};
use crate::store::{
    AnchorCheckRow, AnchorCheckWatermark, ClaimEraCounts, RegisterRow, RegisterRows,
    STALE_HORIZON_DAYS,
};

const DAY_US: i64 = 86_400_000_000;

/// One `claim_row` of a register-shaped read: `register_select`'s columns plus the note it came from.
#[derive(Deserialize)]
struct NoteClaim {
    subject: String,
    predicate: String,
    value: String,
    kind: String,
    confidence: String,
    valid_from_us: i64,
    project: String,
    source_path: String,
}

impl NoteClaim {
    fn into_row(self) -> Result<RegisterRow> {
        Ok(RegisterRow::new(
            self.subject,
            self.predicate,
            self.value,
            self.kind,
            self.confidence,
            micros_to_time(self.valid_from_us, "claim_row valid_from_us")?,
            self.project,
        ))
    }
}

#[derive(Deserialize)]
struct SourceRow {
    subject: String,
    value: String,
    source_path: String,
    valid_from_us: i64,
}

#[derive(Deserialize)]
struct Author {
    source_path: String,
    author: String,
}

#[derive(Deserialize)]
struct PriorRow {
    subject: String,
    predicate: String,
    value: String,
    source_path: String,
    valid_from_us: i64,
    superseded_us: i64,
}

#[derive(Deserialize)]
struct RuleClaim {
    subject: String,
    predicate: String,
    value: String,
    source_path: String,
}

#[derive(Deserialize)]
struct SubjectPath {
    subject: String,
    source_path: String,
}

#[derive(Deserialize)]
struct PredicateValue {
    predicate: String,
    value: String,
}

#[derive(Deserialize)]
struct OwnerCount {
    source_path: String,
    n: i64,
}

#[derive(Deserialize)]
struct EdgeEnd {
    src: String,
    dst: String,
}

#[derive(Deserialize)]
struct AnchorRow {
    subject: String,
    predicate: String,
    valid_from_us: i64,
    anchor: Option<String>,
    anchor_hash: String,
    anchor_symbol: Option<String>,
}

fn utc(us: i64, what: &str) -> Result<DateTime<Utc>> {
    Ok(DateTime::<Utc>::from(micros_to_time(us, what)?))
}

/// pg's `ORDER BY subject, predicate` under the column collation.
fn slot_order(a: (&str, &str), b: (&str, &str)) -> Ordering {
    en_us_order(a.0, b.0).then_with(|| en_us_order(a.1, b.1))
}

/// The `tier` of `declared_claims`: the kinds that solve something first.
fn tier(kind: &str) -> u8 {
    match kind {
        "decision" => 0,
        "next" | "blocked" => 1,
        "risk" => 2,
        _ => 3,
    }
}

impl SurrealClient {
    /// `Store::north_star_claims`: the newest current `decision` per `goal` / `check` of the
    /// project's north-star subject.
    pub async fn north_star_claims(
        &self,
        subject: &str,
        predicates: &[String],
        exclude_origins: &[String],
    ) -> Result<Vec<Claim>> {
        let ask = RegisterAsk {
            k: 0,
            project: None,
            kinds: None,
            exclude_origins,
        };
        let sql = format!(
            "SELECT subject, predicate, value, kind, confidence, valid_from_us, project ?? '' AS project, source_path
               FROM claim_row
              WHERE {} AND kind = 'decision' AND subject = {} AND predicate IN {}
              ORDER BY predicate ASC, valid_from_us DESC;",
            register_where(&ask),
            text(subject),
            texts(predicates),
        );
        let rows: Vec<NoteClaim> = first_statement(&sql, self.rows::<NoteClaim>(&sql).await?)?;
        let mut seen = HashSet::new();
        Ok(rows
            .into_iter()
            .filter(|r| seen.insert(r.predicate.clone()))
            .map(|r| Claim {
                subject: r.subject,
                predicate: r.predicate,
                value: r.value,
                kind: r.kind,
                confidence: r.confidence,
                said_by: None,
            })
            .collect())
    }

    /// `Store::stalled_register_rows` (and, row for row, `stalled_claims`): current claims whose
    /// `valid_from` is older than `older_than_days` and not older than `STALE_HORIZON_DAYS`, the
    /// oldest claim of each note, oldest first, cut at `ask.k` with the full count stated.
    pub async fn stalled_register_rows(
        &self,
        ask: &RegisterAsk<'_>,
        older_than_days: i64,
    ) -> Result<RegisterRows> {
        let now = now_micros()?;
        let sql = format!(
            "SELECT subject, predicate, value, kind, confidence, valid_from_us, project ?? '' AS project, source_path
               FROM claim_row
              WHERE {} AND valid_from_us < {} AND valid_from_us >= {}
              ORDER BY valid_from_us ASC, source_path ASC, subject ASC, predicate ASC;",
            register_where(ask),
            now - older_than_days.saturating_mul(DAY_US),
            now - STALE_HORIZON_DAYS * DAY_US,
        );
        let window: Vec<NoteClaim> = first_statement(&sql, self.rows::<NoteClaim>(&sql).await?)?;
        let mut notes = HashSet::new();
        let oldest: Vec<NoteClaim> = window
            .into_iter()
            .filter(|r| notes.insert(r.source_path.clone()))
            .collect();
        let total_matching = i64::try_from(oldest.len()).context("stalled count")?;
        let rows = oldest
            .into_iter()
            .take(usize::try_from(ask.k.max(0)).unwrap_or(0))
            .map(NoteClaim::into_row)
            .collect::<Result<Vec<_>>>()?;
        Ok(RegisterRows {
            rows,
            total_matching,
        })
    }

    /// `Store::declared_claims`: per asked note, the current claims its `claims` edges reach, one
    /// row per edge (the note's own row first, then the newest), ranked by tier, newest, subject,
    /// predicate, at most `per_doc` of them with the full count.
    pub async fn declared_claims(
        &self,
        asked: &[(String, String)],
        per_doc: i64,
    ) -> Result<HashMap<String, RegisterRows>> {
        let doc_ids: Vec<String> = asked.iter().map(|(id, _)| id.clone()).collect();
        let edge_sql = format!(
            "SELECT src, dst FROM edge WHERE kind = 'claims' AND src IN {};",
            texts(&doc_ids)
        );
        let edges: Vec<EdgeEnd> =
            first_statement(&edge_sql, self.rows::<EdgeEnd>(&edge_sql).await?)?;
        let dsts: HashSet<String> = edges.iter().map(|e| e.dst.clone()).collect();
        let dst_list: Vec<String> = dsts.into_iter().collect();
        // `origin != NONE` is pg's inner join on the note the claim was read from
        let claim_sql = format!(
            "SELECT subject, predicate, value, kind, confidence, valid_from_us, project ?? '' AS project, source_path
               FROM claim_row
              WHERE superseded_us = NONE AND origin != NONE
                AND string::concat('claim:', subject, ':', predicate) IN {};",
            texts(&dst_list),
        );
        let claims: Vec<NoteClaim> =
            first_statement(&claim_sql, self.rows::<NoteClaim>(&claim_sql).await?)?;
        let mut by_node: HashMap<String, Vec<&NoteClaim>> = HashMap::new();
        for c in &claims {
            by_node
                .entry(format!("claim:{}:{}", c.subject, c.predicate))
                .or_default()
                .push(c);
        }
        let path_of: HashMap<&str, &str> = asked
            .iter()
            .map(|(id, path)| (id.as_str(), path.as_str()))
            .collect();
        // one pick per (note, claim node): the row written by the note itself, else the newest
        let mut picked: HashMap<(&str, &str), &NoteClaim> = HashMap::new();
        for e in &edges {
            let Some(&path) = path_of.get(e.src.as_str()) else {
                continue;
            };
            let best = by_node.get(&e.dst).and_then(|rows| {
                rows.iter()
                    .max_by_key(|c| (c.source_path == path, c.valid_from_us))
            });
            if let Some(best) = best {
                picked.insert((path, e.dst.as_str()), best);
            }
        }
        let mut notes_claims: HashMap<&str, Vec<&NoteClaim>> = HashMap::new();
        for ((path, _), claim) in picked {
            notes_claims.entry(path).or_default().push(claim);
        }
        let mut out = HashMap::new();
        for (path, mut rows) in notes_claims {
            rows.sort_by(|a, b| {
                tier(&a.kind)
                    .cmp(&tier(&b.kind))
                    .then_with(|| b.valid_from_us.cmp(&a.valid_from_us))
                    .then_with(|| {
                        slot_order((&a.subject, &a.predicate), (&b.subject, &b.predicate))
                    })
            });
            let total_matching = i64::try_from(rows.len()).context("declared count")?;
            let kept = rows
                .into_iter()
                .take(usize::try_from(per_doc.max(0)).unwrap_or(0))
                .map(|c| {
                    Ok(RegisterRow::new(
                        c.subject.clone(),
                        c.predicate.clone(),
                        c.value.clone(),
                        c.kind.clone(),
                        c.confidence.clone(),
                        micros_to_time(c.valid_from_us, "claim_row valid_from_us")?,
                        c.project.clone(),
                    ))
                })
                .collect::<Result<Vec<_>>>()?;
            out.insert(
                path.to_owned(),
                RegisterRows {
                    rows: kept,
                    total_matching,
                },
            );
        }
        Ok(out)
    }

    /// The author of each note, `unknown` for a note the copy does not hold (pg's `left join` +
    /// `coalesce`).
    async fn authors(&self, paths: &[String]) -> Result<HashMap<String, String>> {
        let sql = format!(
            "SELECT source_path, author FROM document WHERE source_path IN {};",
            texts(paths)
        );
        let rows: Vec<Author> = first_statement(&sql, self.rows::<Author>(&sql).await?)?;
        Ok(rows
            .into_iter()
            .map(|a| (a.source_path, a.author))
            .collect())
    }

    /// `Store::claim_source_rows`: the current claims of one subject, newest first.
    pub async fn claim_source_rows(&self, subject: &str) -> Result<Vec<ClaimRow>> {
        let sql = format!(
            "SELECT subject, value, source_path, valid_from_us FROM claim_row
              WHERE subject = {} AND superseded_us = NONE
              ORDER BY valid_from_us DESC, source_path ASC;",
            text(subject)
        );
        let rows: Vec<SourceRow> = first_statement(&sql, self.rows::<SourceRow>(&sql).await?)?;
        let paths: Vec<String> = rows.iter().map(|r| r.source_path.clone()).collect();
        let authors = self.authors(&paths).await?;
        rows.into_iter()
            .map(|r| claim_row_of(r, &authors))
            .collect()
    }

    /// `Store::claim_source_list_rows`: the current claims of one predicate, newest first.
    pub async fn claim_source_list_rows(&self, predicate: &str) -> Result<Vec<ListRow>> {
        let sql = format!(
            "SELECT subject, value, source_path, valid_from_us FROM claim_row
              WHERE predicate = {} AND superseded_us = NONE
              ORDER BY valid_from_us DESC, source_path ASC, subject ASC;",
            text(predicate)
        );
        let rows: Vec<SourceRow> = first_statement(&sql, self.rows::<SourceRow>(&sql).await?)?;
        let paths: Vec<String> = rows.iter().map(|r| r.source_path.clone()).collect();
        let authors = self.authors(&paths).await?;
        rows.into_iter()
            .map(|r| {
                let subject = r.subject.clone();
                Ok(ListRow {
                    subject,
                    row: claim_row_of(r, &authors)?,
                })
            })
            .collect()
    }

    /// `Store::claim_priors`: the current claims of one note, each with the newest sealed row of
    /// its slot that is older than it.
    pub async fn claim_priors(&self, source: &str) -> Result<Vec<ClaimPrior>> {
        let current_sql = format!(
            "SELECT subject, predicate, value, source_path, valid_from_us, 0 AS superseded_us
               FROM claim_row WHERE source_path = {} AND superseded_us = NONE;",
            text(source)
        );
        let mut current: Vec<PriorRow> =
            first_statement(&current_sql, self.rows::<PriorRow>(&current_sql).await?)?;
        current.sort_by(|a, b| {
            b.valid_from_us
                .cmp(&a.valid_from_us)
                .then_with(|| slot_order((&a.subject, &a.predicate), (&b.subject, &b.predicate)))
        });
        let subjects: Vec<String> = current
            .iter()
            .map(|c| c.subject.clone())
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        let sealed_sql = format!(
            "SELECT subject, predicate, value, source_path, valid_from_us, superseded_us
               FROM claim_row WHERE superseded_us != NONE AND subject IN {}
              ORDER BY valid_from_us DESC;",
            texts(&subjects)
        );
        let sealed: Vec<PriorRow> =
            first_statement(&sealed_sql, self.rows::<PriorRow>(&sealed_sql).await?)?;
        current
            .into_iter()
            .map(|c| {
                let prior = sealed.iter().find(|q| {
                    q.subject == c.subject
                        && q.predicate == c.predicate
                        && q.valid_from_us < c.valid_from_us
                });
                let (value, note, at) = match prior {
                    Some(q) => (
                        Some(q.value.clone()),
                        Some(q.source_path.clone()),
                        Some(utc(q.superseded_us, "claim_row superseded_us")?),
                    ),
                    None => (None, None, None),
                };
                Ok(ClaimPrior {
                    subject: c.subject,
                    predicate: c.predicate,
                    value: c.value,
                    prior: prior_of(value, note, at),
                })
            })
            .collect()
    }

    /// `Store::rule_rows`: current `rule` claims by subject, newest first inside a subject.
    pub async fn rule_rows(&self) -> Result<Vec<RuleRow>> {
        let sql = "SELECT subject, predicate, value, source_path, valid_from_us FROM claim_row
                    WHERE kind = 'rule' AND superseded_us = NONE
                    ORDER BY valid_from_us DESC, predicate ASC, source_path ASC;";
        let mut rows: Vec<RuleClaim> = first_statement(sql, self.rows::<RuleClaim>(sql).await?)?;
        // stable: the newest-first order survives inside a subject
        rows.sort_by(|a, b| en_us_order(&a.subject, &b.subject));
        Ok(rows
            .into_iter()
            .map(|r| RuleRow {
                subject: r.subject,
                predicate: r.predicate,
                value: r.value,
                source_path: r.source_path,
            })
            .collect())
    }

    /// `Store::split_subject_rows`: every live claim's `(subject, source_path)`.
    pub async fn split_subject_rows(&self) -> Result<Vec<(String, String)>> {
        let sql = "SELECT subject, source_path FROM claim_row WHERE superseded_us = NONE;";
        let rows: Vec<SubjectPath> = first_statement(sql, self.rows::<SubjectPath>(sql).await?)?;
        Ok(rows
            .into_iter()
            .map(|r| (r.subject, r.source_path))
            .collect())
    }

    /// `Store::split_samples`: per predicate of the given subjects' live claims, the row count and
    /// the first `SAMPLE_VALUE_CHARS` of the longest value; most rows first, `SAMPLE_LIMIT` kept.
    pub async fn split_samples(&self, variants: &[String]) -> Result<Vec<Sample>> {
        let sql = format!(
            "SELECT predicate, value FROM claim_row WHERE subject IN {} AND superseded_us = NONE;",
            texts(variants)
        );
        let rows: Vec<PredicateValue> =
            first_statement(&sql, self.rows::<PredicateValue>(&sql).await?)?;
        let mut by_predicate: HashMap<String, (i64, String)> = HashMap::new();
        for r in rows {
            let entry = by_predicate.entry(r.predicate).or_default();
            entry.0 += 1;
            // longest value, a tie to the first in the column collation (pg: order by length desc, value)
            let longer = r.value.chars().count().cmp(&entry.1.chars().count());
            if longer == Ordering::Greater
                || (longer == Ordering::Equal && en_us_order(&r.value, &entry.1) == Ordering::Less)
            {
                entry.1 = r.value;
            }
        }
        let mut samples: Vec<Sample> = by_predicate
            .into_iter()
            .map(|(predicate, (rows, value))| Sample {
                predicate,
                rows,
                value: value
                    .chars()
                    .take(usize::try_from(SAMPLE_VALUE_CHARS).unwrap_or(0))
                    .collect(),
            })
            .collect();
        samples.sort_by(|a, b| {
            b.rows
                .cmp(&a.rows)
                .then_with(|| en_us_order(&a.predicate, &b.predicate))
        });
        samples.truncate(usize::try_from(SAMPLE_LIMIT).unwrap_or(0));
        Ok(samples)
    }

    /// `Store::said_by_owner_counts`: current claims the owner said, per note.
    pub async fn said_by_owner_counts(&self, paths: &[String]) -> Result<HashMap<String, i64>> {
        let sql = format!(
            "SELECT source_path, count() AS n FROM claim_row
              WHERE source_path IN {} AND said_by = 'owner' AND superseded_us = NONE
              GROUP BY source_path;",
            texts(paths)
        );
        let rows: Vec<OwnerCount> = first_statement(&sql, self.rows::<OwnerCount>(&sql).await?)?;
        Ok(rows.into_iter().map(|r| (r.source_path, r.n)).collect())
    }

    /// `Store::claim_era_counts`: current claims per era, and those that only label something.
    pub async fn claim_era_counts(&self) -> Result<ClaimEraCounts> {
        let label_only = format!(
            "string::len(value) < {INFORMATIVE_VALUE_CHARS} OR string::matches(predicate, <regex> {})",
            text(&format!("(?i){TAUTOLOGICAL_PREDICATES}"))
        );
        let sql = format!(
            "SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND era = 'anchored' GROUP ALL;
             SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND era = 'unanchored' GROUP ALL;
             SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND era = 'pre-anchor' GROUP ALL;
             SELECT count() AS n FROM claim_row WHERE superseded_us = NONE AND ({label_only}) GROUP ALL;"
        );
        let counted = self.rows::<Count>(&sql).await?;
        let n = |i: usize| -> Result<usize> {
            let rows = counted
                .get(i)
                .with_context(|| format!("surreal reply has no statement {i} for: {sql}"))?;
            Ok(usize::try_from(rows.first().map_or(0, |c| c.n)).unwrap_or(0))
        };
        Ok(ClaimEraCounts {
            anchored: n(0)?,
            unanchored: n(1)?,
            pre_anchor: n(2)?,
            label_only: n(3)?,
        })
    }

    /// `Store::claim_stale_count`: rows marked stale, sealed or not.
    pub async fn claim_stale_count(&self) -> Result<usize> {
        let sql = "SELECT count() AS n FROM claim_row WHERE stale_us != NONE GROUP ALL;";
        let rows: Vec<Count> = first_statement(sql, self.rows::<Count>(sql).await?)?;
        Ok(usize::try_from(rows.first().map_or(0, |c| c.n)).unwrap_or(0))
    }

    /// `Store::claims_due_for_anchor_check`: pinned, not stale, past the watermark, oldest first.
    pub async fn claims_due_for_anchor_check(
        &self,
        after: Option<&AnchorCheckWatermark>,
        limit: usize,
    ) -> Result<Vec<AnchorCheckRow>> {
        let past = after.map_or_else(
            || Ok("true".to_owned()),
            |w| {
                let at = epoch_micros(w.valid_from)?;
                let (s, p) = (text(&w.subject), text(&w.predicate));
                Ok::<_, anyhow::Error>(format!(
                    "(valid_from_us > {at} OR (valid_from_us = {at} \
                      AND (subject > {s} OR (subject = {s} AND predicate > {p}))))"
                ))
            },
        )?;
        self.anchor_rows(&past, limit).await
    }

    /// `Store::claims_due_for_anchor_check_head`: the same list up to and including the watermark.
    pub async fn claims_due_for_anchor_check_head(
        &self,
        watermark: &AnchorCheckWatermark,
        limit: usize,
    ) -> Result<Vec<AnchorCheckRow>> {
        let at = epoch_micros(watermark.valid_from)?;
        let (s, p) = (text(&watermark.subject), text(&watermark.predicate));
        let upto = format!(
            "(valid_from_us < {at} OR (valid_from_us = {at} \
              AND (subject < {s} OR (subject = {s} AND predicate <= {p}))))"
        );
        self.anchor_rows(&upto, limit).await
    }

    async fn anchor_rows(&self, window: &str, limit: usize) -> Result<Vec<AnchorCheckRow>> {
        let sql = format!(
            "SELECT subject, predicate, valid_from_us, anchor, anchor_hash, anchor_symbol FROM claim_row
              WHERE anchor_hash != NONE AND stale_us = NONE AND {window}
              ORDER BY valid_from_us ASC, subject ASC, predicate ASC LIMIT {limit};"
        );
        let rows: Vec<AnchorRow> = first_statement(&sql, self.rows::<AnchorRow>(&sql).await?)?;
        rows.into_iter()
            .map(|r| {
                Ok(AnchorCheckRow {
                    anchor: r.anchor.with_context(|| {
                        format!(
                            "claim_row {}/{} has anchor_hash but no anchor",
                            r.subject, r.predicate
                        )
                    })?,
                    valid_from: micros_to_time(r.valid_from_us, "claim_row valid_from_us")?,
                    subject: r.subject,
                    predicate: r.predicate,
                    anchor_hash: r.anchor_hash,
                    anchor_symbol: r.anchor_symbol,
                })
            })
            .collect()
    }
}

fn epoch_micros(t: SystemTime) -> Result<i64> {
    let since = t
        .duration_since(SystemTime::UNIX_EPOCH)
        .context("watermark before 1970")?;
    i64::try_from(since.as_micros()).context("watermark micros overflow")
}

fn claim_row_of(r: SourceRow, authors: &HashMap<String, String>) -> Result<ClaimRow> {
    Ok(ClaimRow {
        valid_from: utc(r.valid_from_us, "claim_row valid_from_us")?.fixed_offset(),
        author: authors
            .get(&r.source_path)
            .cloned()
            .unwrap_or_else(|| "unknown".to_owned()),
        source_path: r.source_path,
        value: r.value,
    })
}
