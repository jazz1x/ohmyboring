//! C-SURALL2 Z2 reads: what `backend=surreal` answers out of Surreal for the four log tables and
//! the four `code_index` tables. pg is not asked. The log rows are the copies `log_copy_surreal`
//! writes, as fresh as the last copy pass (once per sync tick); the code rows are written to
//! Surreal directly by `code-sync`.
//!
//! Each reader is the SurrealQL of one pg query in `store.rs` / `code_index/store.rs`, condition
//! for condition. Where pg orders by the column collation (`qualified_name`, paths) Surreal
//! orders with `COLLATE`; rows tied on every sort key have no defined order on either side.

use std::cmp::Ordering;
use std::collections::{BTreeMap, HashMap};

use anyhow::{Context, Result, ensure};
use serde::Deserialize;
use serde_json::Value;

use super::{SurrealClient, first_statement, micros_to_time, now_micros, text};
use crate::code_index::{CodeRelation, CodeSearchHit, CodeSymbolDetail, RepositoryStatus};
use crate::store::{
    AnchorCheckWatermark, EventLogFilter, EventLogRow, QueryLogRow, RecallLabelRow,
    RecallLabelStats,
};

/// Rows the code search returns, the pg query's `LIMIT`.
const CODE_SEARCH_LIMIT: usize = 20;

#[derive(Deserialize)]
struct EventRow {
    pid: i64,
    observed_us: i64,
    #[serde(default)]
    time_unix_nano: Option<i64>,
    severity_text: String,
    severity_number: i32,
    service_name: String,
    component: String,
    event_name: String,
    status: String,
    #[serde(default)]
    trace_id: Option<String>,
    #[serde(default)]
    span_id: Option<String>,
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    workflow: Option<String>,
    #[serde(default)]
    workflow_node: Option<String>,
    #[serde(default)]
    workflow_outcome: Option<String>,
    body: String,
    attributes: String,
    resource: String,
}

#[derive(Deserialize)]
struct QueryRow {
    pid: i32,
    created_us: i64,
    endpoint: String,
    query: String,
    hit_paths: Vec<String>,
    hit_dists: Vec<Option<f32>>,
    hit_dist_kinds: Vec<Option<String>>,
    sources: Vec<String>,
    answer_snippet: String,
    #[serde(default)]
    latency_ms: Option<i32>,
}

#[derive(Deserialize)]
struct LabelRow {
    query_log_id: i32,
    hit_index: i32,
    judge: String,
    verdict: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    note: String,
}

#[derive(Deserialize)]
struct WatermarkRow {
    #[serde(rename = "value_us")]
    valid_from_us: i64,
    #[serde(rename = "value_subject")]
    subject: String,
    #[serde(rename = "value_predicate")]
    predicate: String,
}

#[derive(Deserialize)]
struct RepoRow {
    cid: String,
    name: String,
    root_path: String,
    language: String,
    last_synced_us: i64,
}

#[derive(Deserialize)]
struct RepoCount {
    repository_id: String,
    n: i64,
}

#[derive(Deserialize)]
struct SymbolRow {
    cid: String,
    repository_id: String,
    kind: String,
    name: String,
    qualified_name: String,
    start_line: i64,
    end_line: i64,
    #[serde(default)]
    relative_path: Option<String>,
    #[serde(default)]
    rank: i32,
}

#[derive(Deserialize)]
struct RelationRow {
    kind: String,
    #[serde(default)]
    target_symbol_id: Option<String>,
    #[serde(default)]
    target_name: Option<String>,
    start_byte: i64,
    end_byte: i64,
}

fn json_text(what: &str, raw: &str) -> Result<Value> {
    serde_json::from_str(raw).with_context(|| format!("event_log {what} is not JSON text"))
}

fn line(value: i64, what: &str) -> Result<usize> {
    usize::try_from(value).with_context(|| format!("code index {what} {value} is negative"))
}

/// The order pg's `ORDER BY text` gives identifiers and paths under the database's `en_US.utf8`
/// collation (glibc): letters and digits first, case ignored and punctuation skipped; a tie goes
/// to the lowercase spelling, then to the punctuation. Measured on the pgvector image
/// (`parseGamma < Parse_Other < parse_token`, `a::b < a_b < ab < aB`); a non-ASCII text is only
/// as good as `char::is_alphanumeric` and `to_lowercase`.
pub(super) fn en_us_order(a: &str, b: &str) -> Ordering {
    let letters = |s: &str| -> Vec<char> {
        s.chars()
            .filter(|c| c.is_alphanumeric())
            .flat_map(char::to_lowercase)
            .collect()
    };
    let upper_flags = |s: &str| -> Vec<bool> {
        s.chars()
            .filter(|c| c.is_alphanumeric())
            .map(char::is_uppercase)
            .collect()
    };
    letters(a)
        .cmp(&letters(b))
        .then_with(|| upper_flags(a).cmp(&upper_flags(b)))
        .then_with(|| a.cmp(b))
}

