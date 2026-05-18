"""Replication and archiving analyzers.

Covers replication slots, streaming and logical replication status, and
WAL archiver activity.
"""

from __future__ import annotations

from typing import Any

from ..utils.sql_driver import SqlDriver

SQL_REPLICATION_SLOTS = """
    SELECT
        slot_name,
        plugin,
        slot_type,
        database,
        active,
        pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS restart_lsn_lag_bytes
    FROM pg_replication_slots
"""

SQL_REPLICATION_STATUS = """
    SELECT
        application_name,
        client_addr,
        state,
        sync_state,
        sync_priority,
        pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn) AS sent_lag_bytes,
        pg_wal_lsn_diff(flush_lsn, replay_lsn) AS replay_lag_bytes,
        write_lag::text AS write_lag_time,
        flush_lag::text AS flush_lag_time,
        replay_lag::text AS replay_lag_time
    FROM pg_stat_replication
"""

SQL_LOGICAL_REPLICATION_STATUS = """
    SELECT
        subname,
        subid,
        EXTRACT(EPOCH FROM (now() - last_msg_send_time)) AS send_lag_sec,
        EXTRACT(EPOCH FROM (now() - last_msg_receipt_time)) AS receive_lag_sec
    FROM pg_stat_subscription
    WHERE subid IS NOT NULL
    ORDER BY subname
"""

SQL_WAL_ARCHIVER_STATUS = """
    SELECT
        archived_count,
        last_archived_wal,
        last_archived_time,
        failed_count,
        last_failed_wal,
        last_failed_time,
        stats_reset,
        (
            SELECT pg_size_pretty(COALESCE(SUM(size)::bigint,
                (SELECT pg_database_size(current_database()) * 0.1)::bigint))
            FROM pg_ls_waldir()
        ) AS wal_directory_size
    FROM pg_stat_archiver
"""


class ReplicationAnalyzer:
    """Replication slot, streaming, logical replication, and WAL archiver checks."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver

    async def replication_slots(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_REPLICATION_SLOTS)
        return rows or []

    async def replication_status(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_REPLICATION_STATUS)
        return rows or []

    async def logical_replication_status(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_LOGICAL_REPLICATION_STATUS)
        return rows or []

    async def wal_archiver_status(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_WAL_ARCHIVER_STATUS)
        return rows or []
