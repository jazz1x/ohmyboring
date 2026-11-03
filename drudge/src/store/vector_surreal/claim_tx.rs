//! C-SURALL2 Z1: the three places pg locks claim rows (`FOR UPDATE OF c`), done on Surreal.
//!
//! pg takes row locks so that two requests on one slot wait for each other. Surreal has snapshot
//! isolation and no row lock: a transaction that only READS a row does not conflict with one that
//! writes it, so a read-then-write loses an update silently (a lost update, not an error). The
//! equivalent is a write both transactions make: every transaction here first bumps a
//! `claim_slot:<subject, predicate>` guard row, which turns "reads the slot" into a write-write
//! conflict on the guard. The loser fails at COMMIT with a conflict error and the whole
//! transaction is sent again (over HTTP only `BEGIN ... COMMIT` inside one request is a
//! transaction, so a retry is a new request that re-reads).
//!
//! The three places, by what they replace in `store.rs`:
//! - `claim_slot_upsert`  <- `lock_claim_slot_on` + the upsert and seal of `upsert_claim_with_anchor_on`
//! - `seal_note`          <- `lock_claim_slots_of` + `seal_superseded_claims` (every slot of a note)
//! - `merge_split_subject` <- `lock_split_slots` + `Store::merge_split_subject`
//!
//! They work on Surreal's own rows: `claim_row` (every claim, sealed ones too), the vector `claim`
//! table (open claims with an embedding), `document` and `edge`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime};

use anyhow::{Context, Result, anyhow, bail};
use serde::Deserialize;
use serde_json::{Value, json};

use super::claim_reads::epoch_micros;
use super::reads::en_us_order;
use super::{KEY_SEP, RpcReply, SurrealClient, claim_key, floats, rejection, text, texts};
use crate::ingest::canon;
use crate::split_subjects::{Merged, find_group};

/// Whole-transaction resends before a conflict goes up as an error. A conflict means another
/// transaction committed a write to the same guard between this one's snapshot and its COMMIT, so
/// each failed attempt is somebody else's progress: k writers racing for one slot cost at most
/// k-1 conflicts for the unluckiest. The writers of one slot are the ingest, the door, a seal
/// pass and a repair, single digits at the busiest; 8 clears that with room and still ends in
/// well under a second of backoff, so a stuck conflict reads as an error, not a hang.
pub const TX_RETRIES: u32 = 8;

/// What the split merge throws when the rows it planned on changed before it committed: not a
/// Surreal conflict, but the same remedy (read again, plan again).
const SPLIT_STALE: &str = "z1-split-stale";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TxPlace {
    Slot,
    Note,
    Split,
    /// C0b1: a note's `document` + `chunk` rows written by the engine (`doc_tx.rs`).
    Doc,
}

impl TxPlace {
    pub const ALL: [Self; 4] = [Self::Slot, Self::Note, Self::Split, Self::Doc];

    fn index(self) -> usize {
        match self {
            Self::Slot => 0,
            Self::Note => 1,
            Self::Split => 2,
            Self::Doc => 3,
        }
    }

    #[must_use]
    pub fn name(self) -> &'static str {
        match self {
            Self::Slot => "slot",
            Self::Note => "note",
            Self::Split => "split",
            Self::Doc => "doc",
        }
    }
}

#[derive(Debug, Default)]
struct Cell {
    committed: AtomicU64,
    conflicts: AtomicU64,
    retries: AtomicU64,
    exhausted: AtomicU64,
}

/// Per place: transactions committed, conflicts seen, resends made, requests that gave up.
#[derive(Debug, Default)]
pub struct TxStats {
    cells: [Cell; 4],
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct TxCount {
    pub committed: u64,
    pub conflicts: u64,
    pub retries: u64,
    pub exhausted: u64,
}

pub(super) enum Attempt<T> {
    Done(T),
    Conflict(String),
}

/// Who asks for the split merge: the owner may rewrite owner-written notes, anyone else leaves
/// them as they are (`Store::merge_split_subject`'s `Standing`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SplitStanding {
    Owner,
    NotOwner,
}

/// One claim row to write, in the terms `Store::upsert_claim_with_anchor` takes.
#[derive(Debug, Clone, Copy)]
pub struct ClaimWrite<'a> {
    pub subject: &'a str,
    pub predicate: &'a str,
    pub value: &'a str,
    pub source_path: &'a str,
    pub valid_from_us: i64,
    pub embedding: &'a [f32],
    pub kind: &'a str,
    pub confidence: &'a str,
    pub anchor: Option<&'a str>,
    pub anchor_hash: Option<&'a str>,
    pub anchor_symbol: Option<&'a str>,
}

#[derive(Deserialize)]
struct LiveRow {
    subject: String,
    predicate: String,
    source_path: String,
    valid_from_us: i64,
}

#[derive(Deserialize)]
struct SplitAnswer {
    deleted: u64,
    reread: u64,
    owner_held: Vec<String>,
}

fn opt(v: Option<&str>) -> String {
    v.map_or_else(|| "NONE".to_owned(), text)
}

