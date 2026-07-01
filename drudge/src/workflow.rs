//! Workflow graph specs for host-side memory ingestion.
//!
//! This module is the Rust-side "LangGraph" contract: explicit nodes,
//! labelled edges, and graph validation. It does not execute hooks, call LLMs,
//! or replace the deterministic semantic graph in `graph.rs`.

use std::collections::BTreeSet;

/// State nodes in the memory-ingest workflow.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum WorkflowNode {
    SessionDiscovered,
    TranscriptPrepared,
    DistillRequested,
    ResolutionVerified,
    ResolutionRepaired,
    RememberRequested,
    DoneMarked,
    RetryMarked,
    Skipped,
    ResolutionEventRecorded,
    ReadinessProjected,
}

impl WorkflowNode {
    /// Stable event-facing name.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::SessionDiscovered => "session_discovered",
            Self::TranscriptPrepared => "transcript_prepared",
            Self::DistillRequested => "distill_requested",
            Self::ResolutionVerified => "resolution_verified",
            Self::ResolutionRepaired => "resolution_repaired",
            Self::RememberRequested => "remember_requested",
            Self::DoneMarked => "done_marked",
            Self::RetryMarked => "retry_marked",
            Self::Skipped => "skipped",
            Self::ResolutionEventRecorded => "resolution_event_recorded",
            Self::ReadinessProjected => "readiness_projected",
        }
    }
}

/// Label on a workflow edge.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WorkflowOutcome {
    Continue,
    Pass,
    Fail,
    Skip,
    Duplicate,
}

impl WorkflowOutcome {
    /// Stable event-facing name.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Continue => "continue",
            Self::Pass => "pass",
            Self::Fail => "fail",
            Self::Skip => "skip",
            Self::Duplicate => "duplicate",
        }
    }
}

/// Directed edge in a workflow graph.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WorkflowEdge {
    pub from: WorkflowNode,
    pub outcome: WorkflowOutcome,
    pub to: WorkflowNode,
}

impl WorkflowEdge {
    pub const fn new(from: WorkflowNode, outcome: WorkflowOutcome, to: WorkflowNode) -> Self {
        Self { from, outcome, to }
    }
}

/// Closed workflow graph definition.
#[derive(Debug, Clone, Copy)]
pub struct WorkflowGraph {
    pub name: &'static str,
    pub entry: WorkflowNode,
    pub nodes: &'static [WorkflowNode],
    pub edges: &'static [WorkflowEdge],
    pub terminals: &'static [WorkflowNode],
}

impl WorkflowGraph {
    /// Return the next nodes for a labelled transition.
    pub fn next(self, node: WorkflowNode, outcome: WorkflowOutcome) -> Vec<WorkflowNode> {
        self.edges
            .iter()
            .filter(|edge| edge.from == node && edge.outcome == outcome)
            .map(|edge| edge.to)
            .collect()
    }

    /// Validate graph shape without touching external state.
    pub fn validate(self) -> Vec<WorkflowIssue> {
        let node_set: BTreeSet<WorkflowNode> = self.nodes.iter().copied().collect();
        let mut issues = Vec::new();

        if !node_set.contains(&self.entry) {
            issues.push(WorkflowIssue::MissingEntry(self.entry));
        }

        for terminal in self.terminals {
            if !node_set.contains(terminal) {
                issues.push(WorkflowIssue::MissingTerminal(*terminal));
            }
        }

        for edge in self.edges {
            if !node_set.contains(&edge.from) {
                issues.push(WorkflowIssue::UnknownEdgeEndpoint {
                    edge: *edge,
                    endpoint: edge.from,
                });
            }
            if !node_set.contains(&edge.to) {
                issues.push(WorkflowIssue::UnknownEdgeEndpoint {
                    edge: *edge,
                    endpoint: edge.to,
                });
            }
        }

        for node in self.nodes {
            let has_outgoing = self.edges.iter().any(|edge| edge.from == *node);
            let is_terminal = self.terminals.contains(node);
            match (has_outgoing, is_terminal) {
                (true, true) => issues.push(WorkflowIssue::TerminalHasOutgoing(*node)),
                (false, false) => issues.push(WorkflowIssue::NonTerminalDeadEnd(*node)),
                (true, false) | (false, true) => {}
            }
        }

        issues
    }
}

/// Shape issue found in a workflow graph.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WorkflowIssue {
    MissingEntry(WorkflowNode),
    MissingTerminal(WorkflowNode),
    UnknownEdgeEndpoint {
        edge: WorkflowEdge,
        endpoint: WorkflowNode,
    },
    TerminalHasOutgoing(WorkflowNode),
    NonTerminalDeadEnd(WorkflowNode),
}

pub const MEMORY_INGEST_NODES: &[WorkflowNode] = &[
    WorkflowNode::SessionDiscovered,
    WorkflowNode::TranscriptPrepared,
    WorkflowNode::DistillRequested,
    WorkflowNode::ResolutionVerified,
    WorkflowNode::ResolutionRepaired,
    WorkflowNode::RememberRequested,
    WorkflowNode::DoneMarked,
    WorkflowNode::RetryMarked,
    WorkflowNode::Skipped,
    WorkflowNode::ResolutionEventRecorded,
    WorkflowNode::ReadinessProjected,
];

