//! Recurrence register: a recent risk/blocked claim whose value restates an older claim from
//! another note is one row — newer claim plus every older match, with distance and days_apart.
//! Read-only over the `claim` table; vectors are pinned constants, no model server in the loop.
//!
//! Needs a Postgres instance reachable via `BORING_TEST_DATABASE_URL`; without it the tests skip
//! (the same guard `store_integration.rs` / `consumption_integration.rs` use).
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::Store;

fn test_dsn() -> Option<String> {
    std::env::var("BORING_TEST_DATABASE_URL").ok()
}

fn unique_path(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("/vault/wiki/{prefix}-{ts}.md")
}

fn unique_project(prefix: &str) -> String {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("{prefix}-{ts}")
}

/// Basis vector: 1.0 at `one_at`, 0 elsewhere.
fn unit_vec(dim: usize, one_at: usize) -> Vec<f32> {
    let mut v = vec![0.0; dim];
    v[one_at] = 1.0;
    v
}

/// Unit vector whose cosine distance from `unit_vec(dim, one_at)` is exactly `1.0 - cos`:
/// `cos = 0.9` → 0.1 (a recurrence), `cos = 0.1` → 0.9 (a control that must not match).
fn tilted_vec(dim: usize, one_at: usize, tilt_at: usize, cos: f32) -> Vec<f32> {
    let mut v = vec![0.0; dim];
    v[one_at] = cos;
    v[tilt_at] = (1.0 - cos * cos).sqrt();
    v
}

async fn seed_note(store: &Store, path: &str, project: &str) {
    let front = FrontMatter {
        origin: "personal".to_string(),
        project: project.to_string(),
        kind: "note".to_string(),
        source_path: path.to_string(),
        title: Some("recurrence fixture".to_string()),
        tags: vec!["test".to_string()],
        ..Default::default()
    };
    store
        .upsert_document(&front, "sha-v1", SystemTime::now())
        .await
        .expect("upsert doc");
}

#[allow(clippy::too_many_arguments)]
async fn seed_claim(
    store: &Store,
    path: &str,
    subject: &str,
    predicate: &str,
    value: &str,
    kind: &str,
    valid_from: SystemTime,
    embedding: Vec<f32>,
) {
    store
        .upsert_claim(
            subject, predicate, value, path, valid_from, &embedding, kind, "certain",
        )
        .await
        .expect("upsert claim");
}

#[tokio::test]
async fn recurrence_pair_is_one_row_with_oldest_first() {
    let Some(dsn) = test_dsn() else { return };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let project = unique_project("rec");
    let path_a = unique_path("rec-a");
    let path_b = unique_path("rec-b");
    let path_k = unique_path("rec-k");
    seed_note(&store, &path_a, &project).await;
    seed_note(&store, &path_b, &project).await;
    seed_note(&store, &path_k, &project).await;
    let now = SystemTime::now();
    let day = Duration::from_hours(24);
    let value_a = "Orca CLI cannot connect to the running app";
    let value_b = "orca orchestration cannot connect because Orca is not running";
    let value_k = "the CLI cannot reach the running Orca application";
    let subject = unique_project("orca-cli");
    // Newer (now-5d) vs two older claims (now-10d at distance ~0.1, now-15d at ~0.15).
    seed_claim(
        &store,
        &path_a,
        &subject,
        "observed",
        value_a,
        "risk",
        now - day * 5,
        unit_vec(1024, 0),
    )
    .await;
    seed_claim(
        &store,
        &path_b,
        &subject,
        "symptom",
        value_b,
        "blocked",
        now - day * 10,
        tilted_vec(1024, 0, 1, 0.9),
    )
    .await;
    seed_claim(
        &store,
        &path_k,
        &subject,
        "symptom",
        value_k,
        "risk",
        now - day * 15,
        tilted_vec(1024, 0, 2, 0.85),
    )
    .await;

    let rows = store
        .recurrences(30, Some(&project), 10)
        .await
        .expect("recurrences");
    assert_eq!(rows.len(), 1, "one newer claim, one row");
    let row = &rows[0];
    assert_eq!(row.newer.source_path, path_a);
    assert_eq!(row.newer.subject, subject);
    assert_eq!(row.newer.predicate, "observed");
    assert_eq!(row.newer.value, value_a);
    assert_eq!(row.newer.kind, "risk");
    assert_eq!(row.older.len(), 2, "both older claims gather into the row");
    assert_eq!(row.older[0].source_path, path_k, "oldest first");
    assert_eq!(row.older[1].source_path, path_b);
    assert!(
        (0.05..=0.15).contains(&row.distance),
        "distance {}",
        row.distance
    );
    assert_eq!(row.days_apart, 5);
    assert!(!row.label_only);

    // The project filter rides the document join.
    assert!(
        store
            .recurrences(30, Some("elsewhere"), 10)
            .await
            .expect("other project")
            .is_empty()
    );
}

