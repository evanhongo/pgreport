# Tune Query

**Commands:** `explain` → `table-stats` → `hypothetical-index` → `explain --index`

## Steps

1. `pgreport explain --sql "<SQL>" --buffers`. Identify node types, actual vs estimated rows, buffer reads vs hits, most time-consuming nodes. Read the auto-generated `analysis.warnings` and `analysis.recommendations`.
2. `pgreport table-stats --table <name>` for each table in the query. Look for high dead tuples or low idx_scan ratio.
3. Propose indexes based on the plan. Test them as hypothetical (no disk writes):
   - `pgreport hypothetical-index --action create --table <t> --columns <c1,c2> [--index-type btree|gin|gist|brin|hash] [--unique] [--where "..."] [--include "..."]`
   - Access-method guide: WHERE/JOIN columns → btree (default); full-text → gin; large-table range scans → brin.
4. `pgreport explain --sql "<SQL>" --index <table>:<cols>[:type][:unique]` to compare original vs hypothetical plan. Repeat `--index` to stack multiple. Output gives `original_cost`, `cost_with_indexes`, `estimated_improvement_percent`, and `indexes_used`.
5. `pgreport hypothetical-index --action reset` to clean up.
6. Report: CREATE INDEX statement, expected improvement %, query rewrite suggestions.

## Tips

- `--no-analyze` for DML (INSERT/UPDATE/DELETE) to avoid executing them. With `--index` the CLI auto-disables `--analyze` (HypoPG only intercepts the planner, not execution) and adds a `note` to the output.
- Test one index at a time for complex queries to understand each contribution.
- `--format json` is required when `--index` is given (cost comparison parses JSON). Other formats are fine for plain EXPLAIN.
- No plan change after adding a hypothetical index = planner decided it isn't useful for that query pattern.
