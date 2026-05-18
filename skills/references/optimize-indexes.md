# Optimize Indexes

**Commands:** `index-health` → `list-indexes` → `recommend-indexes` → `hypothetical-index` → `explain --index`

## Steps

1. `pgreport index-health [--schema X] [--min-size-mb N] [--no-duplicates]`. Single command surfacing invalid + unused + duplicate/overlapping indexes, with a 0–100 score, `potential_savings_bytes`, and DROP/REINDEX SQL in `recommendations`.
2. `pgreport list-indexes --table <t>` for tables flagged in step 1. Review every existing index (including healthy ones) before changing anything.
3. `pgreport recommend-indexes`. Pass `--queries "Q1;Q2"` for specific SQL, or omit to analyze the live `pg_stat_statements` workload. Add `--tables a,b` to focus on specific tables.
4. `pgreport hypothetical-index --action create --table <t> --columns <cols>` for each candidate. No disk impact.
5. `pgreport explain --sql "<SQL>" --index <table>:<cols>[:type][:unique]` to compare plans. Confirm planner uses the index and check `estimated_improvement_percent` + `indexes_used`.
6. `pgreport hypothetical-index --action reset` to clean up.
7. Report: prioritized DROP INDEX + CREATE INDEX statements with estimated impact.

## Key Considerations

- Zero scans does NOT mean never needed. Check for infrequent critical queries before dropping.
- Use `pgreport hypothetical-index --action hide --index-id <oid>` to test hiding a real index before dropping it.
- Consider partial indexes (`--where`) and covering indexes (`--include`) when creating hypothetical indexes.
- `index-health` already classifies duplicates as `duplicate` / `overlapping` / `related` — read the `relationship` field per row before deciding to DROP.
