//! C-SURAUTH1: `SurrealClient::run` signs in once and sends a Bearer token, so a request does not pay
//! the server's password check (Basic ~10-13 ms, Bearer ~0.2 ms on v3.3.4).
//!
//! Needs a throwaway SurrealDB (root user, password in `BORING_SURREAL_PASSWORD`). Run:
//!   BORING_TEST_SURREAL_URL=http://127.0.0.1:<port> BORING_SURREAL_PASSWORD=root \
//!   cargo test --test vector_surreal_auth -- --ignored --nocapture --test-threads=1
#![allow(clippy::expect_used, clippy::unwrap_used)]

use std::time::{Duration, Instant};

use drudge::config::SurrealConfig;
use drudge::store::SurrealClient;
use serde_json::{Value, json};

const CALLS: usize = 100;
const WARM_UP: usize = 10;

fn client_with(url: &str, password: Option<&str>) -> SurrealClient {
    let config: SurrealConfig = serde_json::from_value(
        json!({"url": url, "namespace": "surauth1", "database": "surauth1"}),
    )
    .unwrap();
    let mut settings = config
        .settings(&|n| std::env::var(n).ok())
        .expect("BORING_SURREAL_PASSWORD");
    if let Some(p) = password {
        p.clone_into(&mut settings.password);
    }
    SurrealClient::new(settings).unwrap()
}

fn url() -> String {
    std::env::var("BORING_TEST_SURREAL_URL").expect("BORING_TEST_SURREAL_URL")
}

fn median_p95(mut v: Vec<Duration>) -> (Duration, Duration) {
    v.sort();
    (v[v.len() / 2], v[v.len() * 95 / 100])
}

async fn timed<F, Fut>(mut call: F) -> (Duration, Duration)
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = ()>,
{
    for _ in 0..WARM_UP {
        call().await;
    }
    let mut samples = Vec::with_capacity(CALLS);
    for _ in 0..CALLS {
        let t = Instant::now();
        call().await;
        samples.push(t.elapsed());
    }
    median_p95(samples)
}

/// Raw reqwest with a token from `/signin`: what the adapter has to come close to.
async fn bearer_control(url: &str) -> (Duration, Duration) {
    let http = reqwest::Client::new();
    let signin: Value = http
        .post(format!("{url}/signin"))
        .header("Accept", "application/json")
        .json(&json!({"user": "root", "pass": std::env::var("BORING_SURREAL_PASSWORD").unwrap()}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let token = signin["token"].as_str().expect("signin token").to_owned();
    timed(|| async {
        let r = http
            .post(format!("{url}/rpc"))
            .bearer_auth(&token)
            .header("Surreal-NS", "surauth1")
            .header("Surreal-DB", "surauth1")
            .header("Accept", "application/json")
            .json(&json!({"id": 1, "method": "query", "params": ["RETURN 1"]}))
            .send()
            .await
            .unwrap();
        assert!(r.status().is_success());
        r.bytes().await.unwrap();
    })
    .await
}

#[tokio::test]
#[ignore = "needs BORING_TEST_SURREAL_URL"]
async fn adapter_latency_is_within_twice_the_bearer_control() {
    let url = url();
    let client = client_with(&url, None);
    let (m, p) = timed(|| async {
        client.run("RETURN 1").await.unwrap();
    })
    .await;
    let (cm, cp) = bearer_control(&url).await;
    println!("adapter median {m:?} p95 {p:?} | bearer control median {cm:?} p95 {cp:?}");
    assert!(
        m <= cm * 2,
        "adapter median {m:?} is over 2x the bearer control {cm:?}"
    );
}

#[tokio::test]
#[ignore = "needs BORING_TEST_SURREAL_URL"]
async fn a_rejected_token_signs_in_again_once_and_a_wrong_password_fails() {
    let url = url();
    let client = client_with(&url, None);
    client.run("RETURN 1").await.unwrap();
    client.replace_token("not-a-token").await;
    let rows = client.run("RETURN 1").await.expect("re-sign-in");
    println!("invalid token -> {rows:?}");
    assert_eq!(rows, vec![json!(1)]);

    let wrong = client_with(&url, Some("definitely-wrong"));
    let err = wrong.run("RETURN 1").await.expect_err("wrong password");
    println!("wrong password -> {err}");
    assert!(err.to_string().contains("problem with authentication"));
}
