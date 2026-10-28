//! The `gap` report — "memory had nothing, or something stale, for the question I asked".
//! Same boundary as `src/ohmyboring/gap/parse.py`: HTTP body and MCP arguments come through the
//! same door and become the same value; the messages are the Python door's, word for word.

use std::collections::HashSet;

use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::claim_source::is_py_space;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Missing,
    Stale,
    Broken,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Stale => "stale",
            Self::Broken => "broken",
        }
    }

    fn parse(value: Option<&Value>) -> Option<Self> {
        match value.and_then(Value::as_str) {
            Some("missing") => Some(Self::Missing),
            Some("stale") => Some(Self::Stale),
            Some("broken") => Some(Self::Broken),
            _ => None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct GapArgs {
    pub session_id: String,
    pub query: String,
    pub kind: Kind,
    /// Distinct, in first-seen order.
    pub handed: Vec<String>,
}

/// The write's answer; field order is the JSON key order.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct GapReport {
    pub gap: String,
    pub kind: &'static str,
    pub handed: usize,
    pub unknown: usize,
}

fn required_text(arguments: &Map<String, Value>, key: &str) -> Result<String, String> {
    match arguments.get(key).and_then(Value::as_str) {
        Some(text) if !text.trim_matches(is_py_space).is_empty() => {
            Ok(text.trim_matches(is_py_space).to_owned())
        }
        _ => Err(format!("{key} is required")),
    }
}

/// `handed` absent is empty; present it must be a list of strings (`null` is not a list).
fn handed(arguments: &Map<String, Value>) -> Result<Vec<String>, String> {
    const MESSAGE: &str = "handed must be a list of strings";
    let Some(value) = arguments.get("handed") else {
        return Ok(Vec::new());
    };
    let items = value.as_array().ok_or(MESSAGE)?;
    let mut seen = HashSet::new();
    let mut distinct = Vec::new();
    for item in items {
        let path = item.as_str().ok_or(MESSAGE)?;
        if seen.insert(path) {
            distinct.push(path.to_owned());
        }
    }
    Ok(distinct)
}

/// Parse once at the boundary. Checks in the Python order: session_id, query, kind, handed,
/// then a stale gap must name what was out of date.
pub fn parse(arguments: &Map<String, Value>) -> Result<GapArgs, String> {
    let session_id = required_text(arguments, "session_id")?;
    let query = required_text(arguments, "query")?;
    let kind = Kind::parse(arguments.get("kind"))
        .ok_or_else(|| "kind must be one of missing, stale, broken".to_owned())?;
    let handed = handed(arguments)?;
    if kind == Kind::Stale && handed.is_empty() {
        return Err("stale gap needs handed notes".to_owned());
    }
    Ok(GapArgs {
        session_id,
        query,
        kind,
        handed,
    })
}

/// `gap:` plus the first 16 hex digits of sha256(`session_id`, newline, `query`).
pub fn gap_node_id(session_id: &str, query: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = hex::encode(Sha256::digest(format!("{session_id}\n{query}").as_bytes()));
    format!("gap:{}", &digest[..16])
}

/// The `tools/list` entry — the text the Python door has been listing.
pub fn tool_definition() -> Value {
    json!({
        "name": "gap",
        "description": "Report a gap in memory. CALL THIS when recall, claims or decisions returned nothing, \
                        a stale value, or failed for the question you were asking (kind: missing, stale, broken). \
                        Send the question you were looking for and the note source_paths you were handed \
                        (required for stale: say which notes were out of date). The door records the report \
                        and never judges it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Your session id."},
                "query": {"type": "string", "description": "The question memory could not answer."},
                "kind": {"type": "string", "enum": ["missing", "stale", "broken"]},
                "handed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "source_path of the notes you were handed (required for stale)."
                }
            },
            "required": ["session_id", "query", "kind"]
        }
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn args(value: &Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    // Expected values measured on `gap_parse.parse` / `gap_pg.gap_node_id` of this tree, 2026-10-28.
    #[test]
    fn parse_gives_the_python_verdicts() {
        let base = json!({"session_id": " s1 ", "query": "q", "kind": "missing"});
        let parsed = parse(&args(&base)).unwrap();
        assert_eq!(parsed.session_id, "s1");
        assert_eq!(parsed.handed, Vec::<String>::new());

        let cases = [
            (
                json!({"query": "q", "kind": "missing"}),
                "session_id is required",
            ),
            (
                json!({"session_id": "s", "query": "\u{1c} ", "kind": "missing"}),
                "query is required",
            ),
            (
                json!({"session_id": "s", "query": "q", "kind": "other"}),
                "kind must be one of missing, stale, broken",
            ),
            (
                json!({"session_id": "s", "query": "q", "kind": "missing", "handed": null}),
                "handed must be a list of strings",
            ),
            (
                json!({"session_id": "s", "query": "q", "kind": "missing", "handed": ["a", 1]}),
                "handed must be a list of strings",
            ),
            (
                json!({"session_id": "s", "query": "q", "kind": "stale"}),
                "stale gap needs handed notes",
            ),
        ];
        for (input, message) in cases {
            assert_eq!(parse(&args(&input)), Err(message.to_owned()), "{input}");
        }
        let dup =
            json!({"session_id": "s", "query": "q", "kind": "stale", "handed": ["b", "a", "b"]});
        assert_eq!(parse(&args(&dup)).unwrap().handed, ["b", "a"]);
    }

    // Fixture: `gap_parse.TOOL` dumped from the Python door, 2026-10-28. The text is what clients read.
    #[test]
    fn tool_definition_is_the_python_tool() {
        let python: Value =
            serde_json::from_str(include_str!("../tests/fixtures/gap_tool_python.json")).unwrap();
        assert_eq!(tool_definition(), python);
    }

    #[test]
    fn node_id_is_the_python_digest() {
        assert_eq!(gap_node_id("s1", "q"), "gap:d3e3705e24d6f7de");
    }
}
