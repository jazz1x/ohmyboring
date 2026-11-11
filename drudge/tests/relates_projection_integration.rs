//! `vault::project_links` visits only the notes a change can reach, and writes what a full pass
//! would write. Needs a scratch Postgres via `BORING_TEST_DATABASE_URL`:
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/<scratch> \`
//!   `  cargo test -p drudge --test relates_projection_integration -- --nocapture`
//! The tests share one database's projection state, so each holds `SERIAL` for its whole run.
#![allow(clippy::expect_used, clippy::unwrap_used, clippy::await_holding_lock)]

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store};
use drudge::vault::project_links;
use tempfile::TempDir;
use tokio_postgres::NoTls;

const DAY: Duration = Duration::from_hours(24);

/// `relates_projection`, its watermark and the dirty query span the whole database, so two
/// tests running at once reset or consume each other's state. Each `#[tokio::test]` runs on its
/// own thread and current-thread runtime, so a blocking std lock held across `.await` only
/// parks the other tests' threads.
static SERIAL: Mutex<()> = Mutex::new(());

struct Fixture {
    store: Store,
    vault: TempDir,
    nonce: u64,
    _serial: MutexGuard<'static, ()>,
}

impl Fixture {
    /// Fresh vault + a projection state that never ran (no rows, no watermark).
    async fn new() -> Option<Self> {
        let dsn = std::env::var("BORING_TEST_DATABASE_URL").ok()?;
        // A test that panicked while holding the lock left nothing to undo: the next one resets.
        let serial = SERIAL.lock().unwrap_or_else(PoisonError::into_inner);
        let store = Store::open(&dsn, 1024).await.expect("open store");
        let (client, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
        tokio::spawn(conn);
        client
            .batch_execute(
                "DELETE FROM relates_projection;
                 DELETE FROM relates_projection_bound;
                 DELETE FROM sync_state WHERE key = 'relates_projection_watermark';",
            )
            .await
            .expect("reset projection state");
        let vault = TempDir::new().expect("tempdir");
        std::fs::create_dir(vault.path().join("wiki")).expect("wiki dir");
        let nonce = u64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        )
        .unwrap();
        Some(Self {
            store,
            vault,
            nonce,
            _serial: serial,
        })
    }

    fn path(&self, id: u32) -> PathBuf {
        self.vault
            .path()
            .join("wiki")
            .join(format!("wiki-{id:04}.md"))
    }

    /// Pseudo-random +-1 vector: two different seeds sit at cosine distance ~1, the same seed at 0.
    fn embedding(&self, seed: u64) -> Vec<f32> {
        let mut state = (self.nonce ^ seed.wrapping_mul(0x9E37_79B9_7F4A_7C15)) | 1;
        (0..1024)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                if state & 1 == 0 { 1.0 } else { -1.0 }
            })
            .collect()
    }

    /// Write the note file and its document, chunk and concept edges.
    async fn put_note(
        &self,
        id: u32,
        project: &str,
        concepts: &[&str],
        vector_seed: u64,
        age: Duration,
    ) {
        self.put_note_vec(id, project, concepts, self.embedding(vector_seed), age)
            .await;
    }

    async fn put_note_vec(
        &self,
        id: u32,
        project: &str,
        concepts: &[&str],
        embedding: Vec<f32>,
        age: Duration,
    ) {
        let path = self.path(id);
        let src = path.to_string_lossy().into_owned();
        std::fs::write(
            &path,
            format!(
                "---\norigin: personal\nproject: {project}\nkind: note\n---\nbody of note {id}\n"
            ),
        )
        .expect("write note");
        let front = FrontMatter {
            origin: "personal".into(),
            project: project.into(),
            kind: "note".into(),
            source_path: src.clone(),
            ..Default::default()
        };
        self.store
            .upsert_document(&front, "sha", SystemTime::now() - age)
            .await
            .expect("upsert document");
        self.store
            .upsert_chunk(&Doc {
                id: format!("{src}#0"),
                content: format!("body of note {id}"),
                embedding,
                front,
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
        self.set_concepts(id, concepts).await;
    }

    async fn set_concepts(&self, id: u32, concepts: &[&str]) {
        let src = self.path(id).to_string_lossy().into_owned();
        self.store
            .clear_semantic_edges(&src)
            .await
            .expect("clear edges");
        for c in concepts {
            let slug = format!("{c}-{}", self.nonce);
            self.store.upsert_concept(&slug, c).await.expect("concept");
            self.store
                .relate_doc_concept(&src, &slug)
                .await
                .expect("edge");
        }
    }

    /// `groups` groups of 4 old notes; the notes of a group share two concepts and a project.
    async fn put_groups(&self, groups: u32) {
        for g in 0..groups {
            for k in 1..=4 {
                let id = 100 * (g + 1) + k;
                self.put_note(
                    id,
                    &format!("proj-{g}"),
                    &[&format!("g{g}a"), &format!("g{g}b")],
                    u64::from(id),
                    DAY,
                )
                .await;
            }
        }
    }

    async fn project(&self, force_full: bool) -> usize {
        project_links(&self.store, self.vault.path(), 6, force_full)
            .await
            .expect("project_links")
    }

    fn contents(&self) -> BTreeMap<String, String> {
        std::fs::read_dir(self.vault.path().join("wiki"))
            .unwrap()
            .map(|e| e.unwrap().path())
            .map(|p| {
                let name = p.file_name().unwrap().to_string_lossy().into_owned();
                (name, std::fs::read_to_string(&p).unwrap())
            })
            .collect()
    }

    fn restore(&self, snapshot: &BTreeMap<String, String>) {
        for (name, text) in snapshot {
            std::fs::write(self.vault.path().join("wiki").join(name), text).unwrap();
        }
    }
}

fn relates_to_of(text: &str) -> &str {
    text.split("relates_to:")
        .nth(1)
        .unwrap_or("")
        .split("\n---")
        .next()
        .unwrap_or("")
}

fn set_mode(path: &Path, mode: u32) {
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(mode)).unwrap();
}