/// The guard of one slot, keyed the way pg's lock is: by the canon spelling of subject and
/// predicate (`lock_claim_slot_on` matches `canon(c.subject) = canon($1)`), so two spellings of
/// one slot meet on one guard.
fn guard_key(subject: &str, predicate: &str) -> String {
    format!("{}{KEY_SEP}{}", canon(subject), canon(predicate))
}

/// `LET $guards = [..]` for these `(subject, predicate)` pairs: their canon keys, each once.
fn guards_let<'a>(pairs: impl IntoIterator<Item = (&'a str, &'a str)>) -> String {
    let keys: std::collections::BTreeSet<String> =
        pairs.into_iter().map(|(s, p)| guard_key(s, p)).collect();
    let keys: Vec<String> = keys.into_iter().collect();
    format!("LET $guards = {};", texts(&keys))
}

/// `[[s, p], ..]` as a SurrealQL literal, each pair once.
fn pairs_literal(pairs: &[(String, String)]) -> String {
    let set: std::collections::BTreeSet<&(String, String)> = pairs.iter().collect();
    let inner: Vec<String> = set
        .into_iter()
        .map(|(s, p)| format!("[{},{}]", text(s), text(p)))
        .collect();
    format!("[{}]", inner.join(","))
}

/// Throws `SPLIT_STALE` when the pairs a transaction planned its guards on are not the pairs in
/// `$slots` any more (a spelling appeared or went away since the plan was read).
const SLOTS_UNCHANGED: &str = "IF array::len(array::complement($slots, $planned)) > 0 OR \
     array::len(array::complement($planned, $slots)) > 0 { THROW 'z1-split-stale'; };";

/// The write both sides of a race make; the Surreal stand-in for pg's row lock.
const BUMP_GUARDS: &str =
    "FOR $g IN $guards { UPSERT type::record('claim_slot', $g) SET n = (n ?? 0) + 1; };";

/// The notes that hold a row in `$slots`, which of them (or of the notes that replaced them) the
/// owner wrote, and which are retired: `Store`'s `RETIRED_NOTE` over Surreal's rows. A
/// `supersedes` edge retires its older note when its newer note exists and either the newer note
/// is owner-written or the older one is not.
const RETIRED_LETS: &str = "\
LET $paths = array::distinct(SELECT VALUE source_path FROM claim_row WHERE [subject, predicate] INSIDE $slots);
LET $dsts = array::distinct(SELECT VALUE string::concat('doc:', source_path) FROM claim_row WHERE [subject, predicate] INSIDE $slots);
LET $srcs = array::distinct(SELECT VALUE string::slice(src, 4) FROM edge WHERE kind = 'supersedes' AND dst INSIDE $dsts);
LET $src_docs = SELECT VALUE source_path FROM document WHERE source_path INSIDE $srcs;
LET $owners = SELECT VALUE source_path FROM document WHERE author = 'owner' AND source_path INSIDE array::union($paths, $srcs);
LET $retired = SELECT VALUE string::slice(dst, 4) FROM edge WHERE kind = 'supersedes' AND dst INSIDE $dsts AND string::slice(src, 4) INSIDE $src_docs AND (string::slice(src, 4) INSIDE $owners OR !(string::slice(dst, 4) INSIDE $owners));";

/// Open rows of `$slots` missing from `claim` are put back from `claim_row` with their stored
/// embedding: pg keeps it on a sealed row, so a promoted row returns with its own floats.
/// `$force` is the row written now, put in even if present (its embedding may be new).
const OPEN_ROWS_TO_CLAIM: &str = "\
LET $have = SELECT VALUE cid FROM claim WHERE [subject, predicate] INSIDE $slots;
FOR $r IN (SELECT * FROM claim_row WHERE [subject, predicate] INSIDE $slots AND superseded_us = NONE AND embedding != NONE AND origin != NONE AND project != NONE AND (cid = $force OR !(cid INSIDE $have))) {
    UPSERT type::record('claim', $r.cid) CONTENT {cid: $r.cid, subject: $r.subject, predicate: $r.predicate,
        valid_from_us: $r.valid_from_us, source_path: $r.source_path, origin: $r.origin, project: $r.project,
        kind: $r.kind, anchor: $r.anchor, stale_us: $r.stale_us, embedding: $r.embedding};
};";

/// The rows a transaction wrote to or took out of the vector `claim` table, added to
/// `vector_meta:claim.dirty` (what `swap_if_changed` reads). Since C0b1 bundle 3 no copy from pg
/// bumps it, so each write transaction does; `n` is a SurrealQL integer expression.
fn claim_dirty_bump(n: &str) -> String {
    format!("UPSERT type::record('vector_meta', 'claim') SET dirty = (dirty ?? 0) + {n};")
}

