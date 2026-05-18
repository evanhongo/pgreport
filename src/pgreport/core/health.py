"""Core health and availability analyzers.

Covers wraparound risk (XID + MultiXactId), blocking locks, deadlock counts,
SSL/GSSAPI session security, wait events, multi-area health-check scoring,
and index health (invalid + unused + duplicate/overlapping).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..utils.constants import SYSTEM_SCHEMAS_SQL
from ..utils.sql_driver import SqlDriver

SQL_XID_WRAPAROUND_RISK = """
    SELECT
        d.datname,
        age(d.datfrozenxid) AS xid_age,
        TRUNC(100 * (age(d.datfrozenxid)::numeric / 2147483647), 2)::numeric
            AS percentage_used
    FROM pg_database d
    WHERE d.datallowconn = true
    ORDER BY xid_age DESC
"""

SQL_MULTIXID_WRAPAROUND_RISK = """
    SELECT
        d.datname,
        age(d.datminmxid) AS mxid_age,
        d.datminmxid,
        current_setting('autovacuum_multixact_freeze_max_age')::bigint
            AS freeze_max_age,
        CASE
            WHEN d.datminmxid = 0 THEN 'FROZEN'
            WHEN age(d.datminmxid) >= 2147483647 THEN 'INVALID_OR_FROZEN'
            WHEN age(d.datminmxid) >=
                current_setting('autovacuum_multixact_freeze_max_age')::bigint THEN 'FORCE_AUTOVACUUM'
            WHEN age(d.datminmxid) >= 2107483647 THEN 'CRITICAL'
            WHEN age(d.datminmxid) >= 2117483647 THEN 'WARNING'
            ELSE 'OK'
        END AS status,
        CASE
            WHEN d.datminmxid = 0 THEN NULL
            WHEN age(d.datminmxid) >= 2147483647 THEN NULL
            ELSE (current_setting('autovacuum_multixact_freeze_max_age')::bigint
                - age(d.datminmxid))
        END AS remaining_to_autovacuum
    FROM pg_database d
    WHERE d.datallowconn = true
    ORDER BY mxid_age DESC
"""

SQL_BLOCKING_LOCKS = """
    SELECT
        waiting_activity.pid AS waiting_pid,
        waiting_activity.usename AS waiting_user,
        waiting_activity.query AS waiting_query,
        age(clock_timestamp(), waiting_activity.query_start) AS waiting_duration,
        blocking_activity.pid AS blocking_pid,
        blocking_activity.usename AS blocking_user,
        blocking_activity.query AS blocking_query,
        age(clock_timestamp(), blocking_activity.query_start) AS blocking_duration
    FROM pg_stat_activity AS waiting_activity
    JOIN pg_locks AS waiting ON waiting_activity.pid = waiting.pid
        AND NOT waiting.granted
    JOIN pg_stat_activity AS blocking_activity
        ON blocking_activity.pid = ANY(pg_blocking_pids(waiting.pid))
    WHERE waiting_activity.backend_type = 'client backend'
"""

SQL_DEADLOCK_DETECTION = """
    SELECT datname, deadlocks
    FROM pg_stat_database
    WHERE datname = current_database()
"""

SQL_CONNECTION_SECURITY_STATUS = """
    SELECT
        a.datname,
        a.usename,
        a.client_addr,
        COALESCE(s.ssl, false) AS ssl_enabled,
        COALESCE(s.version, 'N/A') AS ssl_version,
        COALESCE(s.cipher, 'N/A') AS ssl_cipher,
        COALESCE(g.gss_authenticated, false) AS gssapi_auth,
        COALESCE(g.encrypted, false) AS gssapi_encryption,
        CASE
            WHEN s.ssl = true THEN 'SSL'
            WHEN g.encrypted = true THEN 'GSSAPI'
            WHEN a.client_addr IS NULL THEN 'local'
            ELSE 'unencrypted'
        END AS connection_type
    FROM pg_stat_activity a
    LEFT JOIN pg_stat_ssl s ON a.pid = s.pid
    LEFT JOIN pg_stat_gssapi g ON a.pid = g.pid
    WHERE a.backend_type = 'client backend'
    ORDER BY connection_type, a.datname, a.usename
    LIMIT %s
