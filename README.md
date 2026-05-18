# pgreport CLI — Operations Manual

## Overview

`pgreport` is a Click-based CLI tool for inspecting PostgreSQL database state and collecting diagnostic data.

It reports statistics, execution plans, bloat metrics, and health scores — it does not modify database configuration or execute tuning actions.

## Prerequisites

- PostgreSQL 12+ (some commands require 16+ or 17+ — see notes below).
- Extensions
  - `pg_stat_statements` — required for `top-sql-by-time` and `recommend-indexes`.
  - `pgstattuple` — required for `table-bloat` and `index-bloat`.
  - `hypopg` — required for `hypothetical-index`, `explain-with-indexes`, and `recommend-indexes --include-hypothetical`.

## Command Structure

```text
pgreport [--database-uri URI] [--json] <command> [--flags...]
```

### 1. Overall Health

| Command | Purpose |
|---|---|
| `health-check` | Multi-area health score covering connections, cache, locks, replication, wraparound, disk usage, bgwriter, checkpoints, and critical settings. |

### 2. Connections & Sessions

| Command | Purpose |
|---|---|
| `connection-usage` | Client backend count vs `max_connections`. |
| `connection-security-status` | SSL/GSSAPI encryption status per session. |
| `active-queries` | Detailed active-query view, blocked queries, and summary by state. |
| `long-running-queries` | Active queries running longer than `--threshold-minutes`. |
| `idle-in-transaction-sessions` | Idle-in-tx sessions older than `--threshold-minutes`. |
| `long-running-transactions` | Non-idle transactions older than `--threshold-hours`. |
| `long-running-prepared-transactions` | Stuck 2PC transactions older than `--threshold-hours`. |

### 3. Locks & Wait Events

| Command | Purpose |
|---|---|
| `blocking-locks` | Waiting/blocking session pairs via `pg_blocking_pids()`. |
| `lock-waiters` | Sessions blocked on locks with mode info. |
| `deadlock-detection` | Deadlock count for the current database. |
| `wait-events` | Wait-event breakdown across active client backends. |

### 4. Query Performance & Plans

| Command | Purpose |
|---|---|
| `top-sql-by-time` | Top `pg_stat_statements` queries with rich filters. |
| `explain` | Run EXPLAIN on `--sql` with plan-quality heuristics; optionally compare against HypoPG hypothetical indexes via repeatable `--index`. |
| `user-function-stats` | User-defined function execution metrics. |

### 5. I/O, Cache, WAL & Background

| Command | Purpose |
|---|---|
| `cache-hit-rate` | Buffer cache hit-rate percentage for the current database. |
| `rollback-rate` | Transaction rollback percentage per database. |
| `io-statistics` | Block reads/hits + temp I/O for the current database, plus detailed `pg_stat_io` breakdown when running on PG16+. |
| `disk-io-analysis` | Per-relation buffer-pool / `pg_statio_*` analysis. |
| `bgwriter-stats` | Background writer buffer cleaning metrics. |
| `checkpointer-stats` | Checkpointer activity + write/sync timing analysis (PG17+). |
| `temp-file-usage` | Databases generating temporary files (filter with `--threshold-gb`). |
| `slru-stats` | SLRU cache hit / read / write metrics. |
| `wal-statistics` | WAL record / FPI / byte generation metrics (PG14+). |

### 6. Tables, Indexes & Storage

| Command | Purpose |
|---|---|
| `table-stats` | Detailed user-table statistics. |
| `table-hotspots` | Top tables by DML and scan activity. |
| `top-objects-by-size` | Largest tables and indexes by relpages. |
| `database-sizes` | Top databases by on-disk size. |
| `list-indexes` | List all indexes on a specific table. |
| `index-health` | Invalid + unused + duplicate/overlapping indexes with score and DROP/REINDEX recommendations (use `--min-size-mb`, `--no-duplicates`). |
| `recommend-indexes` | AI-style index recommendations (HypoPG + pglast). |
| `hypothetical-index` | Manage HypoPG indexes (check/create/list/drop/...). |

### 7. Vacuum, Bloat & Wraparound

| Command | Purpose |
|---|---|
| `autovacuum-status` | Autovacuum config, launcher status, active workers. |
| `pg-progress` | Live progress of running ANALYZE / CREATE INDEX / CLUSTER / VACUUM ops (`--operation [analyze\|create-index\|cluster\|vacuum\|all]`). |
| `pending-vacuum` | Tables needing vacuum (dead tuples / wraparound). |
| `vacuum-history` | Recent vacuum / analyze activity by freshness. |
| `stale-statistics` | Tables with >10% rows modified since last ANALYZE. |
| `table-bloat` | Precise table bloat analysis (pgstattuple). |
| `index-bloat` | Precise index bloat analysis (pgstatindex). |
| `wraparound-risk` | Per-database XID and/or MultiXactId age + freeze urgency (`--type [xid\|mxid\|both]`). |
| `freeze-prediction` | Tables approaching the XID/MXID freeze threshold. |
| `sequence-exhaustion` | Non-cycling sequences more than 80% consumed. |

### 8. Replication & Archiving

| Command | Purpose |
|---|---|
| `replication-slots` | Replication slot status and WAL retention. |
| `replication-status` | Streaming replica lag in bytes and time. |
| `logical-replication-status` | Logical replication subscription message lag. |
| `wal-archiver-status` | WAL archiver success/failure counts and waldir size. |
| `database-conflict-stats` | Standby replica query cancellation statistics. |

## Usage Modes

### One-Shot Execution

```bash
pgreport health-check
pgreport top-sql-by-time --limit 5
pgreport long-running-queries --threshold-minutes 10
pgreport recommend-indexes --tables orders,users
pgreport explain --sql "SELECT * FROM users WHERE email='a@b.c'"
pgreport table-bloat --table users --min-bloat 30
pgreport replication-slots --json
```

### Interactive REPL Mode

Provide `--database-uri` without a subcommand to enter REPL mode:

```bash
pgreport --database-uri "postgresql://..."
pgreport> health-check --verbose
pgreport> top-sql-by-time --limit 5
pgreport> recommend-indexes --tables orders,users
pgreport> quit
```

## Database Source Management

Saves database URIs to avoid typing `--database-uri` each time:

```bash
pgreport source add postgres://user:pass@host:5432/mydb dev
pgreport source use dev
pgreport health-check          # automatically uses the active source
```

### Source Resolution Priority

1. `--database-uri` flag (highest priority)
2. Active source in `~/.config/pgreport/config.toml`
