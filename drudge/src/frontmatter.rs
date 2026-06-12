//! Frontmatter 엔티티 — raw `.md` 를 경계에서 1회 typed 로 parse (parse-don't-validate).
//! YAML 프론트매터(`--- ... ---`)가 있으면 파싱, 없으면 경로에서 origin/kind/project 추론.
//! 파싱 실패는 silent fallback 아니라 `Result` 레일로 흘림(ROP) — 호출부가 graceful boundary 결정.
use anyhow::Result;
use serde::{Deserialize, Serialize};

/// 적재 문서의 구조화 메타 — audit·필터·graph 엣지의 근거(SSOT).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct FrontMatter {
    pub origin: String, // personal | company
    pub project: String,
    pub date: String,
    pub kind: String, // note | memory | doc  (enrich 가 생성하는 값; "session" 은 예약어로만 존재)
    pub source_path: String,
    pub title: Option<String>,
    pub tags: Vec<String>,
}

/// origin=company 로 분류할 경로 토큰 — env `DRUDGE_COMPANY_SUBSTR`(':' 구분).
/// 기본 빈값 = 토큰 없음 → 모든 문서 origin=personal (회사 개념 미사용).
/// 다운스트림은 `.env` 에 토큰만 꽂으면 코드 수정 없이 회사 레이어가 켜진다(SSOT).
pub fn company_substrs() -> Vec<String> {
    std::env::var("DRUDGE_COMPANY_SUBSTR")
        .unwrap_or_default()
        .split(':')
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect()
}

/// 경로가 설정된 회사 토큰 중 하나라도 포함하면 true. 토큰 미설정이면 항상 false.
pub fn is_company_path(path: &str) -> bool {
    company_substrs().iter().any(|s| path.contains(s))
}

impl FrontMatter {
    /// 비어있는 필드를 경로 휴리스틱으로 채움(typed 값 구성의 일부).
    fn enrich(&mut self, path: &str) {
        if self.source_path.is_empty() {
            self.source_path.push_str(path);
        }
        if self.origin.is_empty() {
            self.origin.push_str(if is_company_path(path) {
                "company"
            } else {
                "personal"
            });
        }
        if self.kind.is_empty() {
            self.kind.push_str(if path.contains("/notes/") {
                "note"
            } else if path.contains("/memory") {
                "memory"
            } else {
                "doc"
            });
        }
        if self.project.is_empty() {
            self.project = derive_project(path);
        }
    }
}

/// `…/projects/<proj>/…` 의 `<proj>` 또는 부모 디렉터리명.
fn derive_project(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
    if let Some(i) = parts.iter().position(|&p| p == "projects")
        && let Some(proj) = parts.get(i + 1)
    {
        return (*proj).to_owned();
    }
    // fallback: 파일의 부모 디렉터리
    parts
        .iter()
        .rev()
        .nth(1)
        .map_or_else(|| "unknown".to_owned(), |s| (*s).to_owned())
}

/// raw `.md` → (frontmatter, body). 프론트매터 YAML 파싱 실패 시 Err.
pub fn parse(raw: &str, fallback_path: &str) -> Result<(FrontMatter, String)> {
    let raw = raw.strip_prefix('\u{feff}').unwrap_or(raw); // BOM 제거
    let mut front = if let Some(rest) = raw.strip_prefix("---\n") {
        if let Some(end) = rest.find("\n---\n") {
            let yaml = &rest[..end];
            let body = rest[end + 5..].to_owned();
            let front: FrontMatter = serde_yaml::from_str(yaml)?;
            front_enriched(front, fallback_path, &body)
        } else {
            front_enriched(FrontMatter::default(), fallback_path, raw)
        }
    } else {
        front_enriched(FrontMatter::default(), fallback_path, raw)
    };
    let body = std::mem::take(&mut front.1);
    Ok((front.0, body))
}

fn front_enriched(mut fm: FrontMatter, path: &str, body: &str) -> (FrontMatter, String) {
    fm.enrich(path);
    (fm, body.trim_start().to_owned())
}

/// FrontMatter + body → `.md` 텍스트(`--- yaml --- body`).
#[allow(dead_code)] // S8: distill 훅 출력 frontmatter 화에서 사용
pub fn render(front: &FrontMatter, body: &str) -> Result<String> {
    let yaml = serde_yaml::to_string(front)?;
    Ok(format!("---\n{yaml}---\n{body}"))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::{FrontMatter, parse, render};

    #[test]
    fn parse_with_frontmatter() {
        let raw = "---\norigin: company\nproject: demo\ntags:\n  - rust\n  - rop\n---\n본문 시작\n둘째 줄";
        let (fm, body) = parse(raw, "/x/y.md").unwrap();
        assert_eq!(fm.origin, "company");
        assert_eq!(fm.project, "demo");
        assert_eq!(fm.tags, vec!["rust", "rop"]);
        assert_eq!(body, "본문 시작\n둘째 줄");
    }

    #[test]
    fn parse_without_frontmatter_infers_from_path() {
        let (fm, body) = parse(
            "그냥 본문",
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md",
        )
        .unwrap();
        assert_eq!(fm.origin, "personal"); // 회사 토큰 미설정 → personal
        assert_eq!(fm.kind, "note"); // /notes/ 경로
        assert_eq!(fm.project, "oh-my-boring"); // projects/<proj>
        assert_eq!(
            fm.source_path,
            "/Users/x/.claude/projects/oh-my-boring/data/notes/s.md"
        );
        assert_eq!(body, "그냥 본문");
    }

    #[test]
    fn round_trip_render_then_parse() {
        let fm = FrontMatter {
            origin: "personal".to_owned(),
            project: "oh-my-boring".to_owned(),
            kind: "note".to_owned(),
            tags: vec!["a".to_owned(), "b".to_owned()],
            ..Default::default()
        };
        let md = render(&fm, "본문").unwrap();
        let (back, body) = parse(&md, "/p.md").unwrap();
        assert_eq!(back.origin, "personal");
        assert_eq!(back.project, "oh-my-boring");
        assert_eq!(back.tags, vec!["a", "b"]);
        assert_eq!(body, "본문");
    }

    #[test]
    fn malformed_yaml_is_error_not_silent() {
        // ROP: 깨진 프론트매터는 Err 로 흐름(침묵 fallback 아님)
        let raw = "---\norigin: [unclosed\n---\n본문";
        assert!(parse(raw, "/p.md").is_err());
    }
}
