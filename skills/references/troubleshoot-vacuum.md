# Troubleshoot Vacuum & Wraparound

**Commands:** `pg-progress` → `autovacuum-status` → `pending-vacuum` → `vacuum-history` → `wraparound-risk` → `freeze-prediction` → `table-stats`

## Steps

1. `pgreport pg-progress --operation vacuum`. Check for stuck VACUUMs (e.g., "cleaning up indexes" with many rounds = blocked by long transactions).
2. `pgreport autovacuum-status`. All workers busy? Autovacuum enabled? Current thresholds?
3. `pgreport pending-vacuum`. Tables exceeding dead tuple thresholds.
4. `pgreport vacuum-history`. Tables not vacuumed recently despite high writes — output classifies each table as `never` / `stale` / `recent` / `fresh`.
5. `pgreport wraparound-risk --type both`. Per-database XID + MultiXactId age vs the 2^31 limit. Anything > 50% needs attention now.
6. `pgreport freeze-prediction`. Per-table view of which tables are approaching the autovacuum freeze threshold.
7. `pgreport table-stats --order-by dead_tuples` to confirm worst tables.
8. Report: diagnosis + specific `ALTER TABLE ... SET (autovacuum_*)` or `postgresql.conf` changes.

Note: autovacuum tuning checks live in `health-check` — `checks.settings.recommendations` will surface `autovacuum = off` as `critical` and other autovacuum-related guidance. For deeper per-knob inspection, query `pg_settings` directly:

```sql
SELECT name, setting, unit, source
FROM pg_settings
WHERE name LIKE 'autovacuum%' OR name LIKE 'autovacuum_vacuum%';
```

Common knobs to review when autovacuum is misbehaving:
- `autovacuum_vacuum_threshold` too high for small tables
- `autovacuum_vacuum_scale_factor` too high for large tables
- `autovacuum_vacuum_cost_delay` too conservative
- Too few `autovacuum_max_workers`

## Common Fixes

| Problem | Fix |
|---------|-----|
| All workers busy on big tables | Increase `autovacuum_max_workers`, reduce `autovacuum_vacuum_cost_delay` |
| Large tables never finish | Per-table `autovacuum_vacuum_scale_factor = 0.01` |
| Small tables not vacuumed | Lower `autovacuum_vacuum_threshold` per-table |
| VACUUM blocked | Terminate long idle-in-transaction sessions (see [diagnose-connections.md](diagnose-connections.md)) |
| Wraparound risk > 50% | Emergency `VACUUM FREEZE` on the offending tables (from `freeze-prediction`) |