fn slot_sql(w: &ClaimWrite<'_>) -> String {
    let cid = text(&claim_key(w.subject, w.predicate, w.valid_from_us));
    let (s, p, sp) = (text(w.subject), text(w.predicate), text(w.source_path));
    let era = if w.anchor.is_some() {
        "'anchored'"
    } else {
        "'unanchored'"
    };
    let guards = guards_let([(w.subject, w.predicate)]);
    let slots = format!("LET $slots = [[{s},{p}]];");
    let seal = if w.kind == "fact" {
        format!(
            "LET $mx = (SELECT VALUE valid_from_us FROM claim_row WHERE subject = {s} AND predicate = {p} AND !(source_path INSIDE $retired) ORDER BY valid_from_us DESC LIMIT 1)[0];
             IF $mx != NONE {{
                 LET $latest_owner = array::len(SELECT VALUE cid FROM claim_row WHERE subject = {s} AND predicate = {p} AND valid_from_us = $mx AND source_path INSIDE $owners) > 0;
                 UPDATE claim_row SET superseded_us = $mx, via = 'surreal' WHERE subject = {s} AND predicate = {p} AND valid_from_us < $mx AND superseded_us != $mx AND (!(source_path INSIDE $owners) OR $latest_owner);
                 UPDATE claim_row SET superseded_us = NONE, via = 'surreal' WHERE subject = {s} AND predicate = {p} AND superseded_us != NONE AND valid_from_us = $mx;
             }};"
        )
    } else {
        format!(
            "LET $mx = (SELECT VALUE valid_from_us FROM claim_row WHERE subject = {s} AND predicate = {p} AND source_path = {sp} ORDER BY valid_from_us DESC LIMIT 1)[0];
             UPDATE claim_row SET superseded_us = $mx, via = 'surreal' WHERE subject = {s} AND predicate = {p} AND source_path = {sp} AND valid_from_us < $mx AND superseded_us != $mx;
             IF !({sp} INSIDE $retired) {{
                 UPDATE claim_row SET superseded_us = NONE, via = 'surreal' WHERE subject = {s} AND predicate = {p} AND source_path = {sp} AND superseded_us != NONE AND valid_from_us = $mx;
             }};"
        )
    };
    format!(
        "{guards}
         LET $force = {cid};
         {BUMP_GUARDS}
         {slots}
         LET $d = (SELECT origin, project FROM document WHERE source_path = {sp})[0];
         UPSERT type::record('claim_row', {cid}) SET cid = {cid}, subject = {s}, predicate = {p},
             value = {value}, source_path = {sp}, valid_from_us = {vf}, kind = {kind},
             confidence = {conf}, anchor = {anchor}, anchor_hash = {ah}, anchor_symbol = {asym},
             era = {era}, origin = $d.origin, project = $d.project, via = 'surreal',
             embedding = {emb};
         {RETIRED_LETS}
         {seal}
         DELETE claim WHERE subject = {s} AND predicate = {p} AND cid INSIDE (SELECT VALUE cid FROM claim_row WHERE subject = {s} AND predicate = {p} AND superseded_us != NONE);
         {OPEN_ROWS_TO_CLAIM}
         {bump}",
        bump = claim_dirty_bump("1"),
        value = text(w.value),
        vf = w.valid_from_us,
        kind = text(w.kind),
        conf = text(w.confidence),
        anchor = opt(w.anchor),
        ah = opt(w.anchor_hash),
        asym = opt(w.anchor_symbol),
        emb = floats(w.embedding),
    )
}

/// One canon slot as the spellings of it that exist in `claim_row`: the canon-spelled ones
/// (subject and predicate equal their own canon) and every other spelling. pg's unseal ranks a
/// canon-spelled row above any other whatever its `valid_from` (`is_canon_sql`).
#[derive(Debug, Default, PartialEq, Eq)]
struct SlotGroup {
    canon: Vec<(String, String)>,
    other: Vec<(String, String)>,
}

/// The canon slots `touched` names, each with every spelling `every` holds for it. `canon` is
/// `guard_key`'s, so a slot's group is the group its guard covers.
fn slot_groups(touched: &[(String, String)], every: &[(String, String)]) -> Vec<SlotGroup> {
    let keys: std::collections::BTreeSet<String> =
        touched.iter().map(|(s, p)| guard_key(s, p)).collect();
    let mut groups: std::collections::BTreeMap<String, SlotGroup> = keys
        .into_iter()
        .map(|k| (k, SlotGroup::default()))
        .collect();
    for (s, p) in every {
        if let Some(g) = groups.get_mut(&guard_key(s, p)) {
            if *s == canon(s) && *p == canon(p) {
                g.canon.push((s.clone(), p.clone()));
            } else {
                g.other.push((s.clone(), p.clone()));
            }
        }
    }
    groups.into_values().collect()
}

/// `[{c: [[s, p], ..], o: [[s, p], ..]}, ..]` as a SurrealQL literal.
fn groups_literal(groups: &[SlotGroup]) -> String {
    let each: Vec<String> = groups
        .iter()
        .map(|g| {
            format!(
                "{{c: {}, o: {}}}",
                pairs_literal(&g.canon),
                pairs_literal(&g.other)
            )
        })
        .collect();
    format!("[{}]", each.join(","))
}

/// Everything `note_sql` plans from rows read before the transaction.
struct NotePlan {
    /// `(subject, predicate)` of every row of `older`.
    planned: Vec<(String, String)>,
    /// The same for `older`'s fact rows.
    facts: Vec<(String, String)>,
    /// Every distinct `(subject, predicate)` in `claim_row`.
    every: Vec<(String, String)>,
}

