# Health Check

**Commands:** `health-check` → `active-queries` → `wait-events`

## Steps

1. `pgreport health-check --verbose`. Note overall score and weak categories. The check covers connections, cache, locks, replication, wraparound, disk usage, bgwriter, checkpoints, and critical settings (`shared_buffers`, `work_mem`, `effective_cache_size`, `checkpoint_completion_target`, `autovacuum`, etc.). With `--verbose`, the raw `pg_settings` rows show up under `checks.settings.details`.
2. `pgreport active-queries`. Check for long-running queries, idle-in-transaction sessions, blocked queries. The output's `summary.by_state` + `blocked_queries` are the quick wins.
3. `pgreport wait-events`. High Lock waits = contention; high IO waits = disk bottleneck.
4. Prioritized action list: critical first (locks, wraparound), then optimizations.

## Score Interpretation

| Score | Status | Action |
|-------|--------|--------|
| 90-100 | Healthy | Monitor, no immediate action |
| 70-89 | Acceptable | Address warnings at next maintenance window |
| < 70 | Critical | Investigate and act immediately |

## Follow-up Routing

| Finding | Next Workflow |
|---------|--------------|
| Low cache hit ratio | [analyze-io-performance.md](analyze-io-performance.md) |
| Many slow queries | [diagnose-slow-queries.md](diagnose-slow-queries.md) |
| Lock contention / blocked queries | [investigate-locks.md](investigate-locks.md) |
| Too many connections / idle-in-tx | [diagnose-connections.md](diagnose-connections.md) |
| High dead tuples / wraparound risk | [troubleshoot-vacuum.md](troubleshoot-vacuum.md) |
| Bloat warnings | [detect-bloat.md](detect-bloat.md) |
| Replication lag / WAL archiver failures | [monitor-replication.md](monitor-replication.md) |
| Unused/invalid/duplicate indexes | [optimize-indexes.md](optimize-indexes.md) |
