//! Store — pgvector(문서·청크·임베딩·FTS) + 그래프(node/edge 테이블 + 재귀 CTE).
//!
//! ## 레이어 (엔진 무관 그래프 모델)
//! - **pgvector** (`document`, `chunk`): 벡터(HNSW) + FTS(tsvector) + frontmatter 컬럼.
//! - **그래프** (`node`, `edge`): 시맨틱 온톨로지. 노드=엔티티, 엣지=타입드 관계.
//!   - 노드 id 규약: `doc:<source_path>` · `project:<name>` · `topic:<tag>`
//!     · `problem|solution|tool|concept:<slug>` · `attempt:<path>#<idx>`.
//!   - 문서는 `document` 테이블이 SSOT, 그래프에선 `doc:<path>` id 로 참조(중복저장 X).
//! - **순회**: 재귀 CTE(`neighbors_khop`) — 엔진이 그래프DB 아니어도 k-hop 가능.
//!   CTE 가 모자라지면 AGE/서리얼로 lift-and-shift (스키마 동일).
//!
//! ## AGE 대비 이점
//! 모든 값이 `tokio-postgres` 파라미터 바인딩($1,$2…) → cypher 문자열 이스케이프 footgun 제거.
use std::time::SystemTime;

use anyhow::{Context, Result};
use pgvector::Vector;
use tokio_postgres::{Client, NoTls};

use crate::frontmatter::FrontMatter;

#[allow(dead_code)]
pub const EMBED_DIM: usize = 1024; // bge-m3

/// 적재 입력(청크 1개).
pub struct Doc {
    pub id: String, // "{source_path}#{idx}"
    pub content: String,
    pub embedding: Vec<f32>,
    pub front: FrontMatter,
    pub chunk_idx: usize,
}

#[derive(Debug)]
#[allow(dead_code)] // 일부 필드는 retrieve · 표시용
pub struct Hit {
    pub id: String,
    pub content: String,
    pub origin: String,
    pub project: String,
    pub source_path: String,
    pub dist: f32,
}

#[derive(Debug)]
pub struct Meta {
    pub origin: String,
    pub project: String,
    pub kind: String,
    pub source_path: String,
}

/// 최신순 회수 1건 — 한 문서의 본문 전체(청크 결합). `updated_at` 내림차순으로 반환.
#[derive(Debug)]
pub struct RecentDoc {
    pub source_path: String,
    pub project: String,
    pub content: String,
}

/// 그래프 규모 요약(audit 용).
#[derive(Debug, Default)]
pub struct GraphStats {
    pub documents: usize,
    pub chunks: usize,
    pub projects: usize,
    pub topics: usize,
    pub edges: usize,
}

/// GC 삭제 통계.
#[derive(Debug, Default)]
pub struct GcStats {
    pub tool: usize,
    pub concept: usize,
    pub problem: usize,
    pub solution: usize,
    pub attempt: usize,
}

impl GcStats {
    pub const fn total(&self) -> usize {
        self.tool + self.concept + self.problem + self.solution + self.attempt
    }
}

/// 시맨틱 그래프 통계(audit 용).
#[derive(Debug, Default)]
pub struct SemanticStats {
    pub problems: usize,
    pub solutions: usize,
    pub tools: usize,
    pub concepts: usize,
    pub attempts: usize,
    pub addresses: usize,
    pub resolved_by: usize,
    pub uses: usize,
    pub about: usize,
    pub tried: usize,
}

pub struct Store {
    db: Client,
}

/// 시맨틱 엣지 종류(doc→entity) — clear/통계에서 공유하는 SSOT.
const SEMANTIC_EDGE_KINDS: [&str; 6] = [
    "addresses",
    "resolved_by",
    "uses",
    "about",
    "tried",
    "solves",
];

/// 청크 id("path#idx") → 그래프 문서 노드 id("doc:path").
fn doc_node_id(chunk_or_path: &str) -> String {
    let path = chunk_or_path
        .rsplit_once('#')
        .map_or(chunk_or_path, |(p, _)| p);
    format!("doc:{path}")
}

async fn pg_count(db: &Client, sql: &str) -> Result<usize> {
    let row = db.query_one(sql, &[]).await?;
    let n: i64 = row.get(0);
    Ok(usize::try_from(n).unwrap_or(0))
}