/// `plan.planned` is the `(subject, predicate)` pairs of `older`'s rows as read before the
/// transaction; the guards are made from them, and the transaction throws if the rows hold other
/// pairs now (or if `claim_row` holds a different set of spellings than `plan.every`, which is
/// what the unseal's groups were made from: a swapped spelling keeps the count and not the set).
fn note_sql(newer: &str, older: &str, plan: &NotePlan) -> String {
    let (n, o) = (text(newer), text(older));
    let guards = guards_let(plan.planned.iter().map(|(s, p)| (s.as_str(), p.as_str())));
    let groups = slot_groups(&plan.facts, &plan.every);
    let members: Vec<(String, String)> = groups
        .iter()
        .flat_map(|g| g.canon.iter().chain(g.other.iter()).cloned())
        .chain(plan.planned.iter().cloned())
        .collect();
    let (planned, facts, members_lit, groups_lit) = (
        pairs_literal(&plan.planned),
        pairs_literal(&plan.facts),
        pairs_literal(&members),
        groups_literal(&groups),
    );
    let every = pairs_literal(&plan.every);
    format!(
        "{guards}
         LET $planned = {planned};
         LET $slots = array::distinct(SELECT VALUE [subject, predicate] FROM claim_row WHERE source_path = {o});
         {SLOTS_UNCHANGED}
         {BUMP_GUARDS}
         LET $fslots = array::distinct(SELECT VALUE [subject, predicate] FROM claim_row WHERE source_path = {o} AND kind = 'fact');
         LET $seen = {every};
         LET $spelled = array::distinct(SELECT VALUE [subject, predicate] FROM claim_row);
         IF array::len(array::complement($fslots, {facts})) > 0 OR array::len(array::complement({facts}, $fslots)) > 0 OR array::len(array::complement($spelled, $seen)) > 0 OR array::len(array::complement($seen, $spelled)) > 0 {{ THROW {stale}; }};
         LET $slots = {members_lit};
         LET $groups = {groups_lit};
         {RETIRED_LETS}
         LET $newest = (SELECT VALUE valid_from_us FROM claim_row WHERE source_path = {n} ORDER BY valid_from_us DESC LIMIT 1)[0];
         LET $stamp = $newest ?? time::micros(time::now());
         LET $sealed = UPDATE claim_row SET superseded_us = $stamp, via = 'surreal' WHERE source_path = {o} AND superseded_us = NONE AND {o} INSIDE $retired RETURN VALUE cid;
         DELETE claim WHERE source_path = {o} AND cid INSIDE $sealed;
         FOR $g IN $groups {{
             LET $lc = SELECT VALUE valid_from_us FROM claim_row WHERE [subject, predicate] INSIDE $g.c AND !(source_path INSIDE $retired) ORDER BY valid_from_us DESC LIMIT 1;
             LET $lo = SELECT VALUE valid_from_us FROM claim_row WHERE [subject, predicate] INSIDE $g.o AND !(source_path INSIDE $retired) ORDER BY valid_from_us DESC LIMIT 1;
             IF array::len($lc) > 0 {{
                 UPDATE claim_row SET superseded_us = NONE, via = 'surreal' WHERE [subject, predicate] INSIDE $g.c AND kind = 'fact' AND superseded_us != NONE AND valid_from_us = $lc[0];
             }} ELSE IF array::len($lo) > 0 {{
                 UPDATE claim_row SET superseded_us = NONE, via = 'surreal' WHERE [subject, predicate] INSIDE $g.o AND kind = 'fact' AND superseded_us != NONE AND valid_from_us = $lo[0];
             }};
         }};
         LET $force = NONE;
         {OPEN_ROWS_TO_CLAIM}
         {bump}
         RETURN array::len($sealed);",
        bump = claim_dirty_bump("array::len($sealed)"),
        stale = text(SPLIT_STALE),
    )
}

/// `Store::seal_restated_slots` on Surreal: close the rows of `older` in the slots `newer`
/// re-states, stamped with `newer`'s latest `valid_from`. The slots (and so the guards) are the
/// ones `planned` names, read before the transaction and checked again inside it. Nothing is
/// handed on to a next-latest row, and the owner rule is `RETIRED_LETS`.
fn restated_sql(newer: &str, older: &str, planned: &[(String, String)]) -> String {
    let (n, o) = (text(newer), text(older));
    let guards = guards_let(planned.iter().map(|(s, p)| (s.as_str(), p.as_str())));
    format!(
        "{guards}
         LET $planned = {planned};
         LET $slots = array::distinct(SELECT VALUE [subject, predicate] FROM claim_row WHERE source_path = {n});
         {SLOTS_UNCHANGED}
         {BUMP_GUARDS}
         {RETIRED_LETS}
         LET $newest = (SELECT VALUE valid_from_us FROM claim_row WHERE source_path = {n} ORDER BY valid_from_us DESC LIMIT 1)[0];
         LET $stamp = $newest ?? time::micros(time::now());
         LET $sealed = UPDATE claim_row SET superseded_us = $stamp, via = 'surreal' WHERE source_path = {o} AND superseded_us = NONE AND {o} INSIDE $retired AND [subject, predicate] INSIDE $slots RETURN VALUE cid;
         DELETE claim WHERE source_path = {o} AND cid INSIDE $sealed;
         {bump}
         RETURN array::len($sealed);",
        planned = pairs_literal(planned),
        bump = claim_dirty_bump("array::len($sealed)"),
    )
}

