//! `drudge version` must answer without opening the store: Store::open runs the startup
//! schema batch (including the claim unseal UPDATE) against whatever database PG_DSN names.
#![allow(clippy::expect_used)] // tests may fail fast on setup errors

#[test]
fn version_answers_without_opening_the_store() {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_drudge"))
        .arg("version")
        .env("BORING_VECTOR", "on")
        .env("PG_DSN", "postgresql://boring:boring@127.0.0.1:1/boring")
        .output()
        .expect("spawn drudge version");
    assert!(
        out.status.success(),
        "version failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!out.stdout.is_empty(), "version printed nothing");
}
