# Diagnose Connections

**Commands:** `connection-usage` → `active-queries` → `long-running-queries` → `idle-in-transaction-sessions` → `long-running-transactions` → `long-running-prepared-transactions` → `connection-security-status`

## Steps

1. `pgreport connection-usage`. Current client backend count vs `max_connections`. > 80% used = pool too small or sessions leaking.
2. `pgreport active-queries`. Full per-PID snapshot with `summary.by_state` (active / idle / idle in transaction / idle in transaction (aborted) / ...). Identifies which state is hogging slots.
3. Drill into state-specific issues:
   - `pgreport long-running-queries --threshold-minutes 5` — active queries over 5 min (default).
   - `pgreport idle-in-transaction-sessions --threshold-minutes 1` — sessions holding a txn open and doing nothing (most common cause of bloat + lock contention).
   - `pgreport long-running-transactions --threshold-hours 1` — any non-idle txn open > 1h (replication / vacuum / wraparound risk).
   - `pgreport long-running-prepared-transactions --threshold-hours 1` — stuck 2PC prepared txns (will hold locks AND block VACUUM/wraparound forever; almost always need `ROLLBACK PREPARED`).
4. `pgreport connection-security-status`. Audit per-session SSL / GSSAPI encryption status (compliance / external review).

## What to Do

| Symptom | Action |
|---------|--------|
| `connection-usage` near max | Add pgBouncer / increase pool; raise `max_connections` only as last resort (high overhead) |
| Many "idle in transaction" sessions | Set `idle_in_transaction_session_timeout`; fix app code releasing connections without COMMIT/ROLLBACK |
| One very long active query | See [diagnose-slow-queries.md](diagnose-slow-queries.md) or [tune-query.md](tune-query.md) |
| Stuck prepared transactions | `ROLLBACK PREPARED '<gid>'` (after confirming the application doesn't still need it) |
| Unencrypted sessions in compliance scope | Force `hostssl` in `pg_hba.conf`, set `ssl = on`, require client certs |

## Notes

- These commands all hit `pg_stat_activity` (free), so spam them safely during incident triage.
- For *blocked* sessions (waiting on a lock), see [investigate-locks.md](investigate-locks.md).