/// A changed-only run must not open a file outside the dirty set: the files of two untouched
/// groups are made unreadable, so any read of them fails the run.
#[tokio::test]
async fn changed_run_opens_no_file_outside_the_dirty_set() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    fx.put_groups(3).await;
    assert!(
        fx.project(false).await > 0,
        "first run populates every note"
    );

    fx.put_note(900, "proj-0", &["g0a", "g0b"], 900, Duration::ZERO)
        .await;
    let untouched: Vec<PathBuf> = [201, 202, 203, 204, 301, 302, 303, 304]
        .iter()
        .map(|id| fx.path(*id))
        .collect();
    for p in &untouched {
        set_mode(p, 0o000);
    }
    let updated = project_links(&fx.store, fx.vault.path(), 6, false).await;
    for p in &untouched {
        set_mode(p, 0o644);
    }
    let updated = updated.expect("changed-only run must not open untouched groups");
    assert!(updated >= 1, "the new note's neighbours get its link");
    assert!(relates_to_of(&std::fs::read_to_string(fx.path(101)).unwrap()).contains("wiki-0900"));
}

/// A NEW note pointing at an UNTOUCHED old note: the old note's `relates_to` after the
/// changed-only run is exactly what a forced full pass writes.
#[tokio::test]
async fn new_note_reaches_untouched_note_like_a_full_pass() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    fx.put_groups(3).await;
    fx.project(false).await;

    // Old file mtime (restored / `rsync -a` copy): only `ingested_at` says the note is new.
    fx.put_note(900, "proj-1", &["g1a", "g1b"], 900, DAY).await;
    fx.project(false).await;
    let after_changed = fx.contents();
    let old_note = relates_to_of(&after_changed["wiki-0201.md"]).to_owned();
    assert!(
        old_note.contains("wiki-0900"),
        "untouched note 201 now links the new note"
    );

    let rewritten_by_full = fx.project(true).await;
    assert_eq!(
        rewritten_by_full, 0,
        "a full pass finds nothing left to rewrite"
    );
    assert_eq!(fx.contents(), after_changed);
}

