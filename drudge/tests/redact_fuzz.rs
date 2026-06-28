//! Fuzz-style regression tests for secret redaction.
#![allow(clippy::expect_used, clippy::unwrap_used)] // tests may fail fast on setup errors

use drudge::redact::{build_secret_re, redact};

#[test]
fn redacts_github_fine_grained_pat() {
    let re = build_secret_re().expect("regex compiles");
    let dirty = "token: github_pat_11ABCD2EFGHIJKlm3noPQRstuvwxyz4_ABCD5efghij6klmnop7QRst8uvwxyz9";
    let clean = redact(re, dirty);
    assert!(!clean.contains("github_pat_"), "{clean}");
    assert!(clean.contains("‹REDACTED›"), "{clean}");
}

#[test]
fn redacts_aws_session_token() {
    let re = build_secret_re().expect("regex compiles");
    let dirty = "session: AQoEXAMPLEH4aoAH0gNCAPyJxz4BlCFFxWNE1OPTgk5TthT+...";
    let clean = redact(re, dirty);
    assert!(!clean.contains("AQoEXAMPLE"), "{clean}");
}

#[test]
fn redacts_jwt_variants() {
    let re = build_secret_re().expect("regex compiles");
    for jwt in [
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMe",
        "token=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpZCI6MX0.JfXOkw3v9mWJN7O-8f1Q",
    ] {
        let clean = redact(re, jwt);
        assert!(!clean.contains("eyJ"), "{clean}");
        assert!(clean.contains("‹REDACTED›"), "{clean}");
    }
}

#[test]
fn redacts_generic_api_key_with_quotes() {
    let re = build_secret_re().expect("regex compiles");
    for dirty in [
        "api_key: supersecret1234567890abcd",
        "API_KEY=supersecret1234567890abcd",
        "api-key: 'supersecret1234567890abcd'",
        "password = \"supersecret1234567890abcd\"",
    ] {
        let clean = redact(re, dirty);
        assert!(!clean.contains("supersecret1234567890abcd"), "{clean}");
        assert!(clean.contains("‹REDACTED›"), "{clean}");
    }
}
