//! `GET /claim-priors?source=<note path>` — what each current claim of a note replaced.
//! Read-only: for every current claim of the note, the newest earlier row of the same
//! (subject, predicate) that was sealed. A row never sealed is not a prior, so it reads Absent.
//! No judgment and no ranking beyond the order below.

use chrono::{DateTime, Utc};
use serde::Serialize;

/// Order: newest `valid_from` first, then subject/predicate so equal timestamps stay stable.
pub const SQL: &str = "select c.subject, c.predicate, c.value, p.value, p.source_path, p.superseded_at \
    from claim c \
    left join lateral ( \
        select q.value, q.source_path, q.superseded_at from claim q \
        where q.subject = c.subject and q.predicate = c.predicate \
          and q.valid_from < c.valid_from and q.superseded_at is not null \
        order by q.valid_from desc limit 1 \
    ) p on true \
    where c.source_path = $1 and c.superseded_at is null \
    order by c.valid_from desc, c.subject, c.predicate";

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(tag = "state", content = "prior", rename_all = "lowercase")]
pub enum Prior {
    Prior(Sealed),
    Absent,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct Sealed {
    pub value: String,
    pub note: String,
    pub superseded_at: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ClaimPrior {
    pub subject: String,
    pub predicate: String,
    pub value: String,
    #[serde(flatten)]
    pub prior: Prior,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ClaimPriors {
    pub source: String,
    pub claims: Vec<ClaimPrior>,
}

/// The prior columns arrive together or not at all (a left join): all three present → Prior.
pub fn prior_of(
    value: Option<String>,
    note: Option<String>,
    sealed: Option<DateTime<Utc>>,
) -> Prior {
    match (value, note, sealed) {
        (Some(value), Some(note), Some(at)) => Prior::Prior(Sealed {
            value,
            note,
            superseded_at: at.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        }),
        _ => Prior::Absent,
    }
}
