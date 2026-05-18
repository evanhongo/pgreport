# Monitor Replication

**Commands:** `replication-slots` → `replication-status` → `logical-replication-status` → `wal-archiver-status` → `database-conflict-stats`

## Steps

1. `pgreport replication-slots`. Both physical and logical slot status. Watch for inactive slots with mounting `wal_retained_bytes` (= primary keeping WAL the standby will never consume → disk fills up).
2. `pgreport replication-status`. Streaming replica lag in bytes and time (sent / write / flush / replay lag, per standby). Run on the primary.
3. `pgreport logical-replication-status`. Logical subscription send/receive lag. Run on the subscriber.
4. `pgreport wal-archiver-status`. Archived count vs failed count + WAL directory size. Failures here mean continuous archiving is broken (PITR / replica catch-up risk).
5. On standbys: `pgreport database-conflict-stats`. Query cancellations from recovery conflicts (snapshot / lock / buffer-pin / deadlock / tablespace).

## What to Do

| Symptom | Action |
|---------|--------|
| Inactive replication slot growing | Drop the slot if its consumer is permanently gone, or restart the consumer |
| Replay lag increasing | Check standby load (`top-sql-by-time` on the standby); add `hot_standby_feedback` if cancellations are the cause |
| Logical sub far behind | Inspect subscriber `pg_stat_subscription_stats`; large transactions on publisher stall logical decoding |
| `wal-archiver-status` shows failures | Inspect `archive_command` exit code + storage; consider `pg_stat_archiver` in PG logs |
| Recurring recovery conflicts | Increase `max_standby_streaming_delay`, enable `hot_standby_feedback`, or run heavy reports on a non-replica |

## Notes

- All four "replication" commands are no-op (empty `data`) on a standalone server. That's expected output, not an error.
- For per-database WAL pressure rather than archiving health, use `wal-statistics` (PG14+).
