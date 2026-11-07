//! The door's wording of a Postgres failure — psycopg `str(e)`, which is libpq's text — so a 502
//! body carries the same bytes whichever side answered. The fold branches once on what failed:
//! a server refusal while connecting, a server error while querying, or anything else (io, pool
//! timeout), which keeps the engine's own chain text.

use std::net::{IpAddr, ToSocketAddrs};

use deadpool_postgres::PoolError;
use tokio_postgres::config::Host;
use tokio_postgres::error::DbError;

/// Where the pool dials, rendered the way libpq names it in a connection failure. A host name is
/// resolved to its IP once at open: libpq prints the address it dialled, never the name.
#[derive(Debug, Clone)]
pub struct ConnectTarget(String);

impl ConnectTarget {
    pub fn from_config(cfg: &tokio_postgres::Config) -> Self {
        let port = cfg.get_ports().first().copied().unwrap_or(5432);
        let rendered = match cfg.get_hosts().first() {
            Some(Host::Tcp(name)) => {
                format!(
                    "connection to server at \"{}\", port {port}",
                    resolve(name, port)
                )
            }
            #[cfg(unix)]
            Some(Host::Unix(dir)) => format!(
                "connection to server on socket \"{}/.s.PGSQL.{port}\"",
                dir.display()
            ),
            None => format!("connection to server at \"localhost\", port {port}"),
        };
        Self(rendered)
    }
}

/// The address libpq would have printed; a name that does not resolve stays a name (the pool
/// cannot dial it either, and the failure then says which name).
fn resolve(name: &str, port: u16) -> String {
    if name.parse::<IpAddr>().is_ok() {
        return name.to_owned();
    }
    (name, port)
        .to_socket_addrs()
        .ok()
        .and_then(|mut addrs| addrs.next())
        .map_or_else(|| name.to_owned(), |addr| addr.ip().to_string())
}

enum Failure<'a> {
    Connecting(&'a DbError),
    Querying(&'a DbError),
    /// The dial itself was refused at TCP (nothing listening): no server message, only the errno.
    Refused,
    Other,
}

fn is_refused(pg: &tokio_postgres::Error) -> bool {
    let mut cause = std::error::Error::source(pg);
    while let Some(e) = cause {
        if let Some(io) = e.downcast_ref::<std::io::Error>() {
            return io.kind() == std::io::ErrorKind::ConnectionRefused;
        }
        cause = e.source();
    }
    false
}

fn classify(e: &anyhow::Error) -> Failure<'_> {
    for cause in e.chain() {
        if let Some(PoolError::Backend(pg)) = cause.downcast_ref::<PoolError>()
            && let Some(db) = pg.as_db_error()
        {
            return Failure::Connecting(db);
        }
        if let Some(PoolError::Backend(pg)) = cause.downcast_ref::<PoolError>()
            && is_refused(pg)
        {
            return Failure::Refused;
        }
        if let Some(pg) = cause.downcast_ref::<tokio_postgres::Error>()
            && let Some(db) = pg.as_db_error()
        {
            return Failure::Querying(db);
        }
    }
    Failure::Other
}

/// True when the failure is a server refusal at connect time (the door's `store` stage).
pub fn is_connecting(e: &anyhow::Error) -> bool {
    matches!(classify(e), Failure::Connecting(_) | Failure::Refused)
}

/// libpq's message block: message, then DETAIL, HINT, CONTEXT — measured in boring-door with
/// psycopg on `raise exception 'm' using detail='d', hint='h'` (2026-10-28).
fn query_text(db: &DbError) -> String {
    let mut text = db.message().to_owned();
    for (label, part) in [
        ("DETAIL", db.detail()),
        ("HINT", db.hint()),
        ("CONTEXT", db.where_()),
    ] {
        if let Some(part) = part {
            text = format!("{text}\n{label}:  {part}");
        }
    }
    text
}

pub fn door_detail(target: &ConnectTarget, e: &anyhow::Error) -> String {
    match classify(e) {
        Failure::Connecting(db) => format!(
            "connection failed: {} failed: {}:  {}",
            target.0,
            db.severity(),
            db.message()
        ),
        Failure::Querying(db) => query_text(db),
        Failure::Refused => {
            let hint = if target.0.contains(" on socket ") {
                "Is the server running locally and accepting connections on that socket?"
            } else {
                "Is the server running on that host and accepting TCP/IP connections?"
            };
            format!(
                "connection failed: {} failed: Connection refused\n\t{hint}",
                target.0
            )
        }
        Failure::Other => format!("{e:#}"),
    }
}

/// A failure tagged with the search stage it came from — the door's `prefix: detail` shape.
#[derive(Debug)]
pub struct Staged {
    pub stage: &'static str,
    pub error: anyhow::Error,
}

pub fn stage(stage: &'static str) -> impl FnOnce(anyhow::Error) -> Staged {
    move |error| Staged { stage, error }
}

impl Staged {
    /// A server refusal while dialling is the `store` stage whichever query dialled first.
    pub fn door_text(&self, target: &ConnectTarget) -> String {
        let stage = if is_connecting(&self.error) {
            "store"
        } else {
            self.stage
        };
        format!("{stage}: {}", door_detail(target, &self.error))
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use anyhow::Context;
    use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod, Runtime};
    use tokio_postgres::NoTls;

    use super::*;

    /// A pool whose dial hits a port nothing listens on — a TCP refusal, no server involved.
    async fn refused() -> (ConnectTarget, anyhow::Error) {
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let cfg: tokio_postgres::Config =
            format!("postgres://u@127.0.0.1:{port}/d").parse().unwrap();
        let target = ConnectTarget::from_config(&cfg);
        let manager = Manager::from_config(
            cfg,
            NoTls,
            ManagerConfig {
                recycling_method: RecyclingMethod::Verified,
            },
        );
        let pool = Pool::builder(manager)
            .runtime(Runtime::Tokio1)
            .build()
            .unwrap();
        let e = pool
            .get()
            .await
            .map(|_| ())
            .context("get postgres connection from pool")
            .unwrap_err();
        (target, e)
    }

    #[tokio::test]
    async fn tcp_refusal_is_libpq_text_and_the_store_stage() {
        let (target, e) = refused().await;
        let port = target.0.rsplit("port ").next().unwrap().to_owned();
        assert_eq!(
            door_detail(&target, &e),
            format!(
                "connection failed: connection to server at \"127.0.0.1\", port {port} failed: Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?"
            )
        );
        let staged = stage("vector")(e);
        assert!(
            staged
                .door_text(&target)
                .starts_with("store: connection failed: ")
        );
    }
}
