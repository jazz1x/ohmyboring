//! `POST /repairs/split-subjects/reread` — what happens after a split-subjects merge has
//! committed: the engine rereads the vault (`do_sync`), recounts the spellings that are left and
//! says what the merge's card row should show. Moved from the Python door (`_reread_outcomes`,
//! `_record_merged`); the door keeps the Slack row and renders the outcome.
//!
//! The decision is a pure fold over what the reread and the recount returned; the queue (one
//! reread at a time, merges landing meanwhile share the next one) lives in `serve::http`.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// One committed merge asking to be reread. Counts are `None` for a merge whose engine answer the
/// door never saw: it is reread all the same, but no `subject_merged` event states counts nobody saw.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Merge {
    pub subject: String,
    pub deleted_rows: Option<u64>,
    pub reread_notes: Option<u64>,
    #[serde(default)]
    pub owner_held: Vec<String>,
}

/// What the card row of one merge settles to. `error` of `Failed` is the JSON the failed `/sync`
/// would have answered (`{"error": ...}`), which the door formats the way it always did.
#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum Outcome {
    Done { remaining_variants: usize },
    Pending,
    Failed { error: Value },
}

/// Distinct spellings that still normalize to `target`.
pub fn count_variants(rows: &[(String, String)], target: &str) -> usize {
    rows.iter()
        .filter(|(spelling, _)| crate::ingest::canon(spelling) == target)
        .map(|(spelling, _)| spelling.as_str())
        .collect::<BTreeSet<_>>()
        .len()
}

/// One outcome per merge in `batch`, all from the one reread that covered them. A failed reread
/// or a failed recount fails every row; otherwise each merge is counted against the fresh rows.
pub fn decide(
    batch: &[Merge],
    reread: &Result<(), Value>,
    recount: Result<Vec<(String, String)>, Value>,
) -> Vec<Outcome> {
    let rows = match (reread, recount) {
        (Err(error), _) => return failed_all(batch, error),
        (Ok(()), Err(error)) => return failed_all(batch, &error),
        (Ok(()), Ok(rows)) => rows,
    };
    batch
        .iter()
        .map(|merge| match (merge.deleted_rows, merge.reread_notes) {
            (Some(_), Some(_)) => Outcome::Done {
                remaining_variants: count_variants(&rows, &merge.subject),
            },
            _ => Outcome::Pending,
        })
        .collect()
}

fn failed_all(batch: &[Merge], error: &Value) -> Vec<Outcome> {
    batch
        .iter()
        .map(|_| Outcome::Failed {
            error: error.clone(),
        })
        .collect()
}

/// The `subject_merged` event of a merge that was counted, same fields the door wrote.
pub fn merged_event(merge: &Merge, remaining_variants: usize, ts: &str) -> Option<Value> {
    let (deleted_rows, reread_notes) = (merge.deleted_rows?, merge.reread_notes?);
    Some(serde_json::json!({
        "ts": ts,
        "component": "door",
        "event": "subject_merged",
        "status": "ok",
        "subject": merge.subject,
        "deleted_rows": deleted_rows,
        "reread_notes": reread_notes,
        "remaining_variants": remaining_variants,
        "owner_held": merge.owner_held,
    }))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    fn merge(subject: &str, counts: Option<(u64, u64)>) -> Merge {
        Merge {
            subject: subject.to_owned(),
            deleted_rows: counts.map(|c| c.0),
            reread_notes: counts.map(|c| c.1),
            owner_held: vec![],
        }
    }

    fn rows(spellings: &[&str]) -> Vec<(String, String)> {
        spellings
            .iter()
            .map(|s| ((*s).to_owned(), "/a.md".to_owned()))
            .collect()
    }

    #[test]
    fn a_failed_reread_fails_every_row_and_skips_the_recount() {
        let batch = [merge("foo-bar", Some((2, 2))), merge("x", None)];
        let error = json!({"error": "ingest failed"});
        let out = decide(&batch, &Err(error.clone()), Ok(rows(&[])));
        assert_eq!(
            out,
            vec![
                Outcome::Failed {
                    error: error.clone()
                },
                Outcome::Failed { error }
            ]
        );
    }

    #[test]
    fn a_failed_recount_fails_every_row() {
        let error = json!("recount: connection refused");
        let out = decide(
            &[merge("foo-bar", Some((1, 1)))],
            &Ok(()),
            Err(error.clone()),
        );
        assert_eq!(out, vec![Outcome::Failed { error }]);
    }

    #[test]
    fn a_counted_merge_is_done_and_an_unseen_one_is_pending() {
        let batch = [merge("foo-bar", Some((2, 2))), merge("zzz", None)];
        let out = decide(&batch, &Ok(()), Ok(rows(&["foo-bar", "Foo Bar", "other"])));
        assert_eq!(
            out,
            vec![
                Outcome::Done {
                    remaining_variants: 2
                },
                Outcome::Pending
            ]
        );
    }

    #[test]
    fn the_event_exists_only_for_a_counted_merge() {
        assert!(merged_event(&merge("a", None), 0, "t").is_none());
        let event = merged_event(&merge("a", Some((3, 2))), 1, "t").unwrap();
        assert_eq!(event["deleted_rows"], 3);
        assert_eq!(event["event"], "subject_merged");
    }
}