/// The `claim_row` and `claim` rows of one note leave with its `document` (`delete_sql`).
pub(super) fn delete_claims_sql(path: &str) -> String {
    let p = text(path);
    format!(
        "LET $claims_gone = (SELECT VALUE cid FROM claim WHERE source_path = {p});
         DELETE claim_row WHERE source_path = {p};
         DELETE claim WHERE source_path = {p};
         {bump}",
        bump = claim_dirty_bump("array::len($claims_gone)")
    )
}

fn split_sql(
    variants: &[String],
    notes: &[String],
    expected: &[String],
    planned: &[(String, String)],
    standing: SplitStanding,
) -> String {
    let guards = guards_let(planned.iter().map(|(s, p)| (s.as_str(), p.as_str())));
    let planned = pairs_literal(planned);
    let owner = match standing {
        SplitStanding::Owner => "LET $owner = [];".to_owned(),
        SplitStanding::NotOwner => format!(
            "LET $owner = array::sort(SELECT VALUE source_path FROM document WHERE author = 'owner' AND source_path INSIDE {});",
            texts(notes)
        ),
    };
    let v = texts(variants);
    format!(
        "{guards}
         LET $planned = {planned};
         LET $now = SELECT VALUE cid FROM claim_row WHERE subject INSIDE {v} AND superseded_us = NONE;
         LET $want = {want};
         IF array::len(array::complement($now, $want)) > 0 OR array::len(array::complement($want, $now)) > 0 {{ THROW {stale}; }};
         LET $slots = array::distinct(SELECT VALUE [subject, predicate] FROM claim_row WHERE subject INSIDE {v});
         {SLOTS_UNCHANGED}
         {BUMP_GUARDS}
         {owner}
         LET $touched = array::complement({notes}, $owner);
         LET $gone = SELECT VALUE cid FROM claim_row WHERE subject INSIDE {v} AND source_path INSIDE $touched AND superseded_us = NONE;
         FOR $c IN $gone {{ UPSERT type::record('claim_gone', $c) SET cid = $c, via = 'surreal'; }};
         DELETE claim_row WHERE cid INSIDE $gone;
         DELETE claim WHERE cid INSIDE $gone;
         LET $blanked = UPDATE document SET sha = '', via = 'surreal' WHERE source_path INSIDE $touched RETURN source_path;
         RETURN {{deleted: array::len($gone), reread: array::len($blanked), owner_held: $owner}};",
        want = texts(expected),
        stale = text(SPLIT_STALE),
        notes = texts(notes),
    )
}

fn is_retryable(message: &str) -> bool {
    message.contains("conflict")
        || message.contains("can be retried")
        || message.contains(SPLIT_STALE)
}

fn backoff(attempt: u32) -> Duration {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::from(d.subsec_nanos()));
    Duration::from_millis(u64::from(attempt) * (3 + nanos % 7))
}

#[derive(Deserialize)]
struct KindRow {
    subject: String,
    predicate: String,
    kind: String,
}

#[derive(Deserialize)]
struct PairRow {
    subject: String,
    predicate: String,
}

impl SurrealClient {
    /// `claim_row.via` marks a row a Surreal write made or moved (`'surreal'`); `claim_row_diff`
    /// (the Z4 compare against pg) skips marked rows. Since C0b1 bundle 3 no pass copies pg's
    /// rows over Surreal's, so the mark no longer protects a row from anything.
    /// A table laid down by `fill_from` has the field; one made before it gets it here, once per
    /// client (a DDL statement inside a racing transaction would itself conflict).
    pub(super) async fn ensure_via_field(&self) -> Result<()> {
        self.claim_via
            .get_or_try_init(|| async {
                self.run(
                    "DEFINE FIELD IF NOT EXISTS via ON claim_row TYPE option<string>;
                     DEFINE FIELD IF NOT EXISTS embedding ON claim_row TYPE option<array<float>>;
                     DEFINE FIELD IF NOT EXISTS via ON document TYPE option<string>;
                     DEFINE TABLE IF NOT EXISTS claim_gone SCHEMALESS;",
                )
                .await
                .map(drop)
            })
            .await?;
        Ok(())
    }

    /// A client whose transactions are resent at most `n` times on a conflict (`TX_RETRIES` by
    /// default). 0 turns the retry off: the conflict goes up as the error it is.
    #[must_use]
    pub fn with_tx_retries(mut self, n: u32) -> Self {
        self.tx_retries = n;
        self
    }

    #[must_use]
    pub fn tx_count(&self, place: TxPlace) -> TxCount {
        let c = &self.tx_stats.cells[place.index()];
        TxCount {
            committed: c.committed.load(Ordering::Relaxed),
            conflicts: c.conflicts.load(Ordering::Relaxed),
            retries: c.retries.load(Ordering::Relaxed),
            exhausted: c.exhausted.load(Ordering::Relaxed),
        }
    }