/// Two newer claims whose pairs interleave in the nearest-first order (X 0.05, Y 0.10,
/// X 0.15, Y 0.18). Grouping by "same as the previous row" split each into two rows — live,
/// 6 newers came out as 16. The register promises one row per newer claim.
#[tokio::test]
async fn interleaved_pairs_still_gather_one_row_per_newer_claim() {
    let Some(dsn) = test_dsn() else { return };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let project = unique_project("rec-mix");
    let now = SystemTime::now();
    let day = Duration::from_hours(24);
    let path_x = unique_path("mix-x");
    let path_y = unique_path("mix-y");
    let olders = [
        (unique_path("mix-x1"), 0usize, 1usize, 0.95f32, 8u32),
        // Olders of one newer must stay > 0.2 from each other (0.95·0.80 = 0.76 → 0.24) or
        // they pair with each other and the fixture answers a different question.
        (unique_path("mix-x2"), 0, 2, 0.80, 12),
        (unique_path("mix-y1"), 3, 4, 0.90, 9),
        (unique_path("mix-y2"), 3, 5, 0.82, 14),
    ];
    for p in [&path_x, &path_y] {
        seed_note(&store, p, &project).await;
    }
    for (p, _, _, _, _) in &olders {
        seed_note(&store, p, &project).await;
    }
    let subject_x = unique_project("mix-subj-x");
    let subject_y = unique_project("mix-subj-y");
    seed_claim(
        &store,
        &path_x,
        &subject_x,
        "observed",
        "the x trouble came back this week again",
        "risk",
        now - day * 2,
        unit_vec(1024, 0),
    )
    .await;
    seed_claim(
        &store,
        &path_y,
        &subject_y,
        "observed",
        "the y trouble came back this week again",
        "risk",
        now - day * 2,
        unit_vec(1024, 3),
    )
    .await;
    for (p, one_at, tilt_at, cos, days_ago) in &olders {
        let subject = if *one_at == 0 { &subject_x } else { &subject_y };
        seed_claim(
            &store,
            p,
            subject,
            "symptom",
            "an older sighting of the same trouble",
            "risk",
            now - day * *days_ago,
            tilted_vec(1024, *one_at, *tilt_at, *cos),
        )
        .await;
    }

    let rows = store
        .recurrences(30, Some(&project), 10)
        .await
        .expect("recurrences");
    assert_eq!(
        rows.len(),
        2,
        "one row per newer claim, however the pairs interleave"
    );
    for row in &rows {
        assert_eq!(
            row.older.len(),
            2,
            "both olders gather under {}",
            row.newer.source_path
        );
    }
    assert_eq!(
        rows[0].newer.source_path, path_x,
        "nearest pair first (x at 0.05)"
    );
    assert!(
        rows[0].distance < rows[1].distance,
        "row distance is the group's smallest"
    );
}

#[tokio::test]
async fn same_day_pair_is_not_a_recurrence() {
    // Control 1: near-identical values, zero-day gap — the same write restated, not a repeat.
    let Some(dsn) = test_dsn() else { return };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let project = unique_project("rec-sameday");
    let path_c = unique_path("rec-c");
    let path_d = unique_path("rec-d");
    seed_note(&store, &path_c, &project).await;
    seed_note(&store, &path_d, &project).await;
    let now = SystemTime::now();
    let day = Duration::from_hours(24);
    let subject = unique_project("sameday");
    let value = "the deployment pipeline failed with a sealed schema error";
    let same_day = now - day * 4;
    seed_claim(
        &store,
        &path_c,
        &subject,
        "observed",
        value,
        "risk",
        same_day,
        unit_vec(1024, 10),
    )
    .await;
    seed_claim(
        &store,
        &path_d,
        &subject,
        "symptom",
        value,
        "blocked",
        same_day,
        tilted_vec(1024, 10, 11, 0.95),
    )
    .await;

    assert!(
        store
            .recurrences(30, Some(&project), 10)
            .await
            .expect("recurrences")
            .is_empty()
    );
}

