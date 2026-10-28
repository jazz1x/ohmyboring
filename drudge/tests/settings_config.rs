//! boring.json `schedule` / `collect`: the engine binary, given a real process env, reports the
//! effective values the shared fixture says. src/ohmyboring/test_config_settings.py runs the same
//! fixture through the Python parser, so a changed value reads the same on both sides.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use serde_json::Value;

fn run_config(boring: &Value, env: &serde_json::Map<String, Value>) -> Value {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("boring.json");
    let mut doc = boring.clone();
    doc["schema_version"] = Value::from(2);
    std::fs::write(&path, doc.to_string()).expect("write boring.json");
    let mut cmd = std::process::Command::new(env!("CARGO_BIN_EXE_drudge"));
    cmd.arg("config")
        .env_clear()
        .env("PATH", std::env::var("PATH").unwrap_or_default())
        .env("BORING_CONFIG", &path);
    for (name, value) in env {
        cmd.env(name, value.as_str().expect("env values are strings"));
    }
    let out = cmd.output().expect("spawn drudge config");
    assert!(
        out.status.success(),
        "drudge config failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).expect("drudge config prints JSON")
}

#[test]
fn engine_reads_every_fixture_case_like_the_fixture_says() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/settings_cases.json")).expect("fixture JSON");
    for case in &cases {
        let name = case["name"].as_str().expect("case name");
        let env = case["env"].as_object().expect("case env");
        let view = run_config(&case["boring"], env);
        for block in ["schedule", "collect"] {
            let Some(expected) = case["expect"].get(block).and_then(Value::as_object) else {
                continue;
            };
            for (field, want) in expected {
                assert_eq!(
                    &view[block]["effective"][field], want,
                    "{name}: {block}.{field}"
                );
            }
        }
    }
}

#[test]
fn config_get_names_the_env_that_took_over_and_lists_unmanaged_jobs() {
    let env = serde_json::json!({"BORING_SYNC_HOURS": "6", "COLLECT_MIN_KB": "40"});
    let view = run_config(
        &serde_json::json!({"schedule": {"sync_hours": 5}}),
        env.as_object().unwrap(),
    );
    assert_eq!(view["schedule"]["file"]["sync_hours"], 5);
    assert_eq!(view["schedule"]["effective"]["sync_hours"], 6);
    assert_eq!(
        view["schedule"]["overridden_by_env"]["sync_hours"],
        "BORING_SYNC_HOURS"
    );
    assert_eq!(
        view["collect"]["overridden_by_env"]["min_bytes"],
        "COLLECT_MIN_KB"
    );
    assert!(
        view["unmanaged"]
            .as_array()
            .unwrap()
            .iter()
            .any(|job| job["name"] == "com.olympus.collect" && job["interval_seconds"] == 10800)
    );
    assert_eq!(
        view["hermes_cron_jobs"]["effective"]["morning-card"]["schedule"],
        "0 8 * * *"
    );
}

#[test]
fn unknown_field_inside_a_block_loads_with_one_warning() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("boring.json");
    std::fs::write(
        &path,
        r#"{"schema_version": 2, "schedule": {"sync_hour": 3, "sync_hours": 6}}"#,
    )
    .expect("write");
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_drudge"))
        .arg("config")
        .env_clear()
        .env("BORING_CONFIG", &path)
        .output()
        .expect("spawn");
    assert!(
        out.status.success(),
        "load failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    let warnings: Vec<&str> = stderr.lines().filter(|l| l.contains("unknown")).collect();
    assert_eq!(warnings.len(), 1, "stderr: {stderr}");
    assert!(warnings[0].contains("sync_hour"));
    let view: Value = serde_json::from_slice(&out.stdout).expect("JSON");
    assert_eq!(view["schedule"]["effective"]["sync_hours"], 6);
}

#[test]
fn schedule_block_with_a_value_out_of_range_is_refused() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("boring.json");
    std::fs::write(
        &path,
        r#"{"schema_version": 2, "schedule": {"brief_hour": 24}}"#,
    )
    .expect("write");
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_drudge"))
        .arg("config")
        .env("BORING_CONFIG", &path)
        .output()
        .expect("spawn");
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("brief_hour"));
}