/// Same input change, two strategies: the files rewritten and their contents match.
#[tokio::test]
async fn changed_only_and_full_rewrite_the_same_files() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    fx.put_groups(10).await;
    fx.project(false).await;

    // new note on an untouched group, new note near an untouched note only by meaning,
    // an edited note that moves group, a deleted note
    fx.put_note(900, "proj-2", &["g2a", "g2b"], 900, Duration::ZERO)
        .await;
    fx.put_note(901, "proj-solo", &["unrelated"], 501, Duration::ZERO)
        .await;
    fx.set_concepts(401, &["g5a", "g5b"]).await;
    fx.store
        .upsert_document(
            &FrontMatter {
                origin: "personal".into(),
                project: "proj-3".into(),
                kind: "note".into(),
                source_path: fx.path(401).to_string_lossy().into_owned(),
                ..Default::default()
            },
            "sha2",
            SystemTime::now(),
        )
        .await
        .unwrap();
    let deleted = fx.path(701);
    fx.store
        .delete_document(&deleted.to_string_lossy())
        .await
        .unwrap();
    std::fs::remove_file(&deleted).unwrap();
    let before = fx.contents();

    let changed_count = fx.project(false).await;
    let by_changed = fx.contents();
    fx.restore(&before);
    let full_count = fx.project(true).await;
    let by_full = fx.contents();

    let rewritten = |after: &BTreeMap<String, String>| -> Vec<String> {
        after
            .iter()
            .filter(|(k, v)| before.get(*k) != Some(*v))
            .map(|(k, _)| k.clone())
            .collect()
    };
    let (set_changed, set_full) = (rewritten(&by_changed), rewritten(&by_full));
    eprintln!(
        "copy-vault: notes={} changed-only rewrote {} (returned {changed_count}), full rewrote {} (returned {full_count}); set diff = {:?}",
        before.len(),
        set_changed.len(),
        set_full.len(),
        set_changed
            .iter()
            .filter(|f| !set_full.contains(f))
            .chain(set_full.iter().filter(|f| !set_changed.contains(f)))
            .collect::<Vec<_>>()
    );
    assert_eq!(set_changed, set_full);
    assert_eq!(by_changed, by_full);
    assert!(
        by_changed["wiki-0501.md"].contains("wiki-0901"),
        "meaning-only neighbour reached"
    );
}

struct ZeroEmbed;

impl drudge::ingest::Embed for ZeroEmbed {
    fn embed(
        &self,
        _text: &str,
    ) -> impl std::future::Future<Output = anyhow::Result<Vec<f32>>> + Send {
        std::future::ready(Ok(vec![0.0_f32; 1024]))
    }
}

/// Paths under `vault` whose `document.ingested_at` is later than `since`, i.e. the rows the
/// projection's dirty query would take as changed.
async fn ingested_after(
    client: &tokio_postgres::Client,
    vault: &Path,
    since: SystemTime,
) -> Vec<String> {
    let like = format!("{}/%", vault.to_string_lossy());
    let mut paths: Vec<String> = client
        .query(
            "SELECT source_path FROM document WHERE source_path LIKE $1 AND ingested_at > $2;",
            &[&like, &since],
        )
        .await
        .expect("query ingested_at")
        .iter()
        .map(|r| r.get::<_, String>(0))
        .collect();
    paths.sort();
    paths
}

/// A periodic sync of an untouched vault must not mark any document as re-ingested; one edited
/// note marks exactly itself. Otherwise every run's dirty set is the whole corpus.
#[tokio::test]
async fn sync_without_changes_leaves_ingested_at_alone() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let dsn = std::env::var("BORING_TEST_DATABASE_URL").unwrap();
    let (client, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
    tokio::spawn(conn);
    let note = |id: u32, body: &str| {
        let text = format!(
            "---\norigin: personal\nproject: p{nonce}\nkind: note\nclaims:\n  - subject: s{nonce}x{id}\n    predicate: axis\n    value: v1\n    kind: fact\n    confidence: certain\n---\n\n{body}\n",
            nonce = fx.nonce
        );
        std::fs::write(fx.path(id), text).expect("write note");
    };
    for id in 1..=3 {
        note(id, &format!("body {id}"));
    }
    let dirs = vec![fx.vault.path().to_string_lossy().into_owned()];
    let cfg = drudge::config::BoringConfig::default();
    let sync = || {
        drudge::ingest::run_with::<
            drudge::ingest::DefaultChunker,
            drudge::ingest::FrontmatterGraphExtractor,
            _,
        >(&fx.store, &ZeroEmbed, &cfg, &dirs)
    };
    sync().await.expect("first sync");

    let mark: SystemTime = client
        .query_one("SELECT clock_timestamp();", &[])
        .await
        .expect("clock")
        .get(0);
    sync().await.expect("second sync");
    let untouched = ingested_after(&client, fx.vault.path(), mark).await;
    assert_eq!(
        untouched,
        Vec::<String>::new(),
        "no-change sync re-ingested documents"
    );

    note(2, "body 2 edited");
    let mark: SystemTime = client
        .query_one("SELECT clock_timestamp();", &[])
        .await
        .expect("clock")
        .get(0);
    sync().await.expect("third sync");
    let edited = ingested_after(&client, fx.vault.path(), mark).await;
    assert_eq!(edited, vec![fx.path(2).to_string_lossy().into_owned()]);
}

