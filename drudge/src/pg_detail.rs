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
    Other,
}

fn classify(e: &anyhow::Error) -> Failure<'_> {
    for cause in e.chain() {
        if let Some(PoolError::Backend(pg)) = cause.downcast_ref::<PoolError>()
            && let Some(db) = pg.as_db_error()
        {
            return Failure::Connecting(db);
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
    matches!(classify(e), Failure::Connecting(_))
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
