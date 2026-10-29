//! `claim_priors` splits a note's current claims into Prior (an earlier sealed row of the same
//! slot exists) and Absent (none, or the earlier row was never sealed) — contract C-CARDKB1.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the test skips.
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::claim_priors::{Prior, Sealed};
use drudge::store::Store;
use tokio_postgres::NoTls;

fn at(secs: u64) -> SystemTime {
    UNIX_EPOCH + Duration::from_secs(secs)
}

#[tokio::test]
async fn a_sealed_earlier_row_is_a_prior_and_everything_else_is_absent() {
    let Ok(dsn) = std::env::var("BORING_TEST_DATABASE_URL") else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let (db, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    let tag = std::time::SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let old = format!("/vault/wiki/prior-{tag}-old.md");
    let new = format!("/vault/wiki/prior-{tag}-new.md");
    let (sealed, fresh, unsealed) = (
        format!("sealed-{tag}"),
        format!("fresh-{tag}"),
        format!("unsealed-{tag}"),
    );
    let z = [0.0_f32; 1024];
    for (subject, value, path, when) in [
        (&sealed, "old-sealed", &old, 1_000),
        (&unsealed, "old-unsealed", &old, 1_000),
        (&sealed, "new-sealed", &new, 2_000),
        (&fresh, "new-fresh", &new, 2_000),
        (&unsealed, "new-unsealed", &new, 2_000),
    ] {
        store
            .upsert_claim(
                subject,
                "state",
                value,
                path,
                at(when),
                &z,
                "fact",
                "certain",
            )
            .await
            .expect("upsert claim");
    }
    // pin the seal state the test names, whatever the writer did on its own
    db.execute(
        "UPDATE claim SET superseded_at = to_timestamp(2000) WHERE source_path = $1 AND subject = $2",
        &[&old, &sealed],
    )
    .await
    .expect("seal");
    db.execute(
        "UPDATE claim SET superseded_at = NULL WHERE source_path = $1 AND subject = $2",
        &[&old, &unsealed],
    )
    .await
    .expect("unseal");

    let got = store.claim_priors(&new).await.expect("claim priors");
    let by = |subject: &String| got.iter().find(|c| &c.subject == subject).expect("claim");
    assert_eq!(got.len(), 3);
    assert_eq!(
        by(&sealed).prior,
        Prior::Prior(Sealed {
            value: "old-sealed".to_string(),
            note: old.clone(),
            superseded_at: "1970-01-01T00:33:20Z".to_string(),
        })
    );
    assert_eq!(by(&fresh).prior, Prior::Absent);
    assert_eq!(by(&unsealed).prior, Prior::Absent);
}