async fn count_node_kind(db: &Client, kind: &str) -> Result<usize> {
    let row = db
        .query_one("SELECT count(*) FROM node WHERE kind = $1;", &[&kind])
        .await?;
    let n: i64 = row.get(0);
    Ok(usize::try_from(n).unwrap_or(0))
}

async fn count_edge_kind(db: &Client, kind: &str) -> Result<usize> {
    let row = db
        .query_one("SELECT count(*) FROM edge WHERE kind = $1;", &[&kind])
        .await?;
    let n: i64 = row.get(0);
    Ok(usize::try_from(n).unwrap_or(0))
}

impl Store {
    // ── 접속 + 스키마 보장 ────────────────────────────────────────────────────

    /// PostgreSQL 접속 + pgvector + node/edge 그래프 스키마 초기화.
    pub async fn open(dsn: &str) -> Result<Self> {
        let (client, conn) = tokio_postgres::connect(dsn, NoTls)
            .await
            .context("postgres connect")?;
        // 백그라운드 커넥션 드라이버 spawn.
        tokio::spawn(async move {
            if let Err(e) = conn.await {
                eprintln!("postgres connection error: {e}");
            }
        });

        client
            .batch_execute(
                "CREATE EXTENSION IF NOT EXISTS vector;
                 CREATE TABLE IF NOT EXISTS document (
                     source_path text PRIMARY KEY,
                     origin      text NOT NULL DEFAULT '',
                     project     text NOT NULL DEFAULT '',
                     kind        text NOT NULL DEFAULT '',
                     title       text,
                     tags        text[] NOT NULL DEFAULT '{}',
                     sha         text NOT NULL DEFAULT '',
                     extracted_sha text NOT NULL DEFAULT '',
                     updated_at  timestamptz NOT NULL DEFAULT now()
                 );
                 CREATE TABLE IF NOT EXISTS chunk (
                     id          text PRIMARY KEY,
                     source_path text NOT NULL REFERENCES document(source_path) ON DELETE CASCADE,
                     content     text NOT NULL DEFAULT '',
                     embedding   vector(1024),
                     origin      text NOT NULL DEFAULT '',
                     project     text NOT NULL DEFAULT '',
                     kind        text NOT NULL DEFAULT '',
                     chunk_idx   int  NOT NULL DEFAULT 0,
                     tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
                 );
                 CREATE INDEX IF NOT EXISTS chunk_hnsw ON chunk USING hnsw (embedding vector_cosine_ops);
                 CREATE INDEX IF NOT EXISTS chunk_gin  ON chunk USING gin (tsv);
                 CREATE TABLE IF NOT EXISTS node (
                     id      text PRIMARY KEY,
                     kind    text NOT NULL,
                     label   text NOT NULL DEFAULT '',
                     outcome text
                 );
                 CREATE TABLE IF NOT EXISTS edge (
                     src  text NOT NULL,
                     dst  text NOT NULL,
                     kind text NOT NULL,
                     PRIMARY KEY (src, dst, kind)
                 );
                 CREATE INDEX IF NOT EXISTS edge_src ON edge(src);
                 CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst);
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS extracted_sha text NOT NULL DEFAULT '';
                 ALTER TABLE document ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
                 CREATE INDEX IF NOT EXISTS document_updated ON document(updated_at DESC);
                 -- claim: 시간축 사실 권위(Graphiti 무효화 + external-kb-bot claims, 개인 규모 축소).
                 --   (subject,predicate) 의 현재값 = superseded_at IS NULL. 새 value 오면 옛것 봉인.
                 CREATE TABLE IF NOT EXISTS claim (
                     subject       text NOT NULL,
                     predicate     text NOT NULL,
                     value         text NOT NULL,
                     source_path   text NOT NULL,
                     valid_from    timestamptz NOT NULL,
                     superseded_at timestamptz,
                     embedding     vector(1024),
                     PRIMARY KEY (subject, predicate, valid_from)
                 );
                 CREATE INDEX IF NOT EXISTS claim_current ON claim(subject, predicate)
                     WHERE superseded_at IS NULL;
                 CREATE INDEX IF NOT EXISTS claim_hnsw ON claim USING hnsw (embedding vector_cosine_ops)
                     WHERE superseded_at IS NULL;",
            )
            .await
            .context("pgvector + graph schema")?;

