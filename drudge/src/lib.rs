#![recursion_limit = "256"]

//! ohmyboring personal RAG — library surface.
//!
//! The binary (`src/main.rs`) uses this library. Integration tests under
//! `tests/` also link against it so they can exercise the Storage Layer and
//! other kernel contracts directly against a live Postgres backend.
pub mod anchor;
pub mod anchor_hash;
pub mod approved;
pub mod ask;
pub mod audit;
pub mod claim_source;
pub mod code_index;
pub mod config;
pub mod frontmatter;
pub mod graph;
pub mod ingest;
pub mod llm;
pub mod pg_detail;
pub mod pii;
pub mod projects;
pub mod redact;
pub mod retrieve;
pub mod rules;
pub mod serve;
pub mod split_subjects;
pub mod store;
pub mod vault;
pub mod wiki_recall;
pub mod workflow;