#[tokio::test]
async fn far_pair_is_not_a_recurrence() {
    // Control 2: proper 5-day gap, but vectors ~0.9 apart — a different incident.
    let Some(dsn) = test_dsn() else { return };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let project = unique_project("rec-far");
    let path_e = unique_path("rec-e");
    let path_f = unique_path("rec-f");
    seed_note(&store, &path_e, &project).await;
    seed_note(&store, &path_f, &project).await;
    let now = SystemTime::now();
    let day = Duration::from_hours(24);
    let subject = unique_project("far");
    seed_claim(
        &store,
        &path_e,
        &subject,
        "observed",
        "the dashboard exporter lost its database connection overnight",
        "risk",
        now - day * 5,
        unit_vec(1024, 20),
    )
    .await;
    seed_claim(
        &store,
        &path_f,
        &subject,
        "symptom",
        "the weekly report generator ran out of disk space",
        "blocked",
        now - day * 10,
        tilted_vec(1024, 20, 21, 0.1),
    )
    .await;

    assert!(
        store
            .recurrences(30, Some(&project), 10)
            .await
            .expect("recurrences")
            .is_empty()
    );
}

#[tokio::test]
async fn decision_kind_is_not_in_the_register() {
    // Control 3: close vectors and a real gap, but kind=decision — the register is risk/blocked only.
    let Some(dsn) = test_dsn() else { return };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let project = unique_project("rec-decision");
    let path_g = unique_path("rec-g");
    let path_h = unique_path("rec-h");
    seed_note(&store, &path_g, &project).await;
    seed_note(&store, &path_h, &project).await;
    let now = SystemTime::now();
    let day = Duration::from_hours(24);
    let subject = unique_project("decision");
    seed_claim(
        &store,
        &path_g,
        &subject,
        "settled",
        "we will pin the dependency to the last known good version",
        "decision",
        now - day * 5,
        unit_vec(1024, 30),
    )
    .await;
    seed_claim(
        &store,
        &path_h,
        &subject,
        "settled",
        "we decided to pin the dependency to the last good version",
        "decision",
        now - day * 10,
        tilted_vec(1024, 30, 31, 0.9),
    )
    .await;

    assert!(
        store
            .recurrences(30, Some(&project), 10)
            .await
            .expect("recurrences")
            .is_empty()
    );
}

#[tokio::test]
async fn tautological_predicate_pair_is_label_only() {
    // The predicate cannot filter the pair out, so the row answers flagged label_only.
    let Some(dsn) = test_dsn() else { return };
    let store = Store::open(&dsn, 1024).await.expect("open store");
    let project = unique_project("rec-label");
    let path_i = unique_path("rec-i");
    let path_j = unique_path("rec-j");
    seed_note(&store, &path_i, &project).await;
    seed_note(&store, &path_j, &project).await;
    let now = SystemTime::now();
    let day = Duration::from_hours(24);
    let subject = unique_project("labelonly");
    seed_claim(
        &store,
        &path_i,
        &subject,
        "incident",
        "the nightly batch job silently skipped half of its input files",
        "risk",
        now - day * 5,
        unit_vec(1024, 40),
    )
    .await;
    seed_claim(
        &store,
        &path_j,
        &subject,
        "incident",
        "the nightly batch job silently skipped most input files again",
        "risk",
        now - day * 12,
        tilted_vec(1024, 40, 41, 0.9),
    )
    .await;

    let rows = store
        .recurrences(30, Some(&project), 10)
        .await
        .expect("recurrences");
    assert_eq!(rows.len(), 1);
    let row = &rows[0];
    assert!(
        row.label_only,
        "predicate restates its kind — flagged, not dropped"
    );
    assert_eq!(row.days_apart, 7);
    assert_eq!(row.newer.source_path, path_i);
    assert_eq!(row.older.len(), 1);
    assert_eq!(row.older[0].source_path, path_j);
}
