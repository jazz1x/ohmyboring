//! The recurrence register on the Surreal copy. pg's `recurrences` joins `claim` with itself on a
//! cosine distance; here the open risk/blocked claims come out of Surreal (`claim_row` for the
//! text and the note's project, `claim` for the embedding) and the pairing runs in this file, so
//! the rows, their order and the f32 distance are what pg answers.

use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use regex::Regex;
use serde::Deserialize;
use serde::de::DeserializeOwned;

use super::{SurrealView, texts};

use super::super::{
    ClaimRef, INFORMATIVE_VALUE_CHARS, RECURRENCE_MAX_DISTANCE, RECURRENCE_MIN_DAYS_APART,
    Recurrence, TAUTOLOGICAL_PREDICATES,
};

const DAY_US: i64 = 86_400_000_000;

/// Rows per page: an embedding is ~11 KB of text, so 400 of them stay well under the answer size
/// `KEY_PAGE` already ruled out.
const PAGE: usize = 400;

#[derive(Deserialize)]
struct TextRow {
    cid: String,
    source_path: String,
    subject: String,
    predicate: String,
    value: String,
    kind: String,
    valid_from_us: i64,
    project: Option<String>,
}

#[derive(Deserialize)]
struct EmbeddingRow {
    cid: String,
    embedding: Vec<f32>,
}

impl SurrealView<'_> {
    async fn pages<T: DeserializeOwned>(&self, select: &str) -> Result<Vec<T>> {
        let mut all = Vec::new();
        loop {
            let sql = format!(
                "{select} ORDER BY cid ASC LIMIT {PAGE} START {};",
                all.len()
            );
            let page: Vec<T> = self
                .client
                .rows(&sql)
                .await?
                .into_iter()
                .next()
                .unwrap_or_default();
            let n = page.len();
            all.extend(page);
            if n < PAGE {
                return Ok(all);
            }
        }
    }

    /// Open risk/blocked claims of an existing note, text and note project from the `claim_row`
    /// copy, embedding from the `claim` copy (pg: `superseded_at IS NULL`, both `document` joins,
    /// `embedding IS NOT NULL`). A claim only one copy holds is not a pair end.
    pub(super) async fn recurrence_candidates(&self) -> Result<Vec<Candidate>> {
        let kinds = texts(&["risk".to_owned(), "blocked".to_owned()]);
        let texts_of: Vec<TextRow> = self
            .pages(
                &format!(
                    "SELECT cid, source_path, subject, predicate, value, kind, valid_from_us, project \
                       FROM claim_row WHERE kind IN {kinds} AND superseded_us = NONE AND origin != NONE"
                ),
            )
            .await?;
        let embeddings: Vec<EmbeddingRow> = self
            .pages(&format!(
                "SELECT cid, embedding FROM claim WHERE kind IN {kinds}"
            ))
            .await?;
        let mut by_cid: HashMap<String, Vec<f32>> = embeddings
            .into_iter()
            .map(|e| (e.cid, e.embedding))
            .collect();
        Ok(texts_of
            .into_iter()
            .filter_map(|t| {
                by_cid.remove(&t.cid).map(|embedding| Candidate {
                    source_path: t.source_path,
                    subject: t.subject,
                    predicate: t.predicate,
                    value: t.value,
                    kind: t.kind,
                    valid_from_us: t.valid_from_us,
                    project: t.project.unwrap_or_default(),
                    embedding,
                })
            })
            .collect())
    }
}

/// One open risk/blocked claim with its embedding, the unit the pairing works on.
#[derive(Debug, Clone)]
pub(super) struct Candidate {
    pub source_path: String,
    pub subject: String,
    pub predicate: String,
    pub value: String,
    pub kind: String,
    pub valid_from_us: i64,
    pub project: String,
    pub embedding: Vec<f32>,
}

impl Candidate {
    fn claim_ref(&self) -> ClaimRef {
        ClaimRef {
            source_path: self.source_path.clone(),
            subject: self.subject.clone(),
            predicate: self.predicate.clone(),
            value: self.value.clone(),
            kind: self.kind.clone(),
            valid_from: UNIX_EPOCH + Duration::from_micros(self.valid_from_us.unsigned_abs()),
        }
    }

    fn informative(&self) -> bool {
        i32::try_from(self.value.chars().count()).is_ok_and(|n| n >= INFORMATIVE_VALUE_CHARS)
    }
}

/// `1 - cos` the way pgvector's `<=>` computes it: three float sums over the dimensions, then the
/// quotient in double, clamped to [-1, 1].
fn cosine_distance(dot: f32, norm_a: f32, norm_b: f32) -> f64 {
    let similarity = f64::from(dot) / (f64::from(norm_a) * f64::from(norm_b)).sqrt();
    1.0 - similarity.clamp(-1.0, 1.0)
}