/// pg's ascending sort puts NULL last.
fn none_last(a: Option<&str>, b: Option<&str>) -> Ordering {
    match (a, b) {
        (Some(x), Some(y)) => x.cmp(y),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    }
}

impl SurrealClient {
    /// `Store::recent_events`.
    ///
    /// # Errors
    /// The request failed or a stored JSON text is not JSON.
    pub async fn recent_events(&self, f: &EventLogFilter<'_>) -> Result<Vec<EventLogRow>> {
        let mut conds = vec!["true".to_owned()];
        for (column, value) in [
            ("component", f.component),
            ("event_name", f.event_name),
            ("status", f.status),
            ("run_id", f.run_id),
            ("workflow", f.workflow),
        ] {
            if let Some(v) = value {
                conds.push(format!("{column} = {}", text(v)));
            }
        }
        if let Some(hours) = f.since_hours {
            conds.push(format!(
                "observed_us >= {}",
                now_micros()? - i64::from(hours) * 3_600_000_000
            ));
        }
        let sql = format!(
            "SELECT pid, observed_us, time_unix_nano, severity_text, severity_number, service_name,
                    component, event_name, status, trace_id, span_id, run_id, session_id, workflow,
                    workflow_node, workflow_outcome, body, attributes, resource
               FROM event_log WHERE {} ORDER BY observed_us DESC, pid DESC LIMIT {};",
            conds.join(" AND "),
            f.limit.max(0)
        );
        let rows: Vec<EventRow> = first_statement(&sql, self.rows(&sql).await?)?;
        rows.into_iter()
            .map(|r| {
                Ok(EventLogRow {
                    id: r.pid,
                    observed_at: micros_to_time(r.observed_us, "event_log observed_us")?,
                    time_unix_nano: r.time_unix_nano,
                    severity_text: r.severity_text,
                    severity_number: r.severity_number,
                    service_name: r.service_name,
                    component: r.component,
                    event_name: r.event_name,
                    status: r.status,
                    trace_id: r.trace_id,
                    span_id: r.span_id,
                    run_id: r.run_id,
                    session_id: r.session_id,
                    workflow: r.workflow,
                    workflow_node: r.workflow_node,
                    workflow_outcome: r.workflow_outcome,
                    body: json_text("body", &r.body)?,
                    attributes: json_text("attributes", &r.attributes)?,
                    resource: json_text("resource", &r.resource)?,
                })
            })
            .collect()
    }

    /// `Store::recent_queries`.
    ///
    /// # Errors
    /// The request failed.
    pub async fn recent_queries(&self, limit: i64) -> Result<Vec<QueryLogRow>> {
        let sql = format!(
            "SELECT pid, created_us, endpoint, query, hit_paths, hit_dists, hit_dist_kinds,
                    sources, answer_snippet, latency_ms
               FROM query_log ORDER BY created_us DESC, pid DESC LIMIT {};",
            limit.max(0)
        );
        let rows: Vec<QueryRow> = first_statement(&sql, self.rows(&sql).await?)?;
        rows.into_iter()
            .map(|r| {
                Ok(QueryLogRow {
                    id: r.pid,
                    created_at: micros_to_time(r.created_us, "query_log created_us")?,
                    endpoint: r.endpoint,
                    query: r.query,
                    hit_paths: r.hit_paths,
                    hit_dists: r.hit_dists,
                    hit_dist_kinds: r.hit_dist_kinds,
                    sources: r.sources,
                    answer_snippet: r.answer_snippet,
                    latency_ms: r.latency_ms,
                })
            })
            .collect()
    }

    /// `Store::recent_recall_labels`.
    ///
    /// # Errors
    /// The request failed.
    pub async fn recent_recall_labels(&self, limit: i64) -> Result<Vec<RecallLabelRow>> {
        let sql = format!(
            "SELECT query_log_id, hit_index, judge, verdict, model, note
               FROM recall_label ORDER BY created_us DESC LIMIT {};",
            limit.max(0)
        );
        let rows: Vec<LabelRow> = first_statement(&sql, self.rows(&sql).await?)?;
        Ok(rows
            .into_iter()
            .map(|r| RecallLabelRow {
                query_log_id: r.query_log_id,
                hit_index: r.hit_index,
                judge: r.judge,
                verdict: r.verdict,
                model: r.model,
                note: r.note,
            })
            .collect())
    }

