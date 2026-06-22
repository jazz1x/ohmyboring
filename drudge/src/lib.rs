//! ohmyboring personal RAG — library surface.
//!
//! The binary (`src/main.rs`) uses this library. Integration tests under
//! `tests/` also link against it so they can exercise the Storage Layer and
//! other kernel contracts directly against a live Postgres backend.
pub mod ask;
pub mod audit;
pub mod config;
pub mod frontmatter;
pub mod graph;
pub mod ingest;
pub mod llm;
pub mod redact;
pub mod renumber;
pub mod retrieve;
pub mod serve;
pub mod store;
pub mod vault;
pub mod wiki_recall;
