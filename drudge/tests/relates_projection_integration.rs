//! `vault::project_links` visits only the notes a change can reach, and writes what a full pass
//! would write. Needs a scratch Postgres via `BORING_TEST_DATABASE_URL`; run serially:
//!   `BORING_TEST_DATABASE_URL=postgresql://boring:boring@localhost:5432/<scratch> \`
//!   `  cargo test -p drudge --test relates_projection_integration -- --test-threads=1 --nocapture`
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use drudge::frontmatter::FrontMatter;
use drudge::store::{Doc, Store};
use drudge::vault::project_links;
use tempfile::TempDir;
use tokio_postgres::NoTls;

const DAY: Duration = Duration::from_hours(24);

struct Fixture {
    store: Store,
    vault: TempDir,
    nonce: u64,
}

impl Fixture {
    /// Fresh vault + a projection state that never ran (no rows, no watermark).
    async fn new() -> Option<Self> {
        let dsn = std::env::var("BORING_TEST_DATABASE_URL").ok()?;
        let store = Store::open(&dsn, 1024).await.expect("open store");
        let (client, conn) = tokio_postgres::connect(&dsn, NoTls).await.expect("connect");
        tokio::spawn(conn);
        client
            .batch_execute(
                "DELETE FROM relates_projection;
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
                embedding: self.embedding(vector_seed),
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