/// A changed note that lands BEHIND a note's k-th neighbour (4 semantic, 6 graph) cannot change
/// that note's links, so it must not make the note dirty; one that lands in a note with fewer
/// than k neighbours must. Either way the changed-only run equals a full pass.
#[tokio::test]
async fn changed_note_behind_the_kth_neighbour_does_not_dirty_the_note() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    // Semantic: five notes on one vector (4 neighbours each, all at distance 0).
    for id in 101..=105 {
        fx.put_note(id, "proj-sem", &[], 7, DAY).await;
    }
    // Graph: seven notes sharing three concepts (6 neighbours each, 3 shared).
    for id in 121..=127 {
        fx.put_note(id, "proj-graph", &["k1", "k2", "k3"], u64::from(id), DAY)
            .await;
    }
    // Semantic with fewer than 4 neighbours: a pair on one vector.
    for id in 111..=112 {
        fx.put_note(id, "proj-pair", &[], 9, DAY).await;
    }
    fx.project(false).await;

    let nearby = |seed: u64| {
        let mut v = fx.embedding(seed);
        for x in v.iter_mut().take(100) {
            *x = -*x; // 100 of 1024 signs flipped: cosine distance ~0.195 < 0.40
        }
        v
    };
    fx.put_note_vec(900, "proj-x1", &[], nearby(7), Duration::ZERO)
        .await;
    fx.put_note_vec(901, "proj-x2", &[], nearby(9), Duration::ZERO)
        .await;
    fx.put_note(902, "proj-x3", &["k1", "k2"], 902, Duration::ZERO)
        .await;

    let scope = fx.store.projection_scope(false).await.expect("scope");
    let dirty = match scope {
        drudge::store::ProjectionScope::Changed(dirty) => Ok(dirty),
        full @ drudge::store::ProjectionScope::Full(_) => Err(full),
    }
    .expect("a populated projection with a watermark scopes to a changed set");
    let dirty_ids: Vec<u32> = dirty
        .iter()
        .filter(|p| p.starts_with(fx.vault.path().to_string_lossy().as_ref()))
        .filter_map(|p| p.rsplit("wiki-").next()?.strip_suffix(".md")?.parse().ok())
        .collect();
    eprintln!("dirty ids: {dirty_ids:?}");
    for behind in (101..=105).chain(121..=127) {
        assert!(
            !dirty_ids.contains(&behind),
            "note {behind} has k closer neighbours than the changed note"
        );
    }
    for short in [111, 112] {
        assert!(
            dirty_ids.contains(&short),
            "note {short} has fewer than k neighbours, the changed note can join them"
        );
    }

    let before = fx.contents();
    fx.project(false).await;
    let by_changed = fx.contents();
    fx.restore(&before);
    fx.project(true).await;
    assert_eq!(by_changed, fx.contents(), "changed-only run == full pass");
    assert!(
        relates_to_of(&by_changed["wiki-0111.md"]).contains("wiki-0901"),
        "the short note took the new neighbour"
    );
}

impl Fixture {
    /// A document that is not a wiki note (`project_note` never links it): same rows as
    /// `put_note_vec`, at `rel` under the vault (any name, any directory).
    async fn put_other(&self, rel: &str, concepts: &[&str], embedding: Vec<f32>) -> String {
        let file = self.vault.path().join(rel);
        std::fs::create_dir_all(file.parent().expect("parent")).expect("mkdir");
        let src = file.to_string_lossy().into_owned();
        let front = FrontMatter {
            origin: "personal".into(),
            project: "proj-other".into(),
            kind: "note".into(),
            source_path: src.clone(),
            ..Default::default()
        };
        self.store
            .upsert_document(&front, "sha", SystemTime::now() - DAY)
            .await
            .expect("upsert document");
        self.store
            .upsert_chunk(&Doc {
                id: format!("{src}#0"),
                content: format!("other {rel}"),
                embedding,
                front,
                chunk_idx: 0,
            })
            .await
            .expect("upsert chunk");
        self.store
            .clear_semantic_edges(&src)
            .await
            .expect("clear edges");
        for c in concepts {
            let slug = format!("{c}-{}", self.nonce);
            self.store.upsert_concept(&slug, c).await.expect("concept");
            self.store
                .relate_doc_concept(&src, &slug)
                .await
                .expect("edge");
        }
        src
    }

