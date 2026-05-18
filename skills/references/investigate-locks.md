# Investigate Locks

**Commands:** `blocking-locks` → `lock-waiters` → `active-queries` → `deadlock-detection`

## Steps

1. `pgreport blocking-locks`. Direct pairs of waiting vs blocking sessions joined via `pg_blocking_pids()` — the fastest "who's holding what". Each row gives PID, user, query, and how long each side has been waiting / blocking.
2. `pgreport lock-waiters --limit 20`. Detailed lock-wait graph from `pg_locks` self-join — surfaces overlapping/transitive waiters that `pg_blocking_pids()` may miss.
3. `pgreport active-queries`. Full snapshot of every PID with `state`, wait events, and a `summary.by_state` count. Use this to understand the broader picture (e.g., are idle-in-tx sessions causing the lock?).
4. `pgreport deadlock-detection`. Cumulative deadlock count from `pg_stat_database` — > 0 means deadlocks happened since the last stats reset (PG resolves them automatically, but the queries that caused them retried or errored).

## What to Do

| Finding | Action |
|---------|--------|
| One long blocker | `SELECT pg_terminate_backend(<pid>)` after confirming the query is safe to kill |
| Idle-in-transaction blocker | Track down the app session leaking the txn; consider `idle_in_transaction_session_timeout` |
| Many waiters on one row/table | Application-level concurrency bug — examine the transaction pattern |
| Recurring deadlocks | Acquire locks in consistent order across transactions; consider `SELECT ... FOR UPDATE NOWAIT` |

## Notes

- `pgreport active-queries --min-duration 60 --include-idle` is the diagnostic snapshot to attach to incident reports.
- For session/connection issues unrelated to locks (idle-in-tx without a victim, exhausted pool), see [diagnose-connections.md](diagnose-connections.md).
