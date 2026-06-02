---
name: pgreport
description: Use when diagnosing or tuning PostgreSQL performance.
---

# PostgreSQL Diagnostic Reporting

`pgreport` is a CLI of 47 PostgreSQL diagnostic commands. Match the user's scenario to a workflow, then read the corresponding reference file and follow it.

## CLI Surface

```
pgreport [--database-uri URI] [--json] <command> [--flags...]
```

Run `pgreport <command> --help` for full option details.

## Connection — read before running anything

**pgreport carries its own connection config. You almost never pass `--database-uri`.**

- Targets live in `~/.config/pgreport/config.toml`; `active_source` selects the current one. Just run `pgreport <command>` and it connects to the active source — **zero setup. Your first action is always to run the command, never to go hunting for a connection.**
- Switch target: `pgreport source ls` (list + interactive switch) or `pgreport source use <name>` (non-interactive). One-off override: `pgreport --database-uri '<uri>' <command>`. With no config, it falls back to libpq defaults (`PG*` env, `~/.pgpass`).

**Do NOT pre-assume you cannot connect.** A `postgresql` systemd unit showing `inactive`, `pg_isready` returning "no response" on the unix socket, the database running in Docker, or simply not knowing a password — **none of these block pgreport.** It reaches the active source over TCP. Run the command first; react only to an *actual* connection error in pgreport's output.

**Never manufacture credentials to build your own URI.** No `docker inspect`, no reading container env vars, no `docker exec psql` to bypass auth. These leak secrets and are not how pgreport connects. If pgreport genuinely cannot connect, report the error and ask the user for a source name or URI — do not work around it.

## Scenario Router

| User Says | Workflow | Reference |
|-----------|----------|-----------|
| "health check", "is my DB healthy", "overall status" | Health Check | [references/health-check.md](references/health-check.md) |
| "queries slow", "find slow queries", "high latency", "query timeout" | Diagnose Slow Queries | [references/diagnose-slow-queries.md](references/diagnose-slow-queries.md) |
| User pastes SQL, "tune this query", "why is this slow", "EXPLAIN this", "seq scan" | Tune Query | [references/tune-query.md](references/tune-query.md) |
| "optimize indexes", "unused indexes", "duplicate indexes", "invalid index", "index maintenance" | Optimize Indexes | [references/optimize-indexes.md](references/optimize-indexes.md) |
| "bloat", "disk space growing", "table too large", "reclaimable space"| Detect Bloat | [references/detect-bloat.md](references/detect-bloat.md) |
| "autovacuum", "vacuum stuck", "dead tuples growing", "wraparound", "freeze", "vacuum not running" | Troubleshoot Vacuum & Wraparound | [references/troubleshoot-vacuum.md](references/troubleshoot-vacuum.md) |
| "I/O bottleneck", "cache hit ratio low", "disk slow", "temp files", "checkpoint spikes" | Analyze I/O Performance | [references/analyze-io-performance.md](references/analyze-io-performance.md) |
| "locks", "blocked queries", "lock contention", "deadlock", "blocked on lock" | Investigate Locks | [references/investigate-locks.md](references/investigate-locks.md) |
| "replication lag", "standby behind", "logical replication", "replication slot", "WAL archiving" | Monitor Replication | [references/monitor-replication.md](references/monitor-replication.md) |
| "too many connections", "connection pool full", "idle in transaction", "long transactions" | Diagnose Connections | [references/diagnose-connections.md](references/diagnose-connections.md) |
| "baseline", "snapshot", "before deployment", "performance report", "compare before/after" | Performance Baseline | [references/performance-baseline.md](references/performance-baseline.md) |

**If the scenario is unclear**, start with **Health Check** to get an overall picture, then drill into specific workflows based on findings.
