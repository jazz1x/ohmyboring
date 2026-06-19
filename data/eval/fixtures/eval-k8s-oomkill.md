---
id: eval-k8s-oomkill
title: Diagnosing a Kubernetes pod that kept getting OOMKilled
kind: note
origin: personal
date: "2026-02-09"
tags:
  - kubernetes
  - memory
  - reliability
tools:
  - kubectl
  - jvm
  - prometheus
concepts:
  - memory limits
  - oom killer
  - heap sizing
relates_to: []
summary: A JVM pod was OOMKilled because the container memory limit ignored off-heap usage; raising the limit and capping the heap stopped the restarts.
---

A pod kept restarting and `kubectl describe pod` showed `Last State: Terminated,
Reason: OOMKilled`. The JVM heap was set to the full container limit, leaving no
headroom for off-heap memory: thread stacks, metaspace, and direct buffers.

When real usage pushed total RSS past the container `memory.limit`, the kernel OOM
killer terminated the process even though the heap itself was fine. The fix was to
set the JVM max heap to about 70% of the container limit and raise the limit itself
so off-heap memory has room. Prometheus container-memory metrics confirmed RSS now
sits comfortably under the limit.

The lesson: a container memory limit caps total process memory, not just the heap.
Always size the heap below the limit and leave headroom for off-heap allocations.