    /// One `BEGIN; body COMMIT;` request. Every statement's answer comes back (BEGIN and COMMIT
    /// included); a conflict is `Attempt::Conflict`, any other failed statement an error.
    pub(super) async fn tx_once(&self, body: &str) -> Result<Attempt<Vec<Value>>> {
        let sql = format!("BEGIN; {body} COMMIT;");
        let request = json!({"id": 1, "method": "query", "params": [sql]});
        let (status, answer) = self.send_authed(&request).await?;
        let reply: RpcReply = serde_json::from_str(&answer)
            .with_context(|| format!("surreal reply (http {status}) is not an rpc reply"))?;
        match (reply.error, reply.result) {
            (Some(e), _) => Err(rejection("surreal: ", &e.message)),
            (None, Some(statements)) => {
                let failed: Vec<String> = statements
                    .iter()
                    .filter(|s| s.status != "OK")
                    .filter(|s| {
                        // After the first failure the server answers every later statement with
                        // a sentence about the aborted transaction; only the first one says why.
                        let said = s.result.as_str().unwrap_or_default();
                        !said.contains("not executed due to a")
                            && !said.contains("Cannot COMMIT: the transaction was aborted")
                    })
                    .map(|s| {
                        s.result
                            .as_str()
                            .map_or_else(|| s.result.to_string(), str::to_owned)
                    })
                    .collect();
                if failed.is_empty() {
                    Ok(Attempt::Done(
                        statements.into_iter().map(|s| s.result).collect(),
                    ))
                } else if failed.iter().any(|m| is_retryable(m)) {
                    Ok(Attempt::Conflict(failed.join(" | ")))
                } else {
                    Err(anyhow!(
                        "surreal transaction failed: {}",
                        failed.join(" | ")
                    ))
                }
            }
            (None, None) => bail!("surreal reply (http {status}) has neither result nor error"),
        }
    }

    /// Run `attempt` until it commits; a conflict is counted and sent again whole, up to
    /// `tx_retries` times, then returned as an error naming the count.
    pub(super) async fn retrying<T, F, Fut>(&self, place: TxPlace, attempt: F) -> Result<T>
    where
        F: Fn() -> Fut,
        Fut: std::future::Future<Output = Result<Attempt<T>>>,
    {
        let cell = &self.tx_stats.cells[place.index()];
        let mut resent = 0_u32;
        loop {
            match attempt().await? {
                Attempt::Done(v) => {
                    cell.committed.fetch_add(1, Ordering::Relaxed);
                    return Ok(v);
                }
                Attempt::Conflict(message) => {
                    cell.conflicts.fetch_add(1, Ordering::Relaxed);
                    if resent >= self.tx_retries {
                        cell.exhausted.fetch_add(1, Ordering::Relaxed);
                        bail!(
                            "surreal {} transaction still in conflict after {resent} resends: {message}",
                            place.name()
                        );
                    }
                    resent += 1;
                    cell.retries.fetch_add(1, Ordering::Relaxed);
                    tokio::time::sleep(backoff(resent)).await;
                }
            }
        }
    }

