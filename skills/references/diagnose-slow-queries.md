# Diagnose Slow Queries

**Commands:** `top-sql-by-time` → `explain` → `table-stats` → `recommend-indexes`

## Steps

1. `pgreport top-sql-by-time --order-by mean_time --limit 10`. Note queries with high mean time AND high call count (total impact = mean × calls).
2. `pgreport explain --sql "<SQL>" --buffers` for each top offender. Look for:
   - Seq Scans on large tables (missing index)
   - Nested Loop with high row estimates (join strategy)
   - High buffer reads vs hits (cache miss)
   - Sort/Hash spilling to disk
   - The output `analysis.warnings` and `analysis.recommendations` flag common issues automatically.
3. `pgreport table-stats --table <name>` for involved tables. Flag high dead tuples, high seq_scan ratio, low cache hit.
4. `pgreport recommend-indexes --queries "<slow queries separated by semicolons>"` (or omit `--queries` to analyze the live `pg_stat_statements` workload).
5. Report with priority-ordered action items.

## EXPLAIN Quick Reference

| Symptom | Likely Cause | Action |
|---------|-------------|--------|
| Seq Scan on large table | Missing index | Add index on filter/join columns |
| High actual vs estimated rows | Stale statistics | Run ANALYZE |
| Sort with high memory | Missing ORDER BY index | Add covering index |
| Nested Loop many iterations | Poor join strategy | Check join column indexes |
| Buffers: read >> shared hit | Cold cache / table too large | Check `shared_buffers`, add index |