    /// `Store::recall_label_stats`: counts per judge in judge order, then how often the `llm` and
    /// `human` judges agreed on hits both labelled with a real verdict (`unsure` leaves the
    /// denominator). The table is a few thousand rows, so the grouping is done here over all of it.
    ///
    /// # Errors
    /// The request failed.
    pub async fn recall_label_stats(&self) -> Result<(Vec<RecallLabelStats>, i64, i64)> {
        let sql = "SELECT query_log_id, hit_index, judge, verdict, model, note FROM recall_label;";
        let rows: Vec<LabelRow> = first_statement(sql, self.rows(sql).await?)?;
        let mut by_judge: BTreeMap<&str, RecallLabelStats> = BTreeMap::new();
        for r in &rows {
            let stats = by_judge
                .entry(r.judge.as_str())
                .or_insert_with(|| RecallLabelStats {
                    judge: r.judge.clone(),
                    ..RecallLabelStats::default()
                });
            match r.verdict.as_str() {
                "relevant" => stats.relevant += 1,
                "irrelevant" => stats.irrelevant += 1,
                "unsure" => stats.unsure += 1,
                _ => {}
            }
        }
        let per_judge: Vec<RecallLabelStats> = by_judge.into_values().collect();
        let human: HashMap<(i32, i32), &str> = rows
            .iter()
            .filter(|r| r.judge == "human" && r.verdict != "unsure")
            .map(|r| ((r.query_log_id, r.hit_index), r.verdict.as_str()))
            .collect();
        let (mut agreed, mut compared) = (0_i64, 0_i64);
        for r in rows
            .iter()
            .filter(|r| r.judge == "llm" && r.verdict != "unsure")
        {
            if let Some(h) = human.get(&(r.query_log_id, r.hit_index)) {
                compared += 1;
                agreed += i64::from(*h == r.verdict);
            }
        }
        Ok((per_judge, agreed, compared))
    }

    /// `Store::anchor_check_watermark`.
    ///
    /// # Errors
    /// The request failed.
    pub async fn anchor_check_watermark(&self) -> Result<Option<AnchorCheckWatermark>> {
        let sql = "SELECT value_us, value_subject, value_predicate FROM sync_state \
                   WHERE cid = 'anchor_check_watermark';";
        let rows: Vec<WatermarkRow> = first_statement(sql, self.rows(sql).await?)?;
        rows.into_iter()
            .next()
            .map(|r| {
                Ok(AnchorCheckWatermark {
                    valid_from: micros_to_time(r.valid_from_us, "sync_state value_us")?,
                    subject: r.subject,
                    predicate: r.predicate,
                })
            })
            .transpose()
    }

