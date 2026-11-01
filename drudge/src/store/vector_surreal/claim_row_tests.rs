use super::*;

fn row(cid: &str, superseded: Option<i64>) -> Value {
    let mut o = json!({"cid": cid, "subject": "s", "predicate": "p", "value": "v"});
    if let Some(t) = superseded {
        o["superseded_us"] = json!(t);
    }
    o
}

/// The Python door's seal changes `superseded_at` of a key both sides hold; `catch_up`'s key
/// compare cannot see it, the value compare must.
#[test]
fn a_seal_on_a_shared_key_is_a_different_row() {
    let pg = HashMap::from([("k".to_owned(), row("k", Some(5)))]);
    let surreal = HashMap::from([("k".to_owned(), row("k", None))]);
    let diff = ClaimRowDiff::of(&pg, &surreal);
    assert_eq!(diff.different, vec!["k".to_owned()]);
    assert!(diff.missing.is_empty() && diff.extra.is_empty());
    assert_eq!(diff.redo.len(), 1);
}

/// The register query keeps every condition and the order of `registers/pg.py`'s
/// `_RECENT_REGISTER_ROWS_SQL`.
#[test]
fn register_query_carries_the_pg_conditions_and_order() {
    let kinds = vec!["decision".to_owned()];
    let exclude = vec!["community".to_owned()];
    let sql = register_select(&RegisterAsk {
        k: 7,
        project: Some("omb"),
        kinds: Some(&kinds),
        exclude_origins: &exclude,
    });
    for part in [
        "superseded_us = NONE",
        "origin != NONE",
        "!(origin IN [\"community\"])",
        "project = \"omb\"",
        "kind IN [\"decision\"]",
        "eval-|daily-brief-",
        "ORDER BY rank ASC, valid_from_us DESC, subject ASC, predicate ASC",
        "LIMIT 7",
        "string::len(value) < 25",
        "(?i)^(incident|decision",
    ] {
        assert!(sql.contains(part), "missing {part} in {sql}");
    }
}