        Ok(Self { db: client })
    }

    // ── document ─────────────────────────────────────────────────────────────

    pub async fn get_doc_sha(&self, path: &str) -> Result<Option<String>> {
        let rows = self
            .db
            .query("SELECT sha FROM document WHERE source_path = $1;", &[&path])
            .await?;
        Ok(rows.first().map(|r| r.get::<_, String>(0)))
    }

    pub async fn all_doc_paths(&self) -> Result<Vec<String>> {
        let rows = self
            .db
            .query("SELECT source_path FROM document;", &[])
            .await?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// 문서 mtime — claim 의 `valid_from`(시간축 정렬키). 없으면 now()(graceful).
    pub async fn doc_updated_at(&self, path: &str) -> Result<SystemTime> {
        let rows = self
            .db
            .query(
                "SELECT updated_at FROM document WHERE source_path = $1;",
                &[&path],
            )
            .await?;
        Ok(rows
            .first()
            .map_or_else(SystemTime::now, |r| r.get::<_, SystemTime>(0)))
    }

    /// 내용 변화 없는(sha 동일) 문서의 최근성만 갱신 — 재임베딩 없이 mtime backfill.
    /// 최신우선 정렬키(`updated_at`)가 기존 문서에도 채워지게 한다.
    pub async fn set_updated_at(&self, path: &str, updated_at: SystemTime) -> Result<()> {
        self.db
            .execute(
                "UPDATE document SET updated_at = $2
                 WHERE source_path = $1 AND updated_at IS DISTINCT FROM $2;",
                &[&path, &updated_at],
            )
            .await
            .context("touch updated_at")?;
        Ok(())
    }

    /// 최신순 문서 top-N — 본문 전체(청크 결합). 최신우선/supersede 브리핑의 회수.
    /// 의미유사도가 아니라 `updated_at` 내림차순 = "최근에 바뀐 지식이 위".
    pub async fn recent_docs(
        &self,
        limit: i64,
        exclude_origins: &[String],
    ) -> Result<Vec<RecentDoc>> {
        let rows = self
            .db
            .query(
                "SELECT d.source_path, d.project,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                 FROM document d
                 JOIN chunk c ON c.source_path = d.source_path
                 WHERE NOT (d.origin = ANY($2))
                 GROUP BY d.source_path, d.project, d.updated_at
                 ORDER BY d.updated_at DESC
                 LIMIT $1;",
                &[&limit, &exclude_origins],
            )
            .await
            .context("recent docs")?;
        Ok(rows
            .iter()
            .map(|r| RecentDoc {
                source_path: r.get(0),
                project: r.get(1),
                content: r.get(2),
            })
            .collect())
    }

    /// 문서↔문서 관계 — **구체** 시맨틱 노드(concept·tool·problem·solution)를
    /// 공유하는 다른 문서를 공유개수 내림차순으로. Obsidian relates_to 투영의 근거.
    /// 그래프(edge)에서 2-hop: doc → (공유 dst) ← otherDoc.
    /// `project:`/`topic:` 는 제외 — 같은 프로젝트/흔한 태그는 전부를 연결해 헤어볼이 됨.
    /// 최소 2개 이상 공유해야 링크(우연한 1개 겹침 노이즈 컷).
    pub async fn related_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let doc_id = doc_node_id(source_path);
        let rows = self
            .db
            .query(
                "WITH self_nodes AS (
                     SELECT dst FROM edge WHERE src = $1
                     AND dst NOT LIKE 'project:%' AND dst NOT LIKE 'topic:%'
                 )
                 SELECT e.src, count(*) AS shared
                 FROM edge e JOIN self_nodes sn ON e.dst = sn.dst
                 WHERE e.src <> $1 AND e.src LIKE 'doc:%'
                 GROUP BY e.src
                 ORDER BY shared DESC, e.src ASC
                 LIMIT $2;",
                &[&doc_id, &limit],
            )
            .await
            .context("related docs")?;
        // 'doc:<source_path>' → source_path 복원
        Ok(rows
            .iter()
            .map(|r| {
                let id: String = r.get(0);
                id.strip_prefix("doc:").unwrap_or(&id).to_owned()
            })
            .collect())
    }

    /// GraphRAG 회수: 한 문서와 **구체 concept/tool 을 공유**하는 연결문서 top-N 의 본문.
    /// 벡터가 노이즈에 묻은 정답을, 그래프(개념 연결)로 끌어올린다. project/topic 제외.
    pub async fn related_doc_content(
        &self,
        source_path: &str,
        limit: i64,
    ) -> Result<Vec<RecentDoc>> {
        let doc_id = doc_node_id(source_path);
        let rows = self
            .db
            .query(
                "WITH self_nodes AS (
                     SELECT dst FROM edge WHERE src = $1
                     AND dst NOT LIKE 'project:%' AND dst NOT LIKE 'topic:%'
                 ),
                 ranked AS (
                     SELECT e.src AS doc_node, count(*) AS shared
                     FROM edge e JOIN self_nodes sn ON e.dst = sn.dst
                     WHERE e.src <> $1 AND e.src LIKE 'doc:%'
                     GROUP BY e.src ORDER BY shared DESC LIMIT $2
                 )
                 SELECT d.source_path, d.project,
                        string_agg(c.content, E'\n' ORDER BY c.chunk_idx) AS content
                 FROM ranked r
                 JOIN document d ON ('doc:' || d.source_path) = r.doc_node
                 JOIN chunk c ON c.source_path = d.source_path
                 GROUP BY d.source_path, d.project, r.shared
                 ORDER BY r.shared DESC;",
                &[&doc_id, &limit],
            )
            .await
            .context("related doc content")?;
        Ok(rows
            .iter()
            .map(|r| RecentDoc {
                source_path: r.get(0),
                project: r.get(1),
                content: r.get(2),
            })
            .collect())
    }

    /// 같은 프로젝트의 최신 다른 문서 — 고립 문서(concept 겹침 0)용 fallback 링크.
    /// concept 기반 링크가 없을 때만 보충해 orphan 을 막되, mesh 가 되진 않게 소수만.
    pub async fn recent_project_docs(&self, source_path: &str, limit: i64) -> Result<Vec<String>> {
        let rows = self
            .db
            .query(
                "SELECT d2.source_path FROM document d1
                 JOIN document d2 ON d2.project = d1.project
                     AND d2.source_path <> d1.source_path
                 WHERE d1.source_path = $1 AND d1.project <> ''
                 ORDER BY d2.updated_at DESC
                 LIMIT $2;",
                &[&source_path, &limit],
            )
            .await
            .context("recent project docs")?;
        Ok(rows.iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// 시간축 사실 claim upsert + supersede. 같은 `(subject,predicate)`의 옛 value 는
    /// `superseded_at` 봉인, 최신 `valid_from` 행만 현재(NULL). 멱등(같은 행 재적재 무해).
    /// gemma 추가호출 0 — extract 가 이미 뽑은 claims 를 그대로 받는다.
    pub async fn upsert_claim(
        &self,
        subject: &str,
        predicate: &str,
        value: &str,
        source_path: &str,
        valid_from: SystemTime,
        embedding: &[f32],
    ) -> Result<()> {
        let vec = Vector::from(embedding.to_vec());
        self.db
            .execute(
                "INSERT INTO claim (subject, predicate, value, source_path, valid_from, embedding)
                 VALUES ($1, $2, $3, $4, $5, $6)
                 ON CONFLICT (subject, predicate, valid_from) DO UPDATE SET
                     value = EXCLUDED.value, source_path = EXCLUDED.source_path,
                     embedding = EXCLUDED.embedding;",
                &[
                    &subject,
                    &predicate,
                    &value,
                    &source_path,
                    &valid_from,
                    &vec,
                ],
            )
            .await
            .context("insert claim")?;
        // 최신 valid_from 미만은 모두 봉인, 최신 1행만 현재로.
        self.db
            .execute(
                "UPDATE claim c SET superseded_at = m.mx
                 FROM (SELECT subject, predicate, max(valid_from) AS mx FROM claim
                       WHERE subject = $1 AND predicate = $2 GROUP BY subject, predicate) m
                 WHERE c.subject = m.subject AND c.predicate = m.predicate
                   AND c.valid_from < m.mx AND c.superseded_at IS DISTINCT FROM m.mx;",
                &[&subject, &predicate],
            )
            .await
            .context("seal old claims")?;
        self.db
            .execute(
                "UPDATE claim SET superseded_at = NULL
                 WHERE subject = $1 AND predicate = $2 AND superseded_at IS NOT NULL
                   AND valid_from = (SELECT max(valid_from) FROM claim
                                     WHERE subject = $1 AND predicate = $2);",
                &[&subject, &predicate],
            )
            .await
            .context("unseal latest claim")?;
        Ok(())
    }

    /// **현재** claim(superseded_at IS NULL)을 최신순(valid_from desc) top-k. 브리핑 권위 주입용.
    pub async fn recent_claims(&self, k: i64) -> Result<Vec<(String, String, String)>> {
        let rows = self
            .db
            .query(
                "SELECT subject, predicate, value FROM claim
                 WHERE superseded_at IS NULL
                 ORDER BY valid_from DESC
                 LIMIT $1;",
                &[&k],
            )
            .await
            .context("recent claims")?;
        Ok(rows
            .iter()
            .map(|r| {
                (
                    r.get::<_, String>(0),
                    r.get::<_, String>(1),
                    r.get::<_, String>(2),
                )
            })
            .collect())
    }

    /// 질의 임베딩 → **현재** claim(superseded_at IS NULL) top-k. 권위 회수.
    pub async fn current_claims(
        &self,
        query_emb: &[f32],
        k: i64,
    ) -> Result<Vec<(String, String, String)>> {
        let vec = Vector::from(query_emb.to_vec());
        let rows = self
            .db
            .query(
                "SELECT subject, predicate, value FROM claim
                 WHERE superseded_at IS NULL AND embedding IS NOT NULL
                 ORDER BY embedding <=> $1
                 LIMIT $2;",
                &[&vec, &k],
            )
            .await
            .context("current claims")?;
        Ok(rows
            .iter()
            .map(|r| {
                (
                    r.get::<_, String>(0),
                    r.get::<_, String>(1),
                    r.get::<_, String>(2),
                )
            })
            .collect())
    }

    /// document upsert + project/topic 노드 + in_project/tagged 엣지 재생성(멱등).
    /// `updated_at` = 소스 파일 mtime(진짜 최근성 신호) — 최신우선 회수의 정렬키.
    pub async fn upsert_document(
        &self,
        front: &FrontMatter,
        sha: &str,
        updated_at: SystemTime,
    ) -> Result<()> {
        let path = &front.source_path;
        let title_ref: Option<&str> = front.title.as_deref();
        self.db
            .execute(
                "INSERT INTO document (source_path, origin, project, kind, title, tags, sha, updated_at)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (source_path) DO UPDATE SET
                     origin = EXCLUDED.origin, project = EXCLUDED.project, kind = EXCLUDED.kind,
                     title = EXCLUDED.title, tags = EXCLUDED.tags, sha = EXCLUDED.sha,
                     updated_at = EXCLUDED.updated_at;",
                &[
                    path,
                    &front.origin,
                    &front.project,
                    &front.kind,
                    &title_ref,
                    &front.tags,
                    &sha,
                    &updated_at,
                ],
            )
            .await
            .context("upsert document")?;

        let doc_id = doc_node_id(path);

        // project 노드 + in_project 엣지
        if !front.project.is_empty() {
            let pid = format!("project:{}", front.project);
            self.upsert_node(&pid, "project", &front.project, None)
                .await?;
            self.upsert_edge(&doc_id, &pid, "in_project").await?;
        }

        // tagged: 기존 제거 후 재생성(멱등)
        self.db
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind = 'tagged';",
                &[&doc_id],
            )
            .await?;
        for tag in &front.tags {
            let tid = format!("topic:{tag}");
            self.upsert_node(&tid, "topic", tag, None).await?;
            self.upsert_edge(&doc_id, &tid, "tagged").await?;
        }
        Ok(())
    }

    pub async fn delete_doc_chunks(&self, path: &str) -> Result<()> {
        self.db
            .execute("DELETE FROM chunk WHERE source_path = $1;", &[&path])
            .await?;
        Ok(())
    }

    /// 문서 + 청크(CASCADE) + 그래프 엣지/attempt 노드 제거(prune).
    pub async fn delete_document(&self, path: &str) -> Result<()> {
        self.db
            .execute("DELETE FROM document WHERE source_path = $1;", &[&path])
            .await?;
        let doc_id = doc_node_id(path);
        self.db
            .execute("DELETE FROM edge WHERE src = $1 OR dst = $1;", &[&doc_id])
            .await?;
        self.db
            .execute(
                "DELETE FROM node WHERE id LIKE $1;",
                &[&format!("attempt:{path}#%")],
            )
            .await?;
        Ok(())
    }

    // ── chunk (임베딩) ───────────────────────────────────────────────────────

    pub async fn upsert_chunk(&self, d: &Doc) -> Result<()> {
        let vec = Vector::from(d.embedding.clone());
        let idx = i32::try_from(d.chunk_idx).unwrap_or(i32::MAX);
        self.db
            .execute(
                "INSERT INTO chunk (id, source_path, content, embedding, origin, project, kind, chunk_idx)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                 ON CONFLICT (id) DO UPDATE SET
                     content = EXCLUDED.content, embedding = EXCLUDED.embedding, origin = EXCLUDED.origin,
                     project = EXCLUDED.project, kind = EXCLUDED.kind, chunk_idx = EXCLUDED.chunk_idx;",
                &[
                    &d.id, &d.front.source_path, &d.content, &vec,
                    &d.front.origin, &d.front.project, &d.front.kind, &idx,
                ],
            )
            .await
            .context("upsert chunk")?;
        Ok(())
    }

    // ── 회수 ──────────────────────────────────────────────────────────────────

    pub async fn vector_search(&self, vec: &[f32], k: usize) -> Result<Vec<Hit>> {
        let qvec = Vector::from(vec.to_vec());
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db
            .query(
                "SELECT id, content, origin, project, source_path, (embedding <=> $1)::float4 AS dist
                 FROM chunk ORDER BY embedding <=> $1 LIMIT $2;",
                &[&qvec, &k_i64],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    pub async fn text_search(&self, query: &str, k: usize) -> Result<Vec<Hit>> {
        let k_i64 = i64::try_from(k).unwrap_or(i64::MAX);
        let rows = self
            .db
            .query(
                "SELECT id, content, origin, project, source_path,
                        ts_rank(tsv, plainto_tsquery('simple', $1))::float4 AS dist
                 FROM chunk WHERE tsv @@ plainto_tsquery('simple', $1)
                 ORDER BY dist DESC LIMIT $2;",
                &[&query, &k_i64],
            )
            .await?;
        Ok(rows.iter().map(row_to_hit).collect())
    }

    pub async fn count(&self) -> Result<usize> {
        pg_count(&self.db, "SELECT count(*) FROM chunk;").await
    }

    pub async fn all_meta(&self) -> Result<Vec<Meta>> {
        let rows = self
            .db
            .query("SELECT origin, project, kind, source_path FROM chunk;", &[])
            .await?;
        Ok(rows
            .into_iter()
            .map(|r| Meta {
                origin: r.get(0),
                project: r.get(1),
                kind: r.get(2),
                source_path: r.get(3),
            })
            .collect())
    }

    /// 증분 extract — `extracted_sha` 가 현재 `sha` 와 다른 문서만(변경/신규). 본문 결합 반환.
    pub async fn docs_needing_extract(&self) -> Result<Vec<(String, String)>> {
        let rows = self
            .db
            .query(
                "SELECT source_path FROM document WHERE extracted_sha IS DISTINCT FROM sha;",
                &[],
            )
            .await?;
        let paths: Vec<String> = rows.iter().map(|r| r.get::<_, String>(0)).collect();
        let mut out = Vec::with_capacity(paths.len());
        for path in paths {
            let crows = self
                .db
                .query(
                    "SELECT content FROM chunk WHERE source_path = $1 ORDER BY chunk_idx ASC;",
                    &[&path],
                )
                .await?;
            let body = crows
                .into_iter()
                .map(|r| r.get::<_, String>(0))
                .collect::<Vec<_>>()
                .join("\n");
            out.push((path, body));
        }
        Ok(out)
    }

    /// 문서를 추출 완료로 표시(extracted_sha ← sha) — 다음 sync 부터 변경 전까지 skip.
    pub async fn mark_extracted(&self, doc_path: &str) -> Result<()> {
        self.db
            .execute(
                "UPDATE document SET extracted_sha = sha WHERE source_path = $1;",
                &[&doc_path],
            )
            .await?;
        Ok(())
    }

    // ── 그래프 헬퍼 (node/edge upsert) ─────────────────────────────────────────

    async fn upsert_node(
        &self,
        id: &str,
        kind: &str,
        label: &str,
        outcome: Option<&str>,
    ) -> Result<()> {
        self.db
            .execute(
                "INSERT INTO node (id, kind, label, outcome) VALUES ($1, $2, $3, $4)
                 ON CONFLICT (id) DO UPDATE SET label = EXCLUDED.label, outcome = EXCLUDED.outcome;",
                &[&id, &kind, &label, &outcome],
            )
            .await
            .context("upsert node")?;
        Ok(())
    }

    async fn upsert_edge(&self, src: &str, dst: &str, kind: &str) -> Result<()> {
        self.db
            .execute(
                "INSERT INTO edge (src, dst, kind) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING;",
                &[&src, &dst, &kind],
            )
            .await
            .context("upsert edge")?;
        Ok(())
    }

    // ── 시맨틱 노드 ────────────────────────────────────────────────────────────

    pub async fn upsert_problem(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("problem:{slug}"), "problem", text, None)
            .await
    }
    pub async fn upsert_solution(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("solution:{slug}"), "solution", text, None)
            .await
    }
    pub async fn upsert_tool(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("tool:{slug}"), "tool", text, None)
            .await
    }
    pub async fn upsert_concept(&self, slug: &str, text: &str) -> Result<()> {
        self.upsert_node(&format!("concept:{slug}"), "concept", text, None)
            .await
    }

    pub async fn upsert_attempt(
        &self,
        doc_path: &str,
        idx: usize,
        what: &str,
        outcome: &str,
    ) -> Result<()> {
        let id = format!("attempt:{doc_path}#{idx}");
        self.upsert_node(&id, "attempt", what, Some(outcome)).await
    }

    /// 이 문서의 시맨틱 엣지 + attempt 노드 제거(extract 멱등화).
    pub async fn clear_semantic_edges(&self, doc_path: &str) -> Result<()> {
        let doc_id = doc_node_id(doc_path);
        let kinds: Vec<&str> = SEMANTIC_EDGE_KINDS.to_vec();
        // doc → entity 엣지
        self.db
            .execute(
                "DELETE FROM edge WHERE src = $1 AND kind = ANY($2);",
                &[&doc_id, &kinds],
            )
            .await?;
        // 이 문서의 attempt 노드에 닿는 엣지(leads_to 등) + attempt 노드
        let attempt_like = format!("attempt:{doc_path}#%");
        self.db
            .execute(
                "DELETE FROM edge WHERE src LIKE $1 OR dst LIKE $1;",
                &[&attempt_like],
            )
            .await?;
        self.db
            .execute("DELETE FROM node WHERE id LIKE $1;", &[&attempt_like])
            .await?;
        Ok(())
    }

    // ── 시맨틱 엣지 (doc → entity) ─────────────────────────────────────────────

    pub async fn relate_doc_problem(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(
            &doc_node_id(doc_path),
            &format!("problem:{slug}"),
            "addresses",
        )
        .await
    }
    pub async fn relate_doc_solution(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(
            &doc_node_id(doc_path),
            &format!("solution:{slug}"),
            "resolved_by",
        )
        .await
    }
    pub async fn relate_doc_tool(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(&doc_node_id(doc_path), &format!("tool:{slug}"), "uses")
            .await
    }
    pub async fn relate_doc_concept(&self, doc_path: &str, slug: &str) -> Result<()> {
        self.upsert_edge(&doc_node_id(doc_path), &format!("concept:{slug}"), "about")
            .await
    }
    pub async fn relate_doc_attempt(&self, doc_path: &str, idx: usize) -> Result<()> {
        self.upsert_edge(
            &doc_node_id(doc_path),
            &format!("attempt:{doc_path}#{idx}"),
            "tried",
        )
        .await
    }

    // ── 그래프 회수 ────────────────────────────────────────────────────────────

    /// 구조 이웃(project/topic) — 청크의 문서에서 1-hop. 라벨 반환.
    pub async fn graph_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind IN ('in_project', 'tagged');",
                &[&doc_id],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// 시맨틱 이웃(problem/solution/tool/concept/attempt) — 문서에서 1-hop. 라벨 반환.
    pub async fn semantic_neighbors(&self, chunk_id: &str) -> Result<Vec<String>> {
        let doc_id = doc_node_id(chunk_id);
        let rows = self
            .db
            .query(
                "SELECT n.label FROM edge e JOIN node n ON n.id = e.dst
                 WHERE e.src = $1 AND e.kind = ANY($2);",
                &[&doc_id, &SEMANTIC_EDGE_KINDS.to_vec()],
            )
            .await?;
        Ok(rows.into_iter().map(|r| r.get::<_, String>(0)).collect())
    }

    /// lineage — 같은 문서 내 attempt[from] → attempt[to] (`leads_to`). 문제해결 서사 순서.
    pub async fn relate_leads_to(
        &self,
        doc_path: &str,
        from_idx: usize,
        to_idx: usize,
    ) -> Result<()> {
        let src = format!("attempt:{doc_path}#{from_idx}");
        let dst = format!("attempt:{doc_path}#{to_idx}");
        self.upsert_edge(&src, &dst, "leads_to").await
    }

    // ── 통계 / GC ─────────────────────────────────────────────────────────────

    pub async fn graph_stats(&self) -> Result<GraphStats> {
        Ok(GraphStats {
            documents: pg_count(&self.db, "SELECT count(*) FROM document;").await?,
            chunks: pg_count(&self.db, "SELECT count(*) FROM chunk;").await?,
            projects: count_node_kind(&self.db, "project").await?,
            topics: count_node_kind(&self.db, "topic").await?,
            edges: pg_count(&self.db, "SELECT count(*) FROM edge;").await?,
        })
    }

    pub async fn semantic_stats(&self) -> Result<SemanticStats> {
        Ok(SemanticStats {
            problems: count_node_kind(&self.db, "problem").await?,
            solutions: count_node_kind(&self.db, "solution").await?,
            tools: count_node_kind(&self.db, "tool").await?,
            concepts: count_node_kind(&self.db, "concept").await?,
            attempts: count_node_kind(&self.db, "attempt").await?,
            addresses: count_edge_kind(&self.db, "addresses").await?,
            resolved_by: count_edge_kind(&self.db, "resolved_by").await?,
            uses: count_edge_kind(&self.db, "uses").await?,
            about: count_edge_kind(&self.db, "about").await?,
            tried: count_edge_kind(&self.db, "tried").await?,
        })
    }

    /// 고아 시맨틱 노드 제거 — 엣지에서 참조되지 않는 entity 노드.
    pub async fn gc_orphans(&self) -> Result<GcStats> {
        let mut gc = GcStats::default();
        for kind in ["tool", "concept", "problem", "solution", "attempt"] {
            let n = self
                .db
                .execute(
                    "DELETE FROM node WHERE kind = $1
                       AND id NOT IN (SELECT src FROM edge UNION SELECT dst FROM edge);",
                    &[&kind],
                )
                .await?;
            let c = usize::try_from(n).unwrap_or(0);
            match kind {
                "tool" => gc.tool = c,
                "concept" => gc.concept = c,
                "problem" => gc.problem = c,
                "solution" => gc.solution = c,
                _ => gc.attempt = c,
            }
        }
        Ok(gc)
    }
}

fn row_to_hit(r: &tokio_postgres::Row) -> Hit {
    Hit {
        id: r.get(0),
        content: r.get(1),
        origin: r.get(2),
        project: r.get(3),
        source_path: r.get(4),
        dist: r.get(5),
    }
}