    /// Write one claim row and seal around it, as `upsert_claim_with_anchor_on` does in pg: the
    /// insert, then the seal of what the new row replaced and the unseal of the slot's latest.
    ///
    /// # Errors
    /// A statement failed, or the slot stayed in conflict for more than `tx_retries` resends.
    pub async fn claim_slot_upsert(&self, w: &ClaimWrite<'_>) -> Result<()> {
        self.ensure_via_field().await?;
        let sql = slot_sql(w);
        self.retrying(TxPlace::Slot, || async {
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// Seal every current claim of `older` under the note `newer` that replaced it, and hand each
    /// fact slot `older` held to its latest live row (`seal_superseded_claims` in pg). Returns the
    /// claims sealed.
    ///
    /// # Errors
    /// A statement failed, or a slot stayed in conflict for more than `tx_retries` resends.
    pub async fn seal_note(&self, newer: &str, older: &str) -> Result<usize> {
        self.ensure_via_field().await?;
        self.retrying(TxPlace::Note, || async {
            let query = format!(
                "SELECT subject, predicate, kind FROM claim_row WHERE source_path = {};",
                text(older)
            );
            let held: Vec<KindRow> =
                super::first_statement(&query, self.rows::<KindRow>(&query).await?)?;
            let planned: Vec<(String, String)> = held
                .iter()
                .map(|r| (r.subject.clone(), r.predicate.clone()))
                .collect();
            let facts: Vec<(String, String)> = held
                .into_iter()
                .filter(|r| r.kind == "fact")
                .map(|r| (r.subject, r.predicate))
                .collect();
            let query = "SELECT subject, predicate FROM claim_row GROUP BY subject, predicate;";
            let every: Vec<PairRow> =
                super::first_statement(query, self.rows::<PairRow>(query).await?)?;
            let every: Vec<(String, String)> = every
                .into_iter()
                .map(|r| (r.subject, r.predicate))
                .collect();
            let sql = note_sql(
                newer,
                older,
                &NotePlan {
                    planned,
                    facts,
                    every,
                },
            );
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(answers) => {
                    let n = answers
                        .iter()
                        .rev()
                        .nth(1)
                        .and_then(Value::as_u64)
                        .context("seal answered no count")?;
                    Attempt::Done(usize::try_from(n)?)
                }
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// `POST /repairs/split-subjects` on Surreal: delete the live claim rows of one group's other
    /// spellings and blank the affected notes' sha, in one transaction. `None` means `subject`
    /// names no live group. The group is read first and checked again inside the transaction: a
    /// row that moved in between throws, and the whole read-plan-write is done again.
    ///
    /// # Errors
    /// A statement failed, or the group stayed in conflict for more than `tx_retries` resends.
    pub async fn merge_split_subject(
        &self,
        subject: &str,
        standing: SplitStanding,
    ) -> Result<Option<Merged>> {
        self.ensure_via_field().await?;
        self.retrying(TxPlace::Split, || self.split_attempt(subject, standing))
            .await
    }

    async fn split_attempt(
        &self,
        subject: &str,
        standing: SplitStanding,
    ) -> Result<Attempt<Option<Merged>>> {
        let query = "SELECT subject, predicate, source_path, valid_from_us FROM claim_row \
                     WHERE superseded_us = NONE;";
        let live: Vec<LiveRow> = super::first_statement(query, self.rows::<LiveRow>(query).await?)?;
        let pairs: Vec<(String, String)> = live
            .iter()
            .map(|r| (r.subject.clone(), r.source_path.clone()))
            .collect();
        match find_group(&pairs, subject) {
            None => Ok(Attempt::Done(None)),
            Some((variants, notes)) => {
                let expected: Vec<String> = live
                    .iter()
                    .filter(|r| variants.contains(&r.subject))
                    .map(|r| claim_key(&r.subject, &r.predicate, r.valid_from_us))
                    .collect();
                let query = format!(
                    "SELECT subject, predicate FROM claim_row WHERE subject INSIDE {};",
                    texts(&variants)
                );
                let planned: Vec<PairRow> =
                    super::first_statement(&query, self.rows::<PairRow>(&query).await?)?;
                let planned: Vec<(String, String)> = planned
                    .into_iter()
                    .map(|r| (r.subject, r.predicate))
                    .collect();
                let sql = split_sql(&variants, &notes, &expected, &planned, standing);
                Ok(match self.tx_once(&sql).await? {
                    Attempt::Done(answers) => {
                        let answer: SplitAnswer = serde_json::from_value(
                            answers
                                .into_iter()
                                .rev()
                                .nth(1)
                                .context("split answered nothing")?,
                        )
                        .context("split answer")?;
                        Attempt::Done(Some(Merged {
                            subject: subject.to_owned(),
                            deleted_rows: answer.deleted,
                            reread_notes: answer.reread,
                            owner_held: answer.owner_held,
                        }))
                    }
                    Attempt::Conflict(m) => Attempt::Conflict(m),
                })
            }
        }
    }

    /// `Store::claim_is_unchanged_on`: does the note's own latest row of this slot already say
    /// exactly this? Read on `claim_row`, sealed rows included, as pg reads `claim`.
    ///
    /// # Errors
    /// The read failed.
    #[allow(clippy::too_many_arguments)]
    pub async fn claim_is_unchanged(
        &self,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        kind: &str,
        confidence: &str,
        anchor: Option<&str>,
    ) -> Result<bool> {
        let query = format!(
            "SELECT * FROM claim_row \
             WHERE subject = {} AND predicate = {} AND source_path = {} \
             ORDER BY valid_from_us DESC LIMIT 1;",
            text(subject),
            text(predicate),
            text(source_path)
        );
        let latest: Vec<LatestClaim> =
            super::first_statement(&query, self.rows::<LatestClaim>(&query).await?)?;
        Ok(latest.first().is_some_and(|l| {
            l.value == value
                && l.kind == kind
                && l.confidence == confidence
                && l.anchor.as_deref() == anchor
        }))
    }

    /// `Store::mirror_claim_said_by_on`: the note's `said_by` on its own latest row of the slot,
    /// changed only when it differs. Inside the slot's guard, so it cannot meet a newer row of
    /// the same note being written.
    ///
    /// # Errors
    /// A statement failed, or the slot stayed in conflict for more than `tx_retries` resends.
    pub async fn claim_said_by(
        &self,
        subject: &str,
        predicate: &str,
        source_path: &str,
        said_by: Option<&str>,
    ) -> Result<()> {
        self.ensure_via_field().await?;
        let (s, p, sp, by) = (
            text(subject),
            text(predicate),
            text(source_path),
            opt(said_by),
        );
        let sql = format!(
            "{guards}
             {BUMP_GUARDS}
             LET $mx = (SELECT VALUE valid_from_us FROM claim_row WHERE subject = {s} AND predicate = {p} AND source_path = {sp} ORDER BY valid_from_us DESC LIMIT 1)[0];
             UPDATE claim_row SET said_by = {by}, via = 'surreal' WHERE subject = {s} AND predicate = {p} AND source_path = {sp} AND valid_from_us = $mx AND said_by != {by};",
            guards = guards_let([(subject, predicate)]),
        );
        self.write_retrying(TxPlace::Slot, &sql).await
    }

    /// `Store::repin_claim`: move the anchor of one live row (not stale) in `claim_row` and in
    /// the vector `claim` table.
    ///
    /// # Errors
    /// A statement failed, or the rows stayed in conflict for more than `tx_retries` resends.
    pub async fn repin_claim(
        &self,
        subject: &str,
        predicate: &str,
        valid_from: SystemTime,
        anchor: &str,
    ) -> Result<()> {
        self.ensure_via_field().await?;
        let cid = text(&claim_key(subject, predicate, epoch_micros(valid_from)?));
        let anchor = text(anchor);
        let sql = format!(
            "UPDATE claim_row SET anchor = {anchor}, via = 'surreal' WHERE cid = {cid} AND stale_us = NONE;
             UPDATE claim SET anchor = {anchor} WHERE cid = {cid} AND stale_us = NONE;"
        );
        self.write_retrying(TxPlace::Slot, &sql).await
    }

    /// `Store::mark_claim_stale`: stamp one live row stale, now, in `claim_row` and in the vector
    /// `claim` table (the embedding stays, so the HNSW index is untouched).
    ///
    /// # Errors
    /// A statement failed, or the rows stayed in conflict for more than `tx_retries` resends.
    pub async fn mark_claim_stale(
        &self,
        subject: &str,
        predicate: &str,
        valid_from: SystemTime,
        reason: &str,
    ) -> Result<()> {
        self.ensure_via_field().await?;
        let cid = text(&claim_key(subject, predicate, epoch_micros(valid_from)?));
        let reason = text(reason);
        let sql = format!(
            "LET $at = time::micros(time::now());
             UPDATE claim_row SET stale_us = $at, stale_reason = {reason}, via = 'surreal' WHERE cid = {cid} AND stale_us = NONE;
             UPDATE claim SET stale_us = $at WHERE cid = {cid} AND stale_us = NONE;"
        );
        self.write_retrying(TxPlace::Slot, &sql).await
    }

    /// `Store::seal_restated_slots`: close `older`'s rows in the slots `newer` re-states and hand
    /// nothing on. Returns the rows closed.
    ///
    /// # Errors
    /// A statement failed, or a slot stayed in conflict for more than `tx_retries` resends.
    pub async fn seal_restated(&self, newer: &str, older: &str) -> Result<usize> {
        self.ensure_via_field().await?;
        self.retrying(TxPlace::Note, || async {
            let query = format!(
                "SELECT subject, predicate FROM claim_row WHERE source_path = {};",
                text(newer)
            );
            let held: Vec<PairRow> =
                super::first_statement(&query, self.rows::<PairRow>(&query).await?)?;
            let planned: Vec<(String, String)> =
                held.into_iter().map(|r| (r.subject, r.predicate)).collect();
            let sql = restated_sql(newer, older, &planned);
            Ok(match self.tx_once(&sql).await? {
                Attempt::Done(answers) => {
                    let n = answers
                        .iter()
                        .rev()
                        .nth(1)
                        .and_then(Value::as_u64)
                        .context("restated seal answered no count")?;
                    Attempt::Done(usize::try_from(n)?)
                }
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }

    /// `Store::note_retired_slots`: the slots of `older` that carry `newer`'s latest `valid_from`
    /// as their seal, in subject, predicate order. A `newer` with no row seals nothing, so the
    /// answer is empty.
    ///
    /// # Errors
    /// A read failed.
    pub async fn retired_slots(&self, newer: &str, older: &str) -> Result<Vec<(String, String)>> {
        let query = format!(
            "SELECT VALUE valid_from_us FROM claim_row WHERE source_path = {} \
             ORDER BY valid_from_us DESC LIMIT 1;",
            text(newer)
        );
        let newest: Vec<i64> = super::first_statement(&query, self.rows::<i64>(&query).await?)?;
        let Some(stamp) = newest.first() else {
            return Ok(Vec::new());
        };
        let query = format!(
            "SELECT subject, predicate FROM claim_row WHERE source_path = {} AND superseded_us = {stamp};",
            text(older)
        );
        let mut rows: Vec<PairRow> =
            super::first_statement(&query, self.rows::<PairRow>(&query).await?)?;
        rows.sort_by(|a, b| {
            en_us_order(&a.subject, &b.subject)
                .then_with(|| en_us_order(&a.predicate, &b.predicate))
        });
        Ok(rows.into_iter().map(|r| (r.subject, r.predicate)).collect())
    }

    /// One write transaction made of `sql` alone, sent again on a conflict.
    async fn write_retrying(&self, place: TxPlace, sql: &str) -> Result<()> {
        self.retrying(place, || async {
            Ok(match self.tx_once(sql).await? {
                Attempt::Done(_) => Attempt::Done(()),
                Attempt::Conflict(m) => Attempt::Conflict(m),
            })
        })
        .await
    }
}

#[derive(Deserialize)]
struct LatestClaim {
    value: String,
    kind: String,
    confidence: String,
    anchor: Option<String>,
}