/// Four fused-multiply-add lanes (element `i` goes to lane `i % 4`), joined `(l0 + l1) + (l2 + l3)`:
/// the order pgvector 0.8.6 sums in on aarch64. Measured against the pg `<=>` of 36 pairs of live
/// embeddings (1024 dims): this order reproduces the double bit for bit 36 of 36; one sequential
/// f32 sum 0 of 36, four lanes with a different join or without fused multiply-add 5 to 14 of 36.
/// A different build of pgvector may sum in another order; the register's f32 digits follow it.
fn inner_product(a: &[f32], b: &[f32]) -> f32 {
    let mut lanes = [0.0_f32; 4];
    for (i, (x, y)) in a.iter().zip(b).enumerate() {
        lanes[i % 4] = x.mul_add(*y, lanes[i % 4]);
    }
    (lanes[0] + lanes[1]) + (lanes[2] + lanes[3])
}

/// The UTC calendar day of an epoch-microsecond instant (pg's `valid_from::date` in a UTC session).
fn day_of(us: i64) -> i64 {
    us.div_euclid(DAY_US)
}

/// Mirror of `vector_pg.rs` `recurrences`: pairs (newer `a`, older `b`) of open, embedded
/// risk/blocked claims from different notes, informative values, `a` within `days` of `now`,
/// `a` at least `RECURRENCE_MIN_DAYS_APART` days after `b`, distance within the ceiling and, with
/// a project, both notes in it. Nearest pair first, then the newer claim's `valid_from` latest
/// first; a newer claim's pairs gather under its first row; `limit` counts newers.
pub(super) fn pair(
    claims: &[Candidate],
    now: SystemTime,
    days: i64,
    project: Option<&str>,
    limit: i64,
) -> Result<Vec<Recurrence>, regex::Error> {
    let tautology = Regex::new(&format!("(?i){TAUTOLOGICAL_PREDICATES}"))?;
    let now_us = i64::try_from(now.duration_since(UNIX_EPOCH).map_or(0, |d| d.as_micros()))
        .unwrap_or(i64::MAX);
    let cutoff_us = now_us.saturating_sub(days.saturating_mul(DAY_US));
    let ceiling = f64::from(RECURRENCE_MAX_DISTANCE);
    let eligible: Vec<&Candidate> = claims
        .iter()
        .filter(|c| c.informative() && project.is_none_or(|p| c.project == p))
        .collect();
    let norms: Vec<f32> = eligible
        .iter()
        .map(|c| inner_product(&c.embedding, &c.embedding))
        .collect();
    let mut pairs: Vec<(f32, usize, usize)> = Vec::new();
    for (i, a) in eligible.iter().enumerate() {
        if a.valid_from_us < cutoff_us {
            continue;
        }
        for (j, b) in eligible.iter().enumerate() {
            let apart = day_of(a.valid_from_us) - day_of(b.valid_from_us);
            if b.source_path == a.source_path
                || apart < RECURRENCE_MIN_DAYS_APART
                || a.embedding.len() != b.embedding.len()
            {
                continue;
            }
            let dot = inner_product(&a.embedding, &b.embedding);
            let d = cosine_distance(dot, norms[i], norms[j]);
            if d <= ceiling {
                #[allow(clippy::cast_possible_truncation)]
                pairs.push((d as f32, i, j));
            }
        }
    }
    pairs.sort_by(|x, y| {
        x.0.total_cmp(&y.0).then_with(|| {
            eligible[y.1]
                .valid_from_us
                .cmp(&eligible[x.1].valid_from_us)
        })
    });
    let mut out: Vec<Recurrence> = Vec::new();
    let mut index: HashMap<(&str, &str, &str, i64), usize> = HashMap::new();
    for (distance, i, j) in pairs {
        let (a, b) = (eligible[i], eligible[j]);
        let key = (
            a.source_path.as_str(),
            a.subject.as_str(),
            a.predicate.as_str(),
            a.valid_from_us,
        );
        if let Some(&at) = index.get(&key) {
            out[at].older.push(b.claim_ref());
        } else {
            index.insert(key, out.len());
            out.push(Recurrence {
                newer: a.claim_ref(),
                older: vec![b.claim_ref()],
                distance,
                days_apart: day_of(a.valid_from_us) - day_of(b.valid_from_us),
                label_only: tautology.is_match(&a.predicate),
            });
        }
    }
    for row in &mut out {
        row.older.sort_by_key(|c| c.valid_from);
    }
    if limit >= 0 {
        out.truncate(usize::try_from(limit).unwrap_or(out.len()));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn claim(path: &str, day: i64, value: &str, emb: [f32; 2]) -> Candidate {
        Candidate {
            source_path: path.to_owned(),
            subject: format!("s-{path}"),
            predicate: "incident".to_owned(),
            value: value.to_owned(),
            kind: "risk".to_owned(),
            valid_from_us: day * DAY_US + 1,
            project: "omb".to_owned(),
            embedding: emb.to_vec(),
        }
    }

    /// The distance is pgvector's own digits: pg 16 / pgvector 0.8.6 (aarch64) answers
    /// `<=>` of these two 12-dim vectors as 1.3354307810063792; a plain f32 sum gives ...7892998662.
    #[test]
    fn the_distance_sums_in_the_order_pgvector_does() {
        let a: [u32; 12] = [
            0xbeb4_652f,
            0xbf32_c3e6,
            0x3e9a_8e91,
            0xbf5a_e9a2,
            0x3d92_f902,
            0xbe89_88d8,
            0xbf62_4df7,
            0x3c73_a773,
            0xbf6c_cd5e,
            0xbe07_e4c6,
            0xbf5c_3be9,
            0xbf51_8e10,
        ];
        let b: [u32; 12] = [
            0xbe1a_95af,
            0x3f27_5929,
            0xbf40_9d08,
            0xbf0d_b39f,
            0x3e82_7ddb,
            0x3f65_3a1b,
            0x3e1d_e827,
            0xbe53_9930,
            0x3f73_d7b6,
            0xbf68_2651,
            0x3f37_892e,
            0xbed7_70aa,
        ];
        let (a, b) = (a.map(f32::from_bits), b.map(f32::from_bits));
        let d = cosine_distance(
            inner_product(&a, &b),
            inner_product(&a, &a),
            inner_product(&b, &b),
        );
        assert_eq!(d, 1.335_430_781_006_379_2);
    }

    const LONG: &str = "the orca cli cannot connect to the running app";

    fn now_day(d: i64) -> SystemTime {
        UNIX_EPOCH + Duration::from_micros((d * DAY_US).unsigned_abs())
    }

    /// An older claim of another note, 3 days back and in the same direction, is a recurrence;
    /// one only 1 day back and one pointing elsewhere are not.
    #[test]
    fn pairs_only_the_far_close_and_foreign_claims() -> Result<()> {
        let claims = [
            claim("new", 100, LONG, [1.0, 0.0]),
            claim("old", 97, LONG, [1.0, 0.01]),
            claim("yesterday", 99, LONG, [1.0, 0.0]),
            claim("elsewhere", 90, LONG, [0.0, 1.0]),
        ];
        let got = pair(&claims, now_day(101), 30, None, 10)?;
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].newer.source_path, "new");
        assert_eq!(got[0].older.len(), 1);
        assert_eq!(got[0].older[0].source_path, "old");
        assert_eq!(got[0].days_apart, 3);
        assert!(got[0].label_only);
        Ok(())
    }

    /// `days` bounds the newer claim only, and a short value reads as a tag and never pairs.
    #[test]
    fn the_window_bounds_the_newer_claim_and_a_short_value_never_pairs() -> Result<()> {
        let claims = [
            claim("new", 100, LONG, [1.0, 0.0]),
            claim("old", 50, LONG, [1.0, 0.0]),
            claim("tag", 10, "short tag", [1.0, 0.0]),
        ];
        assert_eq!(pair(&claims, now_day(101), 30, None, 10)?.len(), 1);
        assert!(pair(&claims, now_day(200), 30, None, 10)?.is_empty());
        Ok(())
    }

    /// `limit` counts newer claims, not pairs, and a project keeps pairs of that project only.
    #[test]
    fn limit_counts_newers_and_project_filters_both_ends() -> Result<()> {
        let mut other = claim("other", 80, LONG, [1.0, 0.0]);
        other.project = "x".to_owned();
        let claims = [
            claim("new", 100, LONG, [1.0, 0.0]),
            claim("old1", 90, LONG, [1.0, 0.0]),
            claim("old2", 80, LONG, [1.0, 0.0]),
            other,
        ];
        let all = pair(&claims, now_day(101), 30, None, 10)?;
        assert_eq!(all[0].older.len(), 3);
        let mine = pair(&claims, now_day(101), 30, Some("omb"), 10)?;
        assert_eq!(mine[0].older.len(), 2);
        let first = pair(&claims, now_day(101), 30, None, 1)?;
        assert_eq!(first.len(), 1);
        Ok(())
    }
}
