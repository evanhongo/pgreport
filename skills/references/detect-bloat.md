# Detect Bloat

**Commands:** `top-objects-by-size` → `table-bloat` → `index-bloat` → `pending-vacuum`

## Steps

1. `pgreport top-objects-by-size`. Largest tables + indexes (`pg_class.relpages` UNION ALL); pick the worst offenders to drill into.
2. `pgreport table-bloat --schema <s>` for the schema (or `--table <t>` for one table). Use `--approx` on tables > 10GB to avoid full scans. Tune `--min-bloat` and `--min-table-size-gb` to filter noise.
3. `pgreport index-bloat --schema <s>` (or `--table <t>` / `--index <i>`). Check leaf density (< 70% = significant), fragmentation %, empty pages.
4. `pgreport pending-vacuum`. Find vacuum backlog driving bloat.
5. Maintenance plan:
   - Tables needing VACUUM (high dead tuples)
   - Tables needing VACUUM FULL (> 50% bloat, requires downtime + exclusive lock) or `pg_repack`
   - Indexes needing REINDEX (consider REINDEX CONCURRENTLY on PG 12+)
   - Estimated space savings (sum `wasted_bytes` across rows)

## Warning

`table-bloat` without `--approx` performs a full table scan via `pgstattuple`. Use `--approx` (`pgstattuple_approx`) on large tables during production hours. Both require the `pgstattuple` extension.