pub const MEMORY_INGEST_EDGES: &[WorkflowEdge] = &[
    WorkflowEdge::new(
        WorkflowNode::SessionDiscovered,
        WorkflowOutcome::Continue,
        WorkflowNode::TranscriptPrepared,
    ),
    WorkflowEdge::new(
        WorkflowNode::TranscriptPrepared,
        WorkflowOutcome::Continue,
        WorkflowNode::DistillRequested,
    ),
    WorkflowEdge::new(
        WorkflowNode::TranscriptPrepared,
        WorkflowOutcome::Skip,
        WorkflowNode::Skipped,
    ),
    WorkflowEdge::new(
        WorkflowNode::DistillRequested,
        WorkflowOutcome::Continue,
        WorkflowNode::ResolutionVerified,
    ),
    WorkflowEdge::new(
        WorkflowNode::DistillRequested,
        WorkflowOutcome::Skip,
        WorkflowNode::Skipped,
    ),
    WorkflowEdge::new(
        WorkflowNode::ResolutionVerified,
        WorkflowOutcome::Pass,
        WorkflowNode::RememberRequested,
    ),
    WorkflowEdge::new(
        WorkflowNode::ResolutionVerified,
        WorkflowOutcome::Fail,
        WorkflowNode::ResolutionRepaired,
    ),
    WorkflowEdge::new(
        WorkflowNode::ResolutionRepaired,
        WorkflowOutcome::Pass,
        WorkflowNode::RememberRequested,
    ),
    WorkflowEdge::new(
        WorkflowNode::ResolutionRepaired,
        WorkflowOutcome::Fail,
        WorkflowNode::RetryMarked,
    ),
    WorkflowEdge::new(
        WorkflowNode::RememberRequested,
        WorkflowOutcome::Pass,
        WorkflowNode::DoneMarked,
    ),
    WorkflowEdge::new(
        WorkflowNode::RememberRequested,
        WorkflowOutcome::Duplicate,
        WorkflowNode::DoneMarked,
    ),
    WorkflowEdge::new(
        WorkflowNode::RememberRequested,
        WorkflowOutcome::Fail,
        WorkflowNode::RetryMarked,
    ),
    WorkflowEdge::new(
        WorkflowNode::DoneMarked,
        WorkflowOutcome::Continue,
        WorkflowNode::ResolutionEventRecorded,
    ),
    WorkflowEdge::new(
        WorkflowNode::RetryMarked,
        WorkflowOutcome::Continue,
        WorkflowNode::ResolutionEventRecorded,
    ),
    WorkflowEdge::new(
        WorkflowNode::Skipped,
        WorkflowOutcome::Continue,
        WorkflowNode::ResolutionEventRecorded,
    ),
    WorkflowEdge::new(
        WorkflowNode::ResolutionEventRecorded,
        WorkflowOutcome::Continue,
        WorkflowNode::ReadinessProjected,
    ),
];

pub const MEMORY_INGEST_TERMINALS: &[WorkflowNode] = &[WorkflowNode::ReadinessProjected];

/// SSOT graph for the host-side session memory ingest loop.
pub const MEMORY_INGEST_GRAPH: WorkflowGraph = WorkflowGraph {
    name: "memory_ingest",
    entry: WorkflowNode::SessionDiscovered,
    nodes: MEMORY_INGEST_NODES,
    edges: MEMORY_INGEST_EDGES,
    terminals: MEMORY_INGEST_TERMINALS,
};

/// Return the canonical memory-ingest workflow graph.
pub const fn memory_ingest_graph() -> WorkflowGraph {
    MEMORY_INGEST_GRAPH
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]

    use super::{WorkflowNode, WorkflowOutcome, memory_ingest_graph};

    #[test]
    fn memory_ingest_graph_is_well_formed() {
        let graph = memory_ingest_graph();
        let issues = graph.validate();
        assert!(issues.is_empty(), "workflow issues: {issues:?}");
    }

    #[test]
    fn memory_ingest_graph_counts_are_intentional() {
        let graph = memory_ingest_graph();
        assert_eq!(graph.nodes.len(), 11);
        assert_eq!(graph.edges.len(), 16);
        assert_eq!(graph.terminals.len(), 1);
    }

    #[test]
    fn resolution_failure_routes_to_repair_then_retry() {
        let graph = memory_ingest_graph();
        assert_eq!(
            graph.next(WorkflowNode::ResolutionVerified, WorkflowOutcome::Fail),
            vec![WorkflowNode::ResolutionRepaired]
        );
        assert_eq!(
            graph.next(WorkflowNode::ResolutionRepaired, WorkflowOutcome::Fail),
            vec![WorkflowNode::RetryMarked]
        );
    }

    #[test]
    fn remember_duplicate_is_a_done_marker() {
        let graph = memory_ingest_graph();
        assert_eq!(
            graph.next(WorkflowNode::RememberRequested, WorkflowOutcome::Duplicate),
            vec![WorkflowNode::DoneMarked]
        );
    }
}
