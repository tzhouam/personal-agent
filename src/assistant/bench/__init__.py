"""PA-Mix — the benchmark harness (doc/BENCHMARKS.md).

A composition-root package (like `cli/`): it may import both `agent/` and
`platform/`. Nothing in the daemon or pipeline imports it — entry is the
`assistant bench` CLI only, guarded by `BENCH_ENABLED`, and rollback is
deleting this package plus the no-op-by-default `executor_override` seam."""
