//! The owner door. `owner` as an author or a judge is accepted only from a caller presenting
//! the engine's `BORING_OWNER_TOKEN`; a note the owner wrote is superseded or rewritten only by
//! the owner.

use axum::http::HeaderMap;
use deadpool_postgres::Transaction;
use serde_json::json;

use crate::frontmatter::Author;
use crate::store::Store;

pub(crate) const OWNER_TOKEN_HEADER: &str = "x-boring-owner-token";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Caller {
    Owner,
    Unverified,
}

impl Caller {
    pub(crate) fn from_headers(configured: Option<&str>, headers: &HeaderMap) -> Self {
        let presented = headers
            .get(OWNER_TOKEN_HEADER)
            .and_then(|v| v.to_str().ok());
        match (configured, presented) {
            (Some(want), Some(got)) if constant_time_eq(want.as_bytes(), got.as_bytes()) => {
                Self::Owner
            }
            _ => Self::Unverified,
        }
    }
}

/// Whether this request speaks as the owner — decided once, after the token is checked.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Standing {
    Owner,
    NotOwner,
}

pub(crate) fn standing(caller: Caller, claims_owner: bool) -> Result<Standing, String> {
    match (claims_owner, caller) {
        (false, _) => Ok(Standing::NotOwner),
        (true, Caller::Owner) => Ok(Standing::Owner),
        (true, Caller::Unverified) => Err(format!(
            "owner (as author or judge) needs the owner door token in {OWNER_TOKEN_HEADER}"
        )),
    }
}

/// Whether the duplicate gate may skip or fold this write into `existing`: an owner write is
/// gated only by the owner's own notes, never swallowed by one someone else wrote.
pub(crate) fn gated_by(standing: Standing, existing: &Author) -> bool {
    standing == Standing::NotOwner || *existing == Author::Owner
}

/// A note the owner wrote may be superseded by a richer same-session note only from the owner.
pub(crate) fn may_rewrite(standing: Standing, existing: &Author) -> bool {
    standing == Standing::Owner || *existing != Author::Owner
}

/// The owner-written notes among `targets` that this non-owner request may not supersede, each
/// recorded once as `owner_supersede_refused`. Empty for the owner, and without a store — no
/// supersedes edge can be written then, so nothing is replaced.
///
/// Read-only on the request's transaction handle `tx`. The event is not written here: the caller
/// records it with `record_supersede_refusals` once `tx` is released, committed or rolled back.
pub(crate) async fn refused_supersedes(
    tx: &Transaction<'_>,
    standing: Standing,
    targets: &[String],
) -> anyhow::Result<Vec<String>> {
    match standing {
        Standing::NotOwner => Store::owner_authored(tx, targets).await,
        Standing::Owner => Ok(Vec::new()),
    }
}

/// `refused_supersedes` for a request with no transaction of its own (`/remember`): reads on a
/// pooled connection and records the event. Without a store nothing can be replaced.
pub(crate) async fn refused_supersedes_logged(
    store: Option<&Store>,
    standing: Standing,
    targets: &[String],
    door: &str,
) -> anyhow::Result<Vec<String>> {
    match store {
        Some(store) => {
            let mut conn = store.conn().await?;
            let tx = conn.transaction().await?;
            let refused = refused_supersedes(&tx, standing, targets).await?;
            drop(tx);
            drop(conn);
            record_supersede_refusals(Some(store), &refused, door).await;
            Ok(refused)
        }
        None => Ok(Vec::new()),
    }
}

/// One `owner_supersede_refused` event for the refused list, if any. Called after the request's
/// transaction handle is gone; its failure stays a warning.
pub(crate) async fn record_supersede_refusals(
    store: Option<&Store>,
    refused: &[String],
    door: &str,
) {
    if !refused.is_empty() {
        record_refusal(store, "owner_supersede_refused", refused, door).await;
    }
}

/// `/remember` refuses the whole note when it would supersede an owner-written one.
pub(crate) fn none_refused(refused: &[String]) -> Result<(), String> {
    if refused.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "only the owner may supersede an owner-written note: {}",
            refused.join(", ")
        ))
    }
}

pub(crate) async fn record_refusal(
    store: Option<&Store>,
    event: &str,
    targets: &[String],
    door: &str,
) {
    let event = json!({
        "component": "drudge.owner",
        "event": event,
        "status": "warn",
        "door": door,
        "targets": targets,
    });
    match store {
        Some(store) => {
            if let Err(e) = store.log_event(&event).await {
                eprintln!("[owner] refusal event warning (refusal stands): {e:#}");
            }
        }
        None => eprintln!("[owner] {event}"),
    }
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0_u8, |acc, (x, y)| acc | (x ^ y)) == 0
}
