# Performance Baseline

**Commands:** `health-check` → `top-sql-by-time` → `table-stats` → `disk-io-analysis` → `index-health` → `wait-events`

## Steps

1. `pgreport health-check --verbose` (covers cache, locks, replication, wraparound, checkpoints, and critical settings — `shared_buffers`, `work_mem`, `effective_cache_size`, `checkpoint_completion_target`, `autovacuum`).
2. `pgreport top-sql-by-time --limit 20 --order-by mean_time`
3. `pgreport table-stats --order-by size --include-indexes`
4. `pgreport disk-io-analysis --analysis-type all`
5. `pgreport index-health` (single command for invalid + unused + duplicate/overlapping with savings estimate)
6. `pgreport wait-events --all-states`
7. Optional additions for fuller coverage:
   - `pgreport database-sizes` (per-DB on-disk size)
   - `pgreport top-objects-by-size` (largest tables and indexes)
   - `pgreport replication-status` if streaming replicas exist
8. Produce structured report:

```
## Database Performance Baseline - [date]
### 1. Health Score (overall + category breakdown)
### 2. Top Queries (text, calls, mean_time, total_time, rows)
### 3. Table Statistics (size, dead tuples, seq scans)
### 4. I/O Patterns (cache hit ratio, hot tables)
### 5. Index Utilization (score, unused count + size, duplicates)
### 6. Wait Events (distribution by type)
### 7. Configuration (key settings, deviations from recommendations)
### 8. Storage (database sizes, top objects)
```

## Tips

- Run at consistent times of day for fair comparison (avoid peak vs off-peak).
- Reset `pg_stat_statements` before measurement periods for clean data.
- Capture all outputs as `--json` so re-runs can be diffed structurally.
- After optimization, re-run the same baseline to measure impact.
