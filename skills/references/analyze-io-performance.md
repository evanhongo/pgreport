# Analyze I/O Performance

**Commands:** `cache-hit-rate` → `disk-io-analysis` → `temp-file-usage` → `bgwriter-stats` / `checkpointer-stats` → `io-statistics` → `wait-events` → `health-check`

## Steps

1. `pgreport cache-hit-rate`. Baseline buffer cache hit ratio for the current DB. < 95% = memory pressure or cold cache.
2. `pgreport disk-io-analysis --analysis-type all`. Buffer pool + per-relation table/index I/O from `pg_statio_*`. Drill into one area with `--analysis-type buffer_pool|tables|indexes`.
3. `pgreport temp-file-usage --threshold-gb 0`. Databases spilling sort/hash to disk; `--threshold-gb 1` filters to only meaningful spills.
4. `pgreport bgwriter-stats` and `pgreport checkpointer-stats` (PG17+). Checkpoint write/sync timing + `checkpointer_status` flag.
5. `pgreport io-statistics`. Returns the `pg_stat_database` summary AND (on PG16+) the detailed `pg_stat_io` breakdown by backend type / object / context — pinpoints slow read/write contexts. On older servers the detail block is reported as skipped.
6. `pgreport table-stats --order-by seq_scans`. High seq scans on large tables = missing indexes.
7. `pgreport wait-events`. I/O wait types confirm disk is the bottleneck.
8. `pgreport health-check --verbose`. The `checks.settings` block surfaces `shared_buffers`, `work_mem`, `effective_cache_size`, and `checkpoint_completion_target` recommendations as part of the score, so you don't need a separate settings dump for I/O tuning.
9. Report with specific tuning recommendations.

## Common Fixes

| Issue | Fix |
|-------|-----|
| Low cache hit ratio | Increase `shared_buffers` (typically 25% RAM) |
| Temp files from sorts | Increase `work_mem` (per-operation, per-connection) |
| Frequent / forced checkpoints | Increase `max_wal_size`, adjust `checkpoint_timeout` |
| Slow checkpoint write/sync (`checkpointer_status=WARNING`) | Faster storage, smaller `checkpoint_completion_target`, or smaller `shared_buffers` |
| Seq scans on large tables | Add indexes (see [optimize-indexes.md](optimize-indexes.md)) |
| High `buffers_backend` ratio | Tune `bgwriter_lru_maxpages` and `bgwriter_lru_multiplier` |
