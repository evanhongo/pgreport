"""Session, connection, and lock monitoring analyzers.

Covers long-running queries, idle-in-transaction sessions, long / prepared
transactions, connection usage, lock waiters, and the detailed
``active_queries`` view.
"""

from __future__ import annotations

from typing import Any

from ..utils.sql_driver import SqlDriver

LONG_RUNNING_QUERY_SECONDS = 300
IDLE_TRANSACTION_SECONDS = 60


SQL_LONG_RUNNING_QUERIES = """
    SELECT
        pid,
        age(clock_timestamp(), query_start) AS duration,
        usename,
        datname,
        state,
        query
    FROM pg_stat_activity
    WHERE state = 'active'
      AND backend_type = 'client backend'
      AND query_start < now() - (%s::int || ' minutes')::interval
    ORDER BY duration DESC
"""

SQL_IDLE_IN_TRANSACTION_SESSIONS = """
    SELECT
        pid,
        age(clock_timestamp(), xact_start) AS transaction_duration,
        usename,
        datname,
        state
    FROM pg_stat_activity
    WHERE state = 'idle in transaction'
      AND backend_type = 'client backend'
      AND xact_start < now() - (%s::int || ' minutes')::interval
    ORDER BY transaction_duration DESC
"""

SQL_LONG_RUNNING_TRANSACTIONS = """
    SELECT
        pid,
        usename,
        datname,
        state,
        age(clock_timestamp(), xact_start) AS transaction_duration,
        query
    FROM pg_stat_activity
    WHERE state != 'idle'
      AND xact_start IS NOT NULL
      AND backend_type = 'client backend'
      AND age(clock_timestamp(), xact_start) > (%s::int || ' hours')::interval
    ORDER BY xact_start ASC
"""

SQL_LONG_RUNNING_PREPARED_TRANSACTIONS = """
    SELECT
        gid,
        owner,
        database,
        prepared,
        now() - prepared AS duration,
        transaction
    FROM pg_prepared_xacts
    WHERE now() - prepared > (%s::int || ' hours')::interval
    ORDER BY prepared ASC
"""

SQL_CONNECTION_USAGE = """
    SELECT
        (SELECT count(*) FROM pg_stat_activity
            WHERE backend_type = 'client backend') AS used_connections,
        (SELECT setting FROM pg_settings
            WHERE name = 'max_connections')::int AS max_connections
"""

SQL_LOCK_WAITERS = """
    SELECT
        blocked_locks.pid AS blocked_pid,
        blocked_activity.usename AS blocked_user,
        blocked_activity.query AS blocked_query,
        blocking_locks.pid AS blocking_pid,
        blocking_activity.usename AS blocking_user,
        blocking_activity.query AS blocking_query,
        blocked_locks.mode AS blocked_mode,
        blocked_locks.relation::regclass::text AS blocked_relation
    FROM pg_locks blocked_locks
    JOIN pg_stat_activity blocked_activity
        ON blocked_activity.pid = blocked_locks.pid
    JOIN pg_locks blocking_locks
        ON blocking_locks.locktype = blocked_locks.locktype
        AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
        AND blocking_locks.pid != blocked_locks.pid
    JOIN pg_stat_activity blocking_activity
        ON blocking_activity.pid = blocking_locks.pid
    WHERE NOT blocked_locks.granted
    ORDER BY blocked_locks.pid
    LIMIT %s
"""


class SessionAnalyzer:
    """Session, connection, and lock-state observations."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver

    async def long_running_queries(self, threshold_minutes: int = 5) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(
            SQL_LONG_RUNNING_QUERIES, [threshold_minutes]
        )
        return rows or []

    async def idle_in_transaction_sessions(self, threshold_minutes: int = 1) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(
            SQL_IDLE_IN_TRANSACTION_SESSIONS, [threshold_minutes]
        )
        return rows or []

    async def long_running_transactions(self, threshold_hours: int = 1) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(
            SQL_LONG_RUNNING_TRANSACTIONS, [threshold_hours]
        )
        return rows or []

    async def long_running_prepared_transactions(self, threshold_hours: int = 1) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(
            SQL_LONG_RUNNING_PREPARED_TRANSACTIONS, [threshold_hours]
        )
        return rows or []

    async def connection_usage(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_CONNECTION_USAGE)
        return rows or []

    async def lock_waiters(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_LOCK_WAITERS, [limit])
        return rows or []

    async def active_queries(
        self,
        min_duration_seconds: int = 0,
        include_idle: bool = False,
        include_system: bool = False,
        database: str | None = None,
    ) -> dict[str, Any]:
        """Detailed active-query view (pgreport-original; was ``query active``)."""
        filters: list[str] = []
        params: list[Any] = []

        if not include_idle:
            filters.append("state != 'idle'")

        if not include_system:
            filters.append("backend_type = 'client backend'")
            filters.append("query NOT LIKE '%%pg_catalog%%'")
            filters.append("query NOT LIKE '%%information_schema%%'")

        if database:
            filters.append("datname = %s")
            params.append(database)

        if min_duration_seconds > 0:
            filters.append("EXTRACT(epoch FROM now() - query_start) >= %s")
            params.append(min_duration_seconds)

        where_clause = "WHERE " + " AND ".join(filters) if filters else ""

        query = f"""
            SELECT
                pid,
                datname as database,
                usename as username,
                client_addr,
                state,
                wait_event_type,
                wait_event,
                EXTRACT(epoch FROM now() - query_start)::integer as duration_seconds,
                EXTRACT(epoch FROM now() - xact_start)::integer as transaction_seconds,
                LEFT(query, 500) as query,
                backend_type
            FROM pg_stat_activity
            {where_clause}
            ORDER BY
                CASE WHEN state = 'active' THEN 0 ELSE 1 END,
                query_start ASC
        """

        result = await self.driver.execute_query(query, params if params else None)

        summary_query = """
            SELECT state, COUNT(*) as count
            FROM pg_stat_activity
            WHERE backend_type = 'client backend'
            GROUP BY state
        """
        summary = await self.driver.execute_query(summary_query)

        blocked_query = """
            SELECT
                blocked.pid as blocked_pid,
                blocked.query as blocked_query,
                blocking.pid as blocking_pid,
                blocking.query as blocking_query,
                blocked.wait_event_type,
                blocked.wait_event
            FROM pg_stat_activity blocked
            JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
            WHERE blocked.wait_event_type = 'Lock'
            LIMIT 10
        """
        blocked = await self.driver.execute_query(blocked_query)

        output: dict[str, Any] = {
            "summary": {
                "by_state": {row["state"]: row["count"] for row in summary} if summary else {},
                "total_connections": len(result) if result else 0,
            },
            "active_queries": result,
            "blocked_queries": blocked if blocked else [],
        }

        warnings: list[str] = []
        if result:
            for row in result:
                duration = row.get("duration_seconds", 0) or 0
                if duration > LONG_RUNNING_QUERY_SECONDS:
                    warnings.append(
                        f"Long-running query (PID {row['pid']}): {duration}s - Consider investigating"
                    )
                if row.get("state") == "idle in transaction" and (row.get("transaction_seconds", 0) or 0) > IDLE_TRANSACTION_SECONDS:
                    warnings.append(
                        f"Idle transaction (PID {row['pid']}): {row['transaction_seconds']}s - May be holding locks"
                    )

        if warnings:
            output["warnings"] = warnings

        return output