    /// Delete `victim`, then check that the changed-only run (which sees `wiki-0900` as the one
    /// new note) writes what a full pass writes, and that note `b` is among the notes it visits.
    async fn assert_incremental_equals_full_and_visits(&self, b: u32) {
        let before = self.contents();
        let scope = self.store.projection_scope(false).await.expect("scope");
        let dirty = match scope {
            drudge::store::ProjectionScope::Changed(dirty) => Ok(dirty),
            full @ drudge::store::ProjectionScope::Full(_) => Err(full),
        }
        .expect("a changed set");
        assert!(
            dirty.contains(&self.path(b).to_string_lossy().into_owned()),
            "note {b} must be dirty: a new note belongs in its top-k"
        );
        self.project(false).await;
        let by_changed = self.contents();
        self.restore(&before);
        self.project(true).await;
        assert_eq!(by_changed, self.contents(), "changed-only run == full pass");
        assert!(relates_to_of(&by_changed[&format!("wiki-{b:04}.md")]).contains("wiki-0900"));
    }
}

/// Semantic: B's raw top-4 holds a non-wiki document. It was never one of B's links, so deleting
/// it frees a slot that a note beyond the old 4th distance now fills.
#[tokio::test]
async fn deleted_non_wiki_semantic_neighbour_frees_a_slot() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    for id in 101..=104 {
        fx.put_note(id, "proj-sem", &[], 7, DAY).await;
    }
    let other = fx.put_other("notes-sem.md", &[], fx.embedding(7)).await;
    fx.project(false).await;

    fx.store.delete_document(&other).await.expect("delete");
    let mut near = fx.embedding(7);
    for x in near.iter_mut().take(100) {
        *x = -*x;
    }
    fx.put_note_vec(900, "proj-x", &[], near, Duration::ZERO)
        .await;
    fx.assert_incremental_equals_full_and_visits(101).await;
}

/// Graph: B's raw top-6 holds a non-wiki document with a higher shared count.
#[tokio::test]
async fn deleted_non_wiki_graph_neighbour_frees_a_slot() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    fx.put_note(121, "proj-graph", &["k1", "k2", "k3", "k4"], 121, DAY)
        .await;
    for id in 122..=126 {
        fx.put_note(id, "proj-graph", &["k1", "k2", "k3"], u64::from(id), DAY)
            .await;
    }
    let other = fx
        .put_other(
            "notes-graph.md",
            &["k1", "k2", "k3", "k4"],
            fx.embedding(555),
        )
        .await;
    fx.project(false).await;

    fx.store.delete_document(&other).await.expect("delete");
    fx.put_note(900, "proj-x", &["k1", "k2", "k3"], 900, Duration::ZERO)
        .await;
    fx.assert_incremental_equals_full_and_visits(121).await;
}

/// Semantic: B's raw top-4 holds a copy of a neighbour under another directory. `push_new`
/// keeps the first of two paths with one stem, so the other is in the top-4 but not in B's links;
/// moving it away must not leave a bound that hides a note which now belongs in the top-4.
#[tokio::test]
async fn moved_duplicate_stem_neighbour_frees_a_slot() {
    let Some(fx) = Fixture::new().await else {
        eprintln!("SKIP: BORING_TEST_DATABASE_URL not set");
        return;
    };
    let flipped = |flips: usize| {
        let mut v = fx.embedding(7);
        for x in v.iter_mut().take(flips) {
            *x = -*x;
        }
        v
    };
    fx.put_note_vec(101, "proj-sem", &[], flipped(0), DAY).await;
    fx.put_other("copy/wiki-0102.md", &[], flipped(0)).await;
    fx.put_note_vec(102, "proj-sem", &[], flipped(10), DAY)
        .await;
    fx.put_note_vec(103, "proj-sem", &[], flipped(20), DAY)
        .await;
    fx.put_note_vec(104, "proj-sem", &[], flipped(30), DAY)
        .await;
    fx.project(false).await;

    fx.put_note(102, "proj-sem", &[], 4242, Duration::ZERO)
        .await;
    fx.put_note_vec(900, "proj-x", &[], flipped(100), Duration::ZERO)
        .await;
    fx.assert_incremental_equals_full_and_visits(101).await;
}
