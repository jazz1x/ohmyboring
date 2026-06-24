---
id: eval-kafka-rebalance
title: Kafka consumers rebalance in a loop and reprocess the same messages
kind: note
origin: personal
date: "2026-04-02"
tags:
  - kafka
  - consumer-group
  - rebalance
  - idempotency
tools:
  - kafka
  - librdkafka
concepts:
  - max.poll.interval.ms
  - heartbeat vs poll timeout
  - rebalance storm
relates_to: []
summary: Slow per-message processing exceeded max.poll.interval.ms, so the broker evicted the consumer and triggered a rebalance storm with duplicate processing; shrink the batch / move work off the poll loop and make the handler idempotent.
---

# Kafka consumers rebalance in a loop and reprocess messages

A consumer group thrashed: partitions kept getting reassigned, throughput collapsed, and the same messages were processed more than once. Logs were full of "leaving group" / "rejoining group".

Root cause: per-message handling took longer than `max.poll.interval.ms`, so the broker decided the consumer was dead and kicked it from the group. Each eviction triggered a rebalance, which paused everyone, reassigned partitions, and replayed uncommitted offsets — hence the duplicates. Background heartbeats kept the session alive, which masked it as a poll-timeout rather than a crash.

Fix on two axes. Stop the eviction: reduce `max.poll.records` so a batch finishes within the interval, or hand work to a worker pool and only commit after it completes (raising `max.poll.interval.ms` is a last resort). Make duplicates harmless: process idempotently (dedupe by message key / offset) so an at-least-once redelivery after a rebalance can't double-apply.