"""

class HealthAnalyzer:
    """Core health, availability, and security observations."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    async def wraparound_risk(self, type_: str = "both") -> dict[str, Any] | list[dict[str, Any]]:
        """Per-database XID / MultiXactId wraparound risk.

        ``type_`` selects which view to run:
        - ``"xid"``: just transaction-ID wraparound rows
        - ``"mxid"``: just MultiXactId wraparound rows
        - ``"both"`` (default): dict ``{"xid": [...], "mxid": [...]}``
        """
        if type_ == "xid":
            rows = await self.driver.execute_query(SQL_XID_WRAPAROUND_RISK)
            return rows or []
        if type_ == "mxid":
            rows = await self.driver.execute_query(SQL_MULTIXID_WRAPAROUND_RISK)
            return rows or []
        xid_rows = await self.driver.execute_query(SQL_XID_WRAPAROUND_RISK) or []
        mxid_rows = await self.driver.execute_query(SQL_MULTIXID_WRAPAROUND_RISK) or []
        return {"xid": xid_rows, "mxid": mxid_rows}

    async def blocking_locks(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_BLOCKING_LOCKS)
        return rows or []

    async def deadlock_detection(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_DEADLOCK_DETECTION)
        return rows or []

    async def connection_security_status(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_CONNECTION_SECURITY_STATUS, [limit])
        return rows or []

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    async def wait_events(self, active_only: bool = True) -> dict[str, Any]:
        state_filter = "AND state = 'active'" if active_only else ""

        query = f"""
            SELECT
                wait_event_type,
                wait_event,
                COUNT(*) as count,
                array_agg(DISTINCT pid) as pids
            FROM pg_stat_activity
            WHERE wait_event IS NOT NULL
              AND backend_type = 'client backend'
              {state_filter}
            GROUP BY wait_event_type, wait_event
            ORDER BY count DESC
        """
        result = await self.driver.execute_query(query)

        type_query = f"""
            SELECT wait_event_type, COUNT(*) as count
            FROM pg_stat_activity
            WHERE wait_event_type IS NOT NULL
              AND backend_type = 'client backend'
              {state_filter}
            GROUP BY wait_event_type
            ORDER BY count DESC
        """
        type_result = await self.driver.execute_query(type_query)

        analysis: dict[str, list[str]] = {"issues": [], "recommendations": []}

        if type_result:
            for row in type_result:
                wait_type = row.get("wait_event_type")
                count = row.get("count", 0)

                if wait_type == "Lock" and count > 5:
                    analysis["issues"].append(f"{count} processes waiting on locks")
                    analysis["recommendations"].append(
                        "Investigate lock contention using pg_locks and pg_blocking_pids()"
                    )
                elif wait_type == "IO" and count > 10:
                    analysis["issues"].append(f"{count} processes waiting on I/O")
                    analysis["recommendations"].append(
                        "Consider tuning I/O settings or increasing shared_buffers"
                    )
                elif wait_type == "BufferPin" and count > 0:
                    analysis["issues"].append(f"{count} processes waiting on buffer pins")
                    analysis["recommendations"].append(
                        "This may indicate contention on frequently accessed pages"
                    )

        return {
            "wait_events": result,
            "by_type": type_result,
            "analysis": analysis,
        }

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    async def health_check(
        self, include_recommendations: bool = True, verbose: bool = False
    ) -> dict[str, Any]:
        health: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {},
            "overall_score": 0,
            "issues": [],
            "recommendations": [],
        }

        await self._check_connections(health)
        await self._check_cache_ratios(health)
        await self._check_locks(health)
        await self._check_replication(health)
        await self._check_wraparound(health)
        await self._check_disk_usage(health)
        await self._check_bgwriter(health)
        await self._check_checkpoints(health)
        await self._check_critical_settings(health)

        scores = [check.get("score", 100) for check in health["checks"].values()]
        health["overall_score"] = round(sum(scores) / len(scores), 1) if scores else 0

        if health["overall_score"] >= 90:
            health["status"] = "healthy"
        elif health["overall_score"] >= 70:
            health["status"] = "warning"
        else:
            health["status"] = "critical"

        if not include_recommendations:
            health.pop("recommendations", None)

        if not verbose:
            for check in health["checks"].values():
                check.pop("details", None)

        return health

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------

    async def index_health(
        self,
        schema_name: str = "public",
        min_size_mb: float = 0.0,
        include_duplicates: bool = True,
    ) -> dict[str, Any]:
        invalid_query = """
            SELECT c.relname AS index_name,
                   n.nspname AS schema_name,
                   t.relname AS table_name
            FROM pg_class c
            JOIN pg_index i ON c.oid = i.indexrelid
            JOIN pg_class t ON i.indrelid = t.oid
            JOIN pg_namespace n ON c.relnamespace = n.oid
            WHERE NOT i.indisvalid
        """
        invalid_indexes = await self.driver.execute_query(invalid_query) or []

        unused_query = f"""
            SELECT
                s.schemaname,
                s.relname AS table_name,
                s.indexrelname AS index_name,
                s.idx_scan AS scans,
                s.idx_tup_read AS tuples_read,
                s.idx_tup_fetch AS tuples_fetched,
                pg_size_pretty(pg_relation_size(s.indexrelid)) AS size,
                pg_relation_size(s.indexrelid) AS size_bytes,
                pg_get_indexdef(s.indexrelid) AS definition,
                t.n_live_tup AS table_rows
            FROM pg_stat_user_indexes s
            JOIN pg_stat_user_tables t ON s.relid = t.relid
            WHERE s.schemaname = %s
              AND s.schemaname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND pg_relation_size(s.indexrelid) >= %s * 1024 * 1024
              AND s.indexrelname NOT LIKE '%%_pkey'
              AND s.idx_scan = 0
            ORDER BY pg_relation_size(s.indexrelid) DESC
        """
        unused_indexes = await self.driver.execute_query(
            unused_query, [schema_name, min_size_mb]
        ) or []

        duplicate_indexes: list[dict[str, Any]] = []
        if include_duplicates:
            duplicate_query = f"""
                WITH index_cols AS (
                    SELECT
                        n.nspname AS schema_name,
                        t.relname AS table_name,
                        i.relname AS index_name,
                        pg_get_indexdef(i.oid) AS definition,
                        array_agg(a.attname ORDER BY k.n) AS columns,
                        pg_relation_size(i.oid) AS size_bytes
                    FROM pg_index x
                    JOIN pg_class t ON t.oid = x.indrelid
                    JOIN pg_class i ON i.oid = x.indexrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    CROSS JOIN unnest(x.indkey) WITH ORDINALITY AS k(attnum, n)
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                    WHERE n.nspname = %s
                      AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
                    GROUP BY n.nspname, t.relname, i.relname, i.oid
                )
                SELECT
                    a.table_name,
                    a.index_name AS index1,
                    a.columns AS columns1,
                    a.definition AS definition1,
                    a.size_bytes AS size1,
                    b.index_name AS index2,
                    b.columns AS columns2,
                    b.definition AS definition2,
                    b.size_bytes AS size2,
                    CASE
                        WHEN a.columns = b.columns THEN 'duplicate'
                        WHEN a.columns[1:array_length(b.columns, 1)] = b.columns THEN 'overlapping'
                        ELSE 'related'
                    END AS relationship
                FROM index_cols a
                JOIN index_cols b ON a.table_name = b.table_name
                    AND a.index_name < b.index_name
                WHERE a.columns = b.columns
                   OR a.columns[1:array_length(b.columns, 1)] = b.columns
            """
            duplicate_indexes = await self.driver.execute_query(
                duplicate_query, [schema_name]
            ) or []

        invalid_count = len(invalid_indexes)
        unused_count = len(unused_indexes)
        duplicate_count = len(duplicate_indexes)
        potential_savings_bytes = sum(
            (r.get("size_bytes") or 0) for r in unused_indexes
        )

        recommendations: list[str] = []
        for idx in invalid_indexes:
            recommendations.append(
                f"REINDEX INDEX {idx.get('schema_name', schema_name)}.{idx['index_name']}; "
                f"-- invalid index on {idx['table_name']}"
            )
        for idx in unused_indexes[:5]:
            recommendations.append(
                f"DROP INDEX {schema_name}.{idx['index_name']}; "
                f"-- {idx.get('size', 'unknown')}, 0 scans on {idx['table_name']}"
            )
        for dup in duplicate_indexes[:5]:
            rel = dup.get("relationship", "duplicate")
            recommendations.append(
                f"DROP INDEX {schema_name}.{dup['index2']}; "
                f"-- {rel} of {dup['index1']} on {dup['table_name']}"
            )

        score = 100
        if invalid_count > 0:
            score -= min(30, invalid_count * 10)
        if unused_count > 0:
            score -= min(20, unused_count * 2)
        if duplicate_count > 0:
            score -= min(20, duplicate_count * 5)

        output: dict[str, Any] = {
            "schema": schema_name,
            "min_size_mb": min_size_mb,
            "score": max(0, score),
            "summary": {
                "invalid_indexes": invalid_count,
                "unused_indexes": unused_count,
                "duplicate_indexes": duplicate_count,
            },
            "potential_savings_bytes": potential_savings_bytes,
            "invalid_indexes": invalid_indexes,
            "unused_indexes": unused_indexes,
            "recommendations": recommendations,
        }
        if include_duplicates:
            output["duplicate_indexes"] = duplicate_indexes
        return output

    # ------------------------------------------------------------------
    # health_check helpers (unchanged from previous HealthChecker)
    # ------------------------------------------------------------------

    def _parse_size(self, value: str, unit: str | None) -> int:
        try:
            num = int(value)
            multipliers = {
                "B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3,
                "8kB": 8 * 1024, "16kB": 16 * 1024, "32kB": 32 * 1024,
            }
            return num * multipliers.get(unit or "B", 1)
        except (ValueError, TypeError):
            return 0

    async def _check_connections(self, health: dict) -> None:
        query = """
            SELECT
                max_conn, used, res_for_super,
                ROUND(100.0 * used / max_conn, 1) as used_pct
            FROM (
                SELECT setting::int as max_conn FROM pg_settings WHERE name = 'max_connections'
            ) m,
            (
                SELECT COUNT(*) as used FROM pg_stat_activity
            ) u,
            (
                SELECT setting::int as res_for_super FROM pg_settings
                WHERE name = 'superuser_reserved_connections'
            ) r
        """
        result = await self.driver.execute_query(query)

        if result:
            row = result[0]
            used_pct = row.get("used_pct", 0)
            score = 100
            if used_pct > 90:
                score = 30
                health["issues"].append("Critical: Connection usage above 90%")
                health["recommendations"].append(
                    "Increase max_connections or use connection pooling (pgbouncer)"
                )
            elif used_pct > 75:
                score = 70
                health["issues"].append("Warning: Connection usage above 75%")
            health["checks"]["connections"] = {
                "score": score, "used_percent": used_pct, "details": row,
            }

    async def _check_cache_ratios(self, health: dict) -> None:
        query = """
            SELECT
                ROUND(100.0 * sum(heap_blks_hit) / nullif(sum(heap_blks_hit) + sum(heap_blks_read), 0), 2) as buffer_hit_ratio,
                ROUND(100.0 * sum(idx_blks_hit) / nullif(sum(idx_blks_hit) + sum(idx_blks_read), 0), 2) as index_hit_ratio
            FROM pg_statio_user_tables
        """
        result = await self.driver.execute_query(query)

        if result:
            row = result[0]
            buffer_ratio = row.get("buffer_hit_ratio") or 0
            index_ratio = row.get("index_hit_ratio") or 0
            score = 100
            if buffer_ratio < 90:
                score -= 30
                health["issues"].append(f"Buffer cache hit ratio is low: {buffer_ratio}%")
                health["recommendations"].append("Consider increasing shared_buffers")
            if index_ratio < 95:
                score -= 20
                health["issues"].append(f"Index cache hit ratio is low: {index_ratio}%")
            health["checks"]["cache"] = {
                "score": max(0, score), "buffer_hit_ratio": buffer_ratio,
                "index_hit_ratio": index_ratio, "details": row,
            }

    async def _check_locks(self, health: dict) -> None:
        query = """
            SELECT COUNT(*) as total_locks,
                COUNT(*) FILTER (WHERE NOT granted) as waiting_locks,
                COUNT(DISTINCT pid) FILTER (WHERE NOT granted) as waiting_processes
            FROM pg_locks
        """
        result = await self.driver.execute_query(query)

        blocking_query = """
            SELECT COUNT(*) as blocking_count
            FROM pg_stat_activity
            WHERE wait_event_type = 'Lock' AND state = 'active'
        """
        blocking_result = await self.driver.execute_query(blocking_query)

        if result:
            row = result[0]
            waiting = row.get("waiting_locks", 0) or 0
            blocking = blocking_result[0].get("blocking_count", 0) if blocking_result else 0
            score = 100
            if waiting > 10:
                score -= 40
                health["issues"].append(f"High lock contention: {waiting} locks waiting")
            elif waiting > 5:
                score -= 20
            if blocking > 0:
                score -= 30
                health["issues"].append(f"{blocking} queries blocked by locks")
                health["recommendations"].append(
                    "Investigate blocking queries using pg_blocking_pids()"
                )
            health["checks"]["locks"] = {
                "score": max(0, score), "waiting_locks": waiting,
                "blocking_queries": blocking, "details": row,
            }

    async def _check_replication(self, health: dict) -> None:
        query = """
            SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
                pg_wal_lsn_diff(sent_lsn, replay_lsn) as replication_lag_bytes
            FROM pg_stat_replication
        """
        result = await self.driver.execute_query(query)
        score = 100
        if result:
            max_lag = max(r.get("replication_lag_bytes", 0) or 0 for r in result)
            if max_lag > 100 * 1024 * 1024:
                score = 50
                health["issues"].append(f"High replication lag: {max_lag / 1024 / 1024:.1f}MB")
            elif max_lag > 10 * 1024 * 1024:
                score = 80
                health["issues"].append(f"Moderate replication lag: {max_lag / 1024 / 1024:.1f}MB")
            health["checks"]["replication"] = {
                "score": score, "replica_count": len(result),
                "max_lag_bytes": max_lag, "details": result,
            }
        else:
            health["checks"]["replication"] = {
                "score": 100, "replica_count": 0,
                "message": "No replication configured or this is a replica",
            }

    async def _check_wraparound(self, health: dict) -> None:
        query = """
            SELECT datname, age(datfrozenxid) as xid_age,
                2147483647 - age(datfrozenxid) as xids_remaining,
                ROUND(100.0 * age(datfrozenxid) / 2147483647, 2) as pct_towards_wraparound
            FROM pg_database
            WHERE datname NOT IN ('template0', 'template1')
            ORDER BY age(datfrozenxid) DESC
            LIMIT 5
        """
        result = await self.driver.execute_query(query)
        if result:
            max_pct = max(r.get("pct_towards_wraparound", 0) or 0 for r in result)
            score = 100
            if max_pct > 75:
                score = 20
                health["issues"].append(f"Critical: Transaction wraparound at {max_pct}%")
                health["recommendations"].append(
                    "Urgently run VACUUM FREEZE on affected databases"
                )
            elif max_pct > 50:
                score = 70
                health["issues"].append(f"Warning: Transaction wraparound at {max_pct}%")
                health["recommendations"].append(
                    "Schedule VACUUM FREEZE to prevent wraparound"
                )
            health["checks"]["wraparound"] = {
                "score": score, "max_percent": max_pct, "details": result,
            }

    async def _check_disk_usage(self, health: dict) -> None:
        query = """
            SELECT pg_database.datname,
                pg_size_pretty(pg_database_size(pg_database.datname)) as size,
                pg_database_size(pg_database.datname) as size_bytes
            FROM pg_database
            WHERE datname NOT IN ('template0', 'template1')
            ORDER BY pg_database_size(pg_database.datname) DESC
        """
        result = await self.driver.execute_query(query)
        total_size = sum(r.get("size_bytes", 0) or 0 for r in result) if result else 0
        health["checks"]["disk_usage"] = {
            "score": 100,
            "total_database_size_bytes": total_size,
            "total_database_size": f"{total_size / 1024 / 1024 / 1024:.2f} GB",
            "details": result,
        }

    async def _check_bgwriter(self, health: dict) -> None:
        query = """
            SELECT checkpoints_timed, checkpoints_req,
                CASE WHEN checkpoints_timed + checkpoints_req > 0
                     THEN ROUND(100.0 * checkpoints_timed / (checkpoints_timed + checkpoints_req), 1)
                     ELSE 100 END as timed_pct,
                buffers_checkpoint, buffers_clean, buffers_backend,
                CASE WHEN buffers_checkpoint + buffers_clean + buffers_backend > 0
                     THEN ROUND(100.0 * buffers_backend / (buffers_checkpoint + buffers_clean + buffers_backend), 1)
                     ELSE 0 END as backend_pct
            FROM pg_stat_bgwriter
        """
        result = await self.driver.execute_query(query)
        if result:
            row = result[0]
            timed_pct = row.get("timed_pct", 100) or 100
            backend_pct = row.get("backend_pct", 0) or 0
            score = 100
            if timed_pct < 90:
                score -= 20
                health["issues"].append(f"Too many requested checkpoints: {100 - timed_pct}% not timed")
                health["recommendations"].append(
                    "Consider increasing checkpoint_timeout or max_wal_size"
                )
            if backend_pct > 20:
                score -= 30
                health["issues"].append(f"Backend processes doing {backend_pct}% of buffer writes")
                health["recommendations"].append(
                    "Increase shared_buffers or bgwriter settings"
                )
            health["checks"]["bgwriter"] = {
                "score": max(0, score), "timed_checkpoint_pct": timed_pct,
                "backend_write_pct": backend_pct, "details": row,
            }

    async def _check_checkpoints(self, health: dict) -> None:
        query = """
            SELECT total_checkpoints, seconds_since_start,
                CASE WHEN seconds_since_start > 0
                     THEN ROUND(3600.0 * total_checkpoints / seconds_since_start, 2)
                     ELSE 0 END as checkpoints_per_hour
            FROM (
                SELECT checkpoints_timed + checkpoints_req as total_checkpoints,
                    EXTRACT(epoch FROM now() - stats_reset) as seconds_since_start
                FROM pg_stat_bgwriter
            ) s
        """
        result = await self.driver.execute_query(query)
        if result:
            row = result[0]
            cp_per_hour = row.get("checkpoints_per_hour", 0) or 0
            score = 100
            if cp_per_hour > 6:
                score = 70
                health["issues"].append(f"High checkpoint frequency: {cp_per_hour}/hour")
                health["recommendations"].append(
                    "Increase checkpoint_timeout or max_wal_size to reduce checkpoint frequency"
                )
            health["checks"]["checkpoints"] = {
                "score": score, "checkpoints_per_hour": cp_per_hour, "details": row,
            }

    _CRITICAL_SETTING_NAMES: tuple[str, ...] = (
        # memory
        "shared_buffers", "work_mem", "maintenance_work_mem",
        "effective_cache_size", "huge_pages", "temp_buffers",
        # checkpoint
        "checkpoint_timeout", "checkpoint_completion_target",
        "checkpoint_warning", "max_wal_size", "min_wal_size",
        # wal
        "wal_level", "wal_buffers", "wal_compression",
        "synchronous_commit", "fsync", "full_page_writes",
        # autovacuum
        "autovacuum", "autovacuum_max_workers", "autovacuum_naptime",
        "autovacuum_vacuum_threshold", "autovacuum_analyze_threshold",
        "autovacuum_vacuum_scale_factor", "autovacuum_analyze_scale_factor",
        "autovacuum_vacuum_cost_limit",
        # connections
        "max_connections", "superuser_reserved_connections",
        "tcp_keepalives_idle", "tcp_keepalives_interval",
        "statement_timeout", "idle_in_transaction_session_timeout",
    )

    async def _check_critical_settings(self, health: dict) -> None:
        placeholders = ",".join(["%s"] * len(self._CRITICAL_SETTING_NAMES))
        query = f"""
            SELECT name, setting, unit, category, short_desc,
                   context, vartype, source, boot_val, reset_val
            FROM pg_settings
            WHERE name IN ({placeholders})
            ORDER BY category, name
        """
        result = await self.driver.execute_query(
            query, list(self._CRITICAL_SETTING_NAMES)
        ) or []

        recommendations: list[dict[str, str]] = []
        settings_dict = {r["name"]: r for r in result}

        if "shared_buffers" in settings_dict:
            sb = settings_dict["shared_buffers"]
            if self._parse_size(sb["setting"], sb.get("unit")) < 128 * 1024 * 1024:
                recommendations.append({
                    "setting": "shared_buffers",
                    "current": sb["setting"] + (sb.get("unit") or ""),
                    "recommendation": "Consider increasing to at least 25% of system RAM",
                    "severity": "high",
                })

        if "work_mem" in settings_dict:
            wm = settings_dict["work_mem"]
            if self._parse_size(wm["setting"], wm.get("unit")) < 4 * 1024 * 1024:
                recommendations.append({
                    "setting": "work_mem",
                    "current": wm["setting"] + (wm.get("unit") or ""),
                    "recommendation": "Consider increasing for complex queries (4MB-64MB typical)",
                    "severity": "medium",
                })

        if "effective_cache_size" in settings_dict:
            ec = settings_dict["effective_cache_size"]
            recommendations.append({
                "setting": "effective_cache_size",
                "current": ec["setting"] + (ec.get("unit") or ""),
                "recommendation": "Should be ~75% of total system RAM for dedicated DB servers",
                "severity": "info",
            })

        if "checkpoint_completion_target" in settings_dict:
            cct = settings_dict["checkpoint_completion_target"]
            if float(cct["setting"]) < 0.9:
                recommendations.append({
                    "setting": "checkpoint_completion_target",
                    "current": cct["setting"],
                    "recommendation": "Consider increasing to 0.9 to spread checkpoint I/O",
                    "severity": "medium",
                })

        if "autovacuum" in settings_dict:
            av = settings_dict["autovacuum"]
            if av["setting"] == "off":
                recommendations.append({
                    "setting": "autovacuum",
                    "current": "off",
                    "recommendation": "CRITICAL: Enable autovacuum to prevent transaction wraparound",
                    "severity": "critical",
                })

        severity_penalty = {"critical": 40, "high": 20, "medium": 10, "info": 0}
        score = 100
        for rec in recommendations:
            score -= severity_penalty.get(rec.get("severity", "info"), 0)
            if rec.get("severity") in ("critical", "high"):
                health["issues"].append(
                    f"{rec['severity'].capitalize()}: {rec['setting']} = "
                    f"{rec['current']} — {rec['recommendation']}"
                )
            health["recommendations"].append(
                f"{rec['setting']}: {rec['recommendation']}"
            )

        health["checks"]["settings"] = {
            "score": max(0, score),
            "recommendations": recommendations,
            "details": result,
        }