    /// `CodeIndexStore::status`.
    ///
    /// # Errors
    /// The request failed.
    pub async fn code_status(&self, repository_id: Option<&str>) -> Result<Vec<RepositoryStatus>> {
        let scope = |column: &str| {
            repository_id.map_or_else(String::new, |r| format!(" AND {column} = {}", text(r)))
        };
        let repos_sql = format!(
            "SELECT cid, name, root_path, language, last_synced_us FROM code_repository \
             WHERE true{};",
            scope("cid")
        );
        let counts = |table: &str, extra: &str, agg: &str| {
            format!(
                "SELECT repository_id, {agg} AS n FROM {table} WHERE true{extra}{} \
                 GROUP BY repository_id;",
                scope("repository_id")
            )
        };
        let statements = [
            repos_sql,
            counts("code_file", "", "count()"),
            counts("code_symbol", "", "count()"),
            counts("code_relation", "", "count()"),
            counts("code_file", " AND error_count > 0", "count()"),
            counts("code_file", "", "math::sum(error_count)"),
        ];
        let sql = statements.concat();
        let answered = self.run(&sql).await?;
        ensure!(
            answered.len() == statements.len(),
            "surreal answered {} statements to {}",
            answered.len(),
            statements.len()
        );
        let mut parts = answered.into_iter();
        let mut next = || parts.next().context("a code_status statement is missing");
        let repos: Vec<RepoRow> =
            serde_json::from_value(next()?).context("surreal code_repository rows")?;
        let mut tallies: Vec<HashMap<String, i64>> = Vec::with_capacity(5);
        for _ in 0..5 {
            let rows: Vec<RepoCount> =
                serde_json::from_value(next()?).context("surreal code_status counts")?;
            tallies.push(rows.into_iter().map(|c| (c.repository_id, c.n)).collect());
        }
        let mut out = repos
            .into_iter()
            .map(|r| {
                let n = |i: usize| tallies[i].get(&r.cid).copied().unwrap_or(0);
                Ok(RepositoryStatus {
                    last_synced_at: micros_to_time(r.last_synced_us, "code_repository")?,
                    files: line(n(0), "files")?,
                    symbols: line(n(1), "symbols")?,
                    relations: line(n(2), "relations")?,
                    files_with_errors: line(n(3), "files_with_errors")?,
                    parse_errors: line(n(4), "parse_errors")?,
                    repository_id: r.cid,
                    name: r.name,
                    root_path: r.root_path,
                    language: r.language,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        out.sort_by(|a, b| a.repository_id.cmp(&b.repository_id));
        Ok(out)
    }

    /// `CodeIndexStore::search`: symbols whose name, qualified name or file path contains the
    /// query (case-insensitive), best match first, the first 20.
    ///
    /// # Errors
    /// The request failed.
    pub async fn code_search(
        &self,
        query: &str,
        repository_id: Option<&str>,
    ) -> Result<Vec<CodeSearchHit>> {
        let needle = text(&query.to_lowercase());
        let path =
            "(SELECT VALUE relative_path FROM ONLY type::record('code_file', $parent.file_id))";
        let repo =
            repository_id.map_or_else(String::new, |r| format!(" AND repository_id = {}", text(r)));
        let sql = format!(
            "SELECT cid, repository_id, kind, name, qualified_name, start_line, end_line,
                    {path} AS relative_path,
                    (IF string::lowercase(name) = {needle} {{ 0 }}
                     ELSE IF string::starts_with(string::lowercase(name), {needle}) {{ 1 }}
                     ELSE IF string::starts_with(string::lowercase(qualified_name), {needle}) {{ 2 }}
                     ELSE {{ 3 }}) AS rank
               FROM code_symbol
              WHERE {path} != NONE{repo}
                AND (string::contains(string::lowercase(name), {needle})
                     OR string::contains(string::lowercase(qualified_name), {needle})
                     OR string::contains(string::lowercase({path}), {needle}))
              ORDER BY rank ASC;"
        );
        let mut rows: Vec<SymbolRow> = first_statement(&sql, self.rows(&sql).await?)?;
        rows.sort_by(|a, b| {
            a.rank
                .cmp(&b.rank)
                .then_with(|| en_us_order(&a.qualified_name, &b.qualified_name))
                .then_with(|| {
                    en_us_order(
                        a.relative_path.as_deref().unwrap_or(""),
                        b.relative_path.as_deref().unwrap_or(""),
                    )
                })
                .then_with(|| a.repository_id.cmp(&b.repository_id))
                .then_with(|| a.cid.cmp(&b.cid))
        });
        rows.into_iter()
            .take(CODE_SEARCH_LIMIT)
            .map(symbol_hit)
            .collect()
    }

    /// `CodeIndexStore::symbol`.
    ///
    /// # Errors
    /// The request failed.
    pub async fn code_symbol(&self, id: &str) -> Result<Option<CodeSymbolDetail>> {
        let sql = format!(
            "SELECT cid, repository_id, kind, name, qualified_name, start_line, end_line,
                    (SELECT VALUE relative_path FROM ONLY type::record('code_file', $parent.file_id))
                        AS relative_path
               FROM [type::record('code_symbol', {})];",
            text(id)
        );
        let rows: Vec<SymbolRow> = first_statement(&sql, self.rows(&sql).await?)?;
        let Some(row) = rows.into_iter().find(|r| r.relative_path.is_some()) else {
            return Ok(None);
        };
        let symbol = symbol_hit(row)?;
        let sql = format!(
            "SELECT kind, target_symbol_id, target_name, start_byte, end_byte FROM code_relation \
             WHERE repository_id = {} AND source_symbol_id = {};",
            text(&symbol.repository_id),
            text(&symbol.id)
        );
        let mut found: Vec<RelationRow> = first_statement(&sql, self.rows(&sql).await?)?;
        found.sort_by(|a, b| {
            a.start_byte
                .cmp(&b.start_byte)
                .then_with(|| a.kind.cmp(&b.kind))
                .then_with(|| none_last(a.target_name.as_deref(), b.target_name.as_deref()))
        });
        let relations = found
            .into_iter()
            .map(|r| {
                Ok(CodeRelation {
                    kind: r.kind,
                    target_symbol_id: r.target_symbol_id,
                    target_name: r.target_name,
                    start_byte: line(r.start_byte, "start_byte")?,
                    end_byte: line(r.end_byte, "end_byte")?,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(Some(CodeSymbolDetail { symbol, relations }))
    }
}

fn symbol_hit(r: SymbolRow) -> Result<CodeSearchHit> {
    Ok(CodeSearchHit {
        start_line: line(r.start_line, "start_line")?,
        end_line: line(r.end_line, "end_line")?,
        relative_path: r.relative_path.context("a symbol without a file row")?,
        id: r.cid,
        repository_id: r.repository_id,
        kind: r.kind,
        name: r.name,
        qualified_name: r.qualified_name,
    })
}
