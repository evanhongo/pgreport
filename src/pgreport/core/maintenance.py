"""Maintenance and storage analyzers.

Covers autovacuum status, table and index bloat (pgstattuple/pgstatindex),
the largest objects on disk, stale ANALYZE statistics, per-database sizes,
sequence exhaustion, freeze prediction, index listing, pending vacuum,
and vacuum history.
"""

from __future__ import annotations

from typing import Any

from ..utils.constants import SYSTEM_SCHEMAS_SQL, gb_to_bytes, sql_pct_ratio
from ..utils.sql_driver import SqlDriver

# ----------------------------------------------------------------------
# ----------------------------------------------------------------------

SQL_TOP_OBJECTS_BY_SIZE = """
    (SELECT 'table' as type, s.schemaname, s.relname as object_name,
        pg_size_pretty(c.relpages * current_setting('block_size')::bigint) as size
    FROM pg_stat_user_tables s
    JOIN pg_class c ON c.oid = s.relid
    ORDER BY c.relpages DESC
    LIMIT %s)
    UNION ALL
    (SELECT 'index' as type, s.schemaname, s.indexrelname as object_name,
        pg_size_pretty(c.relpages * current_setting('block_size')::bigint) as size
    FROM pg_stat_user_indexes s
    JOIN pg_class c ON c.oid = s.indexrelid
    ORDER BY c.relpages DESC
    LIMIT %s)
"""

SQL_STALE_STATISTICS = """
    SELECT schemaname, relname, n_live_tup, last_autoanalyze,
        (n_mod_since_analyze::numeric / GREATEST(n_live_tup, 1) * 100)::numeric(10,2)
            AS modified_percent
    FROM pg_stat_user_tables
    WHERE n_live_tup > 1000
      AND (n_mod_since_analyze::numeric / GREATEST(n_live_tup, 1)) > 0.10
    ORDER BY modified_percent DESC
    LIMIT %s
"""

SQL_DATABASE_SIZES = """
    SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size
    FROM pg_database
    WHERE datistemplate = false
    ORDER BY pg_database_size(datname) DESC
    LIMIT %s
"""

SQL_SEQUENCE_EXHAUSTION = """
    SELECT n.nspname AS schemaname, c.relname AS sequence_name,
        pg_sequence_last_value(c.oid) AS last_val,
        s.seqmax AS max_val,
        (pg_sequence_last_value(c.oid)::numeric * 100) / s.seqmax::numeric
            AS percentage_used
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_sequence s ON s.seqrelid = c.oid
    WHERE c.relkind = 'S'
      AND NOT s.seqcycle
      AND (pg_sequence_last_value(c.oid)::numeric / s.seqmax::numeric) > 0.8
    ORDER BY percentage_used DESC
"""

SQL_FREEZE_PREDICTION = """
    WITH table_mxid_status AS (
        SELECT c.oid, c.relname, n.nspname, c.relminmxid,
            age(c.relfrozenxid) AS xid_age,
            age(c.relminmxid) AS mxid_age,
            CASE
                WHEN c.relminmxid = 0 THEN TRUE
                WHEN age(c.relminmxid) = 2147483647 THEN TRUE
                ELSE FALSE
            END AS is_mxid_invalid
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'm')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    )
    SELECT oid::regclass::text AS table_name, nspname AS schemaname,
        pg_size_pretty(pg_total_relation_size(oid)) AS total_size,
        (current_setting('autovacuum_freeze_max_age')::int - xid_age) AS xid_remain_ages,
        CASE
            WHEN is_mxid_invalid THEN NULL
            ELSE (current_setting('autovacuum_multixact_freeze_max_age')::int - mxid_age)
        END AS mxid_remain_ages,
        CASE
            WHEN (current_setting('autovacuum_freeze_max_age')::int - xid_age) < 0 THEN 'XID_OVERDUE'
            WHEN is_mxid_invalid THEN 'MXID_NA'
            WHEN (current_setting('autovacuum_multixact_freeze_max_age')::int - mxid_age) < 0 THEN 'MXID_OVERDUE'
            WHEN LEAST(
                (current_setting('autovacuum_freeze_max_age')::int - xid_age),
                (current_setting('autovacuum_multixact_freeze_max_age')::int - mxid_age)
            ) < (current_setting('autovacuum_freeze_max_age')::int * 0.10) THEN 'CRITICAL'
            WHEN LEAST(
                (current_setting('autovacuum_freeze_max_age')::int - xid_age),
                (current_setting('autovacuum_multixact_freeze_max_age')::int - mxid_age)
            ) < (current_setting('autovacuum_freeze_max_age')::int * 0.25) THEN 'WARNING'
            ELSE 'OK'
        END AS freeze_status
    FROM table_mxid_status
    WHERE (current_setting('autovacuum_freeze_max_age')::int - xid_age)
        < (current_setting('autovacuum_freeze_max_age')::int * 0.25)
        OR (NOT is_mxid_invalid AND
            (current_setting('autovacuum_multixact_freeze_max_age')::int - mxid_age) <
            (current_setting('autovacuum_multixact_freeze_max_age')::int * 0.25))
    ORDER BY freeze_status, LEAST(
        (current_setting('autovacuum_freeze_max_age')::int - xid_age),
        CASE WHEN is_mxid_invalid THEN 2147483647
            ELSE (current_setting('autovacuum_multixact_freeze_max_age')::int - mxid_age) END
    )
    LIMIT %s
"""

class MaintenanceAnalyzer:
    """Maintenance, storage, vacuum, bloat and unused-index observations."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver

    async def top_objects_by_size(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_TOP_OBJECTS_BY_SIZE, [limit, limit])
        return rows or []

    async def stale_statistics(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_STALE_STATISTICS, [limit])
        return rows or []

    async def database_sizes(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_DATABASE_SIZES, [limit])
        return rows or []

    async def sequence_exhaustion(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_SEQUENCE_EXHAUSTION)
        return rows or []

    async def freeze_prediction(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_FREEZE_PREDICTION, [limit])
        return rows or []

    async def autovacuum_status(self) -> dict[str, Any]:
        settings_query = """
            SELECT name, setting, unit, short_desc
            FROM pg_settings
            WHERE name LIKE 'autovacuum%'
            ORDER BY name
        """
        settings = await self.driver.execute_query(settings_query)

        workers_query = """
            SELECT pid, datname as database,
                SUBSTRING(query FROM 'autovacuum: (.*)') as operation,
                state,
                EXTRACT(epoch FROM now() - xact_start)::integer as duration_seconds,
                wait_event_type, wait_event
            FROM pg_stat_activity
            WHERE backend_type = 'autovacuum worker'
        """
        workers = await self.driver.execute_query(workers_query)

        launcher_query = """
            SELECT pid, state,
                EXTRACT(epoch FROM now() - backend_start)::integer as uptime_seconds
            FROM pg_stat_activity
            WHERE backend_type = 'autovacuum launcher'
        """
        launcher = await self.driver.execute_query(launcher_query)

        settings = settings or []
        workers = workers or []
        settings_map = {s["name"]: s["setting"] for s in settings}
        max_workers = int(settings_map.get("autovacuum_max_workers", 3))
        naptime = int(settings_map.get("autovacuum_naptime", 60))

        output: dict[str, Any] = {
            "autovacuum_enabled": settings_map.get("autovacuum") == "on" if settings_map else True,
            "settings": settings,
            "launcher": launcher[0] if launcher else None,
            "active_workers": workers,
            "summary": {
                "max_workers": max_workers,
                "active_worker_count": len(workers),
                "naptime_seconds": naptime,
                "workers_available": max_workers - len(workers),
            },
        }
        if output["summary"]["workers_available"] == 0:
            output["warning"] = "All autovacuum workers are busy - consider increasing autovacuum_max_workers"
        return output

    async def list_indexes(
        self, table_name: str, schema_name: str = "public"
    ) -> dict[str, Any]:
        query = """
            SELECT
                i.relname as index_name,
                am.amname as access_method,
                array_agg(a.attname ORDER BY x.ordinality) as columns,
                ix.indisunique as is_unique,
                ix.indisprimary as is_primary,
                ix.indisvalid as is_valid,
                pg_size_pretty(pg_relation_size(i.oid)) as size,
                pg_relation_size(i.oid) as size_bytes,
                pg_get_indexdef(i.oid) as definition
            FROM pg_index ix
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_am am ON am.oid = i.relam
            JOIN pg_attribute a ON a.attrelid = t.oid
            JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS x(attnum, ordinality)
                ON a.attnum = x.attnum
            WHERE t.relname = %s
              AND n.nspname = %s
            GROUP BY i.relname, am.amname, ix.indisunique, ix.indisprimary, ix.indisvalid, i.oid
            ORDER BY ix.indisprimary DESC, i.relname
        """
        results = await self.driver.execute_query(query, [table_name, schema_name])
        stats_query = """
            SELECT indexrelname as index_name,
                idx_scan as scans, idx_tup_read as tuples_read, idx_tup_fetch as tuples_fetched
            FROM pg_stat_user_indexes
            WHERE relname = %s AND schemaname = %s
        """
        stats = await self.driver.execute_query(stats_query, [table_name, schema_name])
        stats_map = {r["index_name"]: r for r in stats} if stats else {}

        indexes = []
        total_size = 0
        for idx in (results or []):
            idx_stats = stats_map.get(idx["index_name"], {})
            indexes.append({
                **idx,
                "scans": idx_stats.get("scans", 0),
                "tuples_read": idx_stats.get("tuples_read", 0),
                "tuples_fetched": idx_stats.get("tuples_fetched", 0),
            })
            total_size += idx.get("size_bytes", 0) or 0

        return {
            "table": table_name,
            "schema": schema_name,
            "index_count": len(indexes),
            "total_index_size_bytes": total_size,
            "indexes": indexes,
        }

    async def table_bloat(
        self,
        table_name: str | None = None,
        schema_name: str = "public",
        use_approx: bool = False,
        min_table_size_gb: float = 5,
        include_toast: bool = False,
        min_bloat_percent: float = 20,
    ) -> dict[str, Any]:
        ext_check = await self._check_pgstattuple()
        if not ext_check["available"]:
            return {
                "error": (
                    "pgstattuple extension is not installed.\n"
                    "Install it with: CREATE EXTENSION IF NOT EXISTS pgstattuple;\n\n"
                    "Note: You may need superuser privileges or the pg_stat_scan_tables role."
                )
            }
        if table_name:
            return await self._analyze_single_table(
                schema_name, table_name, use_approx, include_toast
            )
        return await self._analyze_schema_tables(
            schema_name, use_approx, min_table_size_gb, min_bloat_percent
        )

    async def index_bloat(
        self,
        index_name: str | None = None,
        table_name: str | None = None,
        schema_name: str = "public",
        min_index_size_gb: float = 5,
        min_bloat_percent: float = 20,
    ) -> dict[str, Any]:
        ext_check = await self._check_pgstattuple()
        if not ext_check["available"]:
            return {
                "error": (
                    "pgstattuple extension is not installed.\n"
                    "Install it with: CREATE EXTENSION IF NOT EXISTS pgstattuple;"
                )
            }
        if index_name:
            return await self._analyze_single_index(schema_name, index_name)
        if table_name:
            return await self._analyze_table_indexes(
                schema_name, table_name, min_index_size_gb, min_bloat_percent
            )
        return await self._analyze_schema_indexes(
            schema_name, min_index_size_gb, min_bloat_percent
        )

    async def pending_vacuum(
        self,
        schema_name: str | None = "public",
        include_toast: bool = False,
        min_dead_tuples: int = 1000,
    ) -> dict[str, Any]:
        toast_filter = "" if include_toast else "AND c.relname NOT LIKE 'pg_toast%%'"
        schema_filter = ""
        schema_params: list[Any] = []
        if schema_name:
            schema_filter = "AND n.nspname = %s"
            schema_params = [schema_name]
        params: list[Any] = [min_dead_tuples] + schema_params

        query = f"""
            SELECT
                n.nspname as schema_name,
                c.relname as table_name,
                s.n_live_tup, s.n_dead_tup,
                {sql_pct_ratio("s.n_dead_tup", "s.n_live_tup", "dead_tuple_ratio")},
                s.last_vacuum, s.last_autovacuum,
                s.vacuum_count, s.autovacuum_count,
                pg_size_pretty(pg_table_size(c.oid)) as table_size,
                pg_table_size(c.oid) as table_size_bytes,
                COALESCE(
                    (SELECT setting::integer FROM pg_settings WHERE name = 'autovacuum_vacuum_threshold'), 50
                ) +
                COALESCE(
                    (SELECT setting::float FROM pg_settings WHERE name = 'autovacuum_vacuum_scale_factor'), 0.2
                ) * s.n_live_tup as autovacuum_threshold,
                CASE
                    WHEN s.n_dead_tup > (
                        COALESCE((SELECT setting::integer FROM pg_settings WHERE name = 'autovacuum_vacuum_threshold'), 50) +
                        COALESCE((SELECT setting::float FROM pg_settings WHERE name = 'autovacuum_vacuum_scale_factor'), 0.2) * s.n_live_tup
                    )
                    THEN true ELSE false
                END as exceeds_threshold
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND s.n_dead_tup >= %s
              {schema_filter}
              {toast_filter}
            ORDER BY s.n_dead_tup DESC
            LIMIT 50
        """
        results = await self.driver.execute_query(query, params)

        wraparound_query = f"""
            SELECT
                n.nspname as schema_name,
                c.relname as table_name,
                age(c.relfrozenxid) as xid_age,
                pg_size_pretty(pg_table_size(c.oid)) as table_size,
                ROUND(100.0 * age(c.relfrozenxid) / 2147483647, 2) as pct_towards_wraparound
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND age(c.relfrozenxid) > 100000000
            ORDER BY age(c.relfrozenxid) DESC
            LIMIT 20
        """
        wraparound_results = await self.driver.execute_query(wraparound_query)

        results = results or []
        wraparound_results = wraparound_results or []

        output: dict[str, Any] = {
            "tables_needing_vacuum": results,
            "tables_with_wraparound_risk": wraparound_results,
            "summary": {
                "tables_with_dead_tuples": len(results),
                "tables_exceeding_threshold": sum(1 for r in results if r.get("exceeds_threshold")),
                "tables_with_wraparound_risk": len(wraparound_results),
            },
            "recommendations": [],
        }
        if results:
            high_dead_ratio = [r for r in results if (r.get("dead_tuple_ratio") or 0) > 20]
            if high_dead_ratio:
                output["recommendations"].append({
                    "severity": "high",
                    "message": f"{len(high_dead_ratio)} tables have > 20% dead tuple ratio",
                    "action": "Consider running VACUUM on these tables",
                })
        if wraparound_results:
            critical = [r for r in wraparound_results if (r.get("pct_towards_wraparound") or 0) > 50]
            if critical:
                output["recommendations"].append({
                    "severity": "critical",
                    "message": f"{len(critical)} tables are > 50% towards transaction wraparound",
                    "action": "Urgently run VACUUM FREEZE on these tables",
                })
        return output

    async def vacuum_history(
        self, schema_name: str | None = "public", include_toast: bool = False
    ) -> dict[str, Any]:
        toast_filter = "" if include_toast else "AND c.relname NOT LIKE 'pg_toast%%'"
        schema_filter = ""
        schema_params: list[Any] = []
        if schema_name:
            schema_filter = "AND n.nspname = %s"
            schema_params = [schema_name]

        query = f"""
            SELECT
                n.nspname as schema_name, c.relname as table_name,
                s.last_vacuum, s.last_autovacuum, s.last_analyze, s.last_autoanalyze,
                s.vacuum_count, s.autovacuum_count, s.analyze_count, s.autoanalyze_count,
                s.n_live_tup, s.n_dead_tup, s.n_mod_since_analyze,
                pg_size_pretty(pg_table_size(c.oid)) as table_size,
                GREATEST(s.last_vacuum, s.last_autovacuum) as last_vacuumed,
                CASE
                    WHEN GREATEST(s.last_vacuum, s.last_autovacuum) IS NULL THEN 'never'
                    WHEN GREATEST(s.last_vacuum, s.last_autovacuum) < now() - interval '7 days' THEN 'stale'
                    WHEN GREATEST(s.last_vacuum, s.last_autovacuum) < now() - interval '1 day' THEN 'recent'
                    ELSE 'fresh'
                END as vacuum_status
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
              {schema_filter}
              {toast_filter}
            ORDER BY GREATEST(s.last_vacuum, s.last_autovacuum) DESC NULLS LAST
            LIMIT 50
        """
        results = await self.driver.execute_query(
            query, schema_params if schema_params else None
        )
        if not results:
            return {
                "message": f"No tables found in schema: {schema_name or 'all'}",
                "recent_activity": [],
                "summary": {
                    "total_tables": 0, "never_vacuumed": 0,
                    "stale_vacuum": 0, "recent_vacuum": 0, "fresh_vacuum": 0,
                },
                "recommendations": [],
            }
        never_vacuumed = [r for r in results if r.get("vacuum_status") == "never"]
        stale = [r for r in results if r.get("vacuum_status") == "stale"]
        recent = [r for r in results if r.get("vacuum_status") == "recent"]
        fresh = [r for r in results if r.get("vacuum_status") == "fresh"]
        output: dict[str, Any] = {
            "recent_activity": results,
            "summary": {
                "total_tables": len(results),
                "never_vacuumed": len(never_vacuumed),
                "stale_vacuum": len(stale),
                "recent_vacuum": len(recent),
                "fresh_vacuum": len(fresh),
            },
            "recommendations": [],
        }
        if never_vacuumed:
            output["recommendations"].append({
                "severity": "warning",
                "message": f"{len(never_vacuumed)} tables have never been vacuumed",
                "tables": [t["table_name"] for t in never_vacuumed[:5]],
            })
        if stale:
            output["recommendations"].append({
                "severity": "info",
                "message": f"{len(stale)} tables haven't been vacuumed in over 7 days",
                "tables": [t["table_name"] for t in stale[:5]],
            })
        return output

    async def _check_pgstattuple(self) -> dict[str, Any]:
        query = """
            SELECT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'pgstattuple'
            ) as available,
            (SELECT extversion FROM pg_extension WHERE extname = 'pgstattuple') as version
        """
        result = await self.driver.execute_query(query)
        if result:
            return {
                "available": result[0].get("available", False),
                "version": result[0].get("version"),
            }
        return {"available": False, "version": None}

    async def _analyze_single_table(
        self, schema_name: str, table_name: str, use_approx: bool, include_toast: bool
    ) -> dict[str, Any]:
        size_query = """
            SELECT
                pg_total_relation_size(quote_ident(%s) || '.' || quote_ident(%s)) as total_size,
                pg_table_size(quote_ident(%s) || '.' || quote_ident(%s)) as table_size,
                pg_indexes_size(quote_ident(%s) || '.' || quote_ident(%s)) as indexes_size
        """
        size_result = await self.driver.execute_query(
            size_query,
            (schema_name, table_name, schema_name, table_name, schema_name, table_name),
        )
        table_size = size_result[0] if size_result else {}
        stats_func = "pgstattuple_approx" if use_approx else "pgstattuple"
        stats_query = f"""
            SELECT * FROM {stats_func}(quote_ident(%s) || '.' || quote_ident(%s))
        """
        stats_result = await self.driver.execute_query(
            stats_query, (schema_name, table_name)
        )
        if not stats_result:
            return {
                "error": f"Could not analyze table {schema_name}.{table_name}",
                "hint": "Make sure the table exists and you have permissions to access it",
            }
        stats = stats_result[0]
        if use_approx:
            result = self._build_approx_result(schema_name, table_name, stats, table_size)
        else:
            result = self._build_exact_result(schema_name, table_name, stats, table_size)
        result["recommendations"] = self._table_recommendations(result)
        if include_toast:
            toast_result = await self._analyze_toast_table(schema_name, table_name, use_approx)
            if toast_result:
                result["toast_table"] = toast_result
        return result

    def _build_exact_result(
        self, schema_name: str, table_name: str, stats: dict, table_size: dict
    ) -> dict[str, Any]:
        table_len = stats.get("table_len", 0) or 0
        tuple_len = stats.get("tuple_len", 0) or 0
        dead_tuple_len = stats.get("dead_tuple_len", 0) or 0
        free_space = stats.get("free_space", 0) or 0
        tuple_percent = stats.get("tuple_percent", 0) or 0
        dead_tuple_percent = stats.get("dead_tuple_percent", 0) or 0
        free_percent = stats.get("free_percent", 0) or 0
        wasted_space = dead_tuple_len + free_space
        wasted_percent = round(100.0 * wasted_space / table_len, 2) if table_len > 0 else 0
        bloat_analysis = self._table_bloat_severity(dead_tuple_percent, free_percent, tuple_percent)
        return {
            "schema": schema_name, "table_name": table_name, "analysis_type": "exact",
            "size": {
                "table_len_bytes": table_len,
                "table_len_pretty": _format_bytes(table_len),
                "total_relation_size": table_size.get("total_size"),
                "total_relation_size_pretty": _format_bytes(table_size.get("total_size", 0)),
                "indexes_size": table_size.get("indexes_size"),
                "indexes_size_pretty": _format_bytes(table_size.get("indexes_size", 0)),
            },
            "tuples": {
                "live_tuple_count": stats.get("tuple_count", 0),
                "live_tuple_len": tuple_len,
                "live_tuple_percent": tuple_percent,
                "dead_tuple_count": stats.get("dead_tuple_count", 0),
                "dead_tuple_len": dead_tuple_len,
                "dead_tuple_percent": dead_tuple_percent,
            },
            "free_space": {
                "free_space_bytes": free_space,
                "free_space_pretty": _format_bytes(free_space),
                "free_percent": free_percent,
            },
            "bloat": {
                "wasted_space_bytes": wasted_space,
                "wasted_space_pretty": _format_bytes(wasted_space),
                "wasted_percent": wasted_percent,
                "bloat_severity": bloat_analysis["overall_severity"],
                "dead_tuple_status": bloat_analysis["dead_tuple_status"],
                "free_space_status": bloat_analysis["free_space_status"],
                "tuple_density_status": bloat_analysis["tuple_density_status"],
                "issues": bloat_analysis["issues"],
            },
        }

    def _build_approx_result(
        self, schema_name: str, table_name: str, stats: dict, table_size: dict
    ) -> dict[str, Any]:
        table_len = stats.get("table_len", 0) or 0
        approx_tuple_len = stats.get("approx_tuple_len", 0) or 0
        dead_tuple_len = stats.get("dead_tuple_len", 0) or 0
        approx_free_space = stats.get("approx_free_space", 0) or 0
        approx_tuple_percent = stats.get("approx_tuple_percent", 0) or 0
        dead_tuple_percent = stats.get("dead_tuple_percent", 0) or 0
        approx_free_percent = stats.get("approx_free_percent", 0) or 0
        wasted_space = dead_tuple_len + approx_free_space
        wasted_percent = round(100.0 * wasted_space / table_len, 2) if table_len > 0 else 0
        bloat_analysis = self._table_bloat_severity(dead_tuple_percent, approx_free_percent, approx_tuple_percent)
        return {
            "schema": schema_name, "table_name": table_name, "analysis_type": "approximate",
            "scanned_percent": stats.get("scanned_percent", 0),
            "size": {
                "table_len_bytes": table_len,
                "table_len_pretty": _format_bytes(table_len),
                "total_relation_size": table_size.get("total_size"),
                "total_relation_size_pretty": _format_bytes(table_size.get("total_size", 0)),
                "indexes_size": table_size.get("indexes_size"),
                "indexes_size_pretty": _format_bytes(table_size.get("indexes_size", 0)),
            },
            "tuples": {
                "approx_live_tuple_count": stats.get("approx_tuple_count", 0),
                "approx_live_tuple_len": approx_tuple_len,
                "approx_live_tuple_percent": approx_tuple_percent,
                "dead_tuple_count": stats.get("dead_tuple_count", 0),
                "dead_tuple_len": dead_tuple_len,
                "dead_tuple_percent": dead_tuple_percent,
            },
            "free_space": {
                "approx_free_space_bytes": approx_free_space,
                "approx_free_space_pretty": _format_bytes(approx_free_space),
                "approx_free_percent": approx_free_percent,
            },
            "bloat": {
                "wasted_space_bytes": wasted_space,
                "wasted_space_pretty": _format_bytes(wasted_space),
                "wasted_percent": wasted_percent,
                "bloat_severity": bloat_analysis["overall_severity"],
                "dead_tuple_status": bloat_analysis["dead_tuple_status"],
                "free_space_status": bloat_analysis["free_space_status"],
                "tuple_density_status": bloat_analysis["tuple_density_status"],
                "issues": bloat_analysis["issues"],
            },
        }

    async def _analyze_schema_tables(
        self, schema_name: str, use_approx: bool, min_size_gb: float, min_bloat_percent: float = 20
    ) -> dict[str, Any]:
        min_size_bytes = gb_to_bytes(min_size_gb)
        tables_query = f"""
            SELECT c.relname as table_name, pg_table_size(c.oid) as table_size
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = %s
              AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND pg_table_size(c.oid) >= %s::bigint
            ORDER BY pg_table_size(c.oid) DESC
        """
        tables = await self.driver.execute_query(
            tables_query, (schema_name, min_size_bytes)
        )
        if not tables:
            return {
                "schema": schema_name,
                "message": f"No tables found with size >= {min_size_gb}GB",
                "tables": [],
            }
        results: list[dict[str, Any]] = []
        total_wasted = 0
        total_size = 0
        for table in tables:
            try:
                table_result = await self._analyze_single_table(
                    schema_name, table["table_name"], use_approx, include_toast=False
                )
                if "error" not in table_result:
                    bloat_info = table_result.get("bloat", {})
                    tuples_info = table_result.get("tuples", {})
                    free_space_info = table_result.get("free_space", {})
                    tuple_percent = tuples_info.get("live_tuple_percent", 0) or tuples_info.get(
                        "approx_live_tuple_percent", 0
                    )
                    free_percent = free_space_info.get("free_percent", 0) or free_space_info.get(
                        "approx_free_percent", 0
                    )
                    results.append({
                        "table_name": table["table_name"],
                        "table_size": table_result.get("size", {}).get("table_len_bytes", 0),
                        "table_size_pretty": table_result.get("size", {}).get("table_len_pretty", "0"),
                        "dead_tuple_percent": tuples_info.get("dead_tuple_percent", 0),
                        "free_percent": free_percent,
                        "tuple_percent": tuple_percent,
                        "wasted_space_bytes": bloat_info.get("wasted_space_bytes", 0),
                        "wasted_space_pretty": bloat_info.get("wasted_space_pretty", "0"),
                        "wasted_percent": bloat_info.get("wasted_percent", 0),
                        "bloat_severity": bloat_info.get("bloat_severity", "low"),
                    })
                    total_wasted += bloat_info.get("wasted_space_bytes", 0)
                    total_size += table_result.get("size", {}).get("table_len_bytes", 0)
            except Exception as e:
                results.append({"table_name": table["table_name"], "error": str(e)})
        results.sort(key=lambda x: x.get("wasted_space_bytes", 0), reverse=True)
        if min_bloat_percent > 0:
            results = [r for r in results if r.get("wasted_percent", 0) >= min_bloat_percent or "error" in r]
        return {
            "schema": schema_name,
            "analysis_type": "approximate" if use_approx else "exact",
            "tables_analyzed": len(results),
            "summary": {
                "total_table_size": total_size,
                "total_table_size_pretty": _format_bytes(total_size),
                "total_wasted_space": total_wasted,
                "total_wasted_space_pretty": _format_bytes(total_wasted),
                "overall_wasted_percent": round(100.0 * total_wasted / total_size, 2) if total_size > 0 else 0,
            },
            "tables": results,
            "recommendations": self._schema_recommendations(results),
        }

    async def _analyze_toast_table(
        self, schema_name: str, table_name: str, use_approx: bool
    ) -> dict[str, Any] | None:
        toast_query = """
            SELECT t.relname as toast_table_name,
                pg_relation_size(t.oid) as toast_size
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
            WHERE c.relname = %s AND n.nspname = %s AND t.relname IS NOT NULL
        """
        toast_result = await self.driver.execute_query(
            toast_query, (table_name, schema_name)
        )
        if not toast_result or not toast_result[0].get("toast_table_name"):
            return None
        toast_table = toast_result[0]["toast_table_name"]
        stats_func = "pgstattuple_approx" if use_approx else "pgstattuple"
        stats_query = f"SELECT * FROM {stats_func}(%s::regclass)"
        try:
            stats = await self.driver.execute_query(
                stats_query, (f"pg_toast.{toast_table}",)
            )
            if stats:
                return {
                    "toast_table_name": toast_table,
                    "toast_size": toast_result[0]["toast_size"],
                    "toast_size_pretty": _format_bytes(toast_result[0]["toast_size"]),
                    "stats": stats[0],
                }
        except Exception:
            pass
        return None

    def _table_bloat_severity(
        self, dead_tuple_percent: float, free_percent: float, tuple_percent: float
    ) -> dict[str, Any]:
        severity_result: dict[str, Any] = {
            "overall_severity": "minimal",
            "dead_tuple_status": "normal",
            "free_space_status": "normal",
            "tuple_density_status": "normal",
            "issues": [],
        }
        score = 0
        if dead_tuple_percent > 30:
            severity_result["dead_tuple_status"] = "critical"
            severity_result["issues"].append(
                f"Dead tuple percent ({dead_tuple_percent:.1f}%) is critical (>30%). "
                "Manual VACUUM recommended."
            )
            score += 3
        elif dead_tuple_percent > 10:
            severity_result["dead_tuple_status"] = "warning"
            severity_result["issues"].append(
                f"Dead tuple percent ({dead_tuple_percent:.1f}%) indicates autovacuum lag (>10%). "
                "Tune autovacuum settings."
            )
            score += 2
        if free_percent > 30:
            severity_result["free_space_status"] = "critical"
            severity_result["issues"].append(
                f"Free space ({free_percent:.1f}%) is very high (>30%). "
                "Consider VACUUM FULL, CLUSTER, or pg_repack."
            )
            score += 3
        elif free_percent > 20:
            severity_result["free_space_status"] = "warning"
            severity_result["issues"].append(
                f"Free space ({free_percent:.1f}%) indicates page fragmentation (>20%). "
                "Consider VACUUM FULL or CLUSTER."
            )
            score += 2
        if tuple_percent < 50:
            severity_result["tuple_density_status"] = "critical"
            severity_result["issues"].append(
                f"Tuple density ({tuple_percent:.1f}%) is critically low (<50%). "
                f"Only {tuple_percent:.1f}% of table contains real data. VACUUM FULL strongly recommended."
            )
            score += 3
        elif tuple_percent < 70:
            severity_result["tuple_density_status"] = "warning"
            severity_result["issues"].append(
                f"Tuple density ({tuple_percent:.1f}%) is low (<70%). "
                "Heavy bloat detected. VACUUM FULL likely needed."
            )
            score += 2
        if score >= 6:
            severity_result["overall_severity"] = "critical"
        elif score >= 4:
            severity_result["overall_severity"] = "high"
        elif score >= 2:
            severity_result["overall_severity"] = "moderate"
        elif score >= 1:
            severity_result["overall_severity"] = "low"
        return severity_result

    def _table_recommendations(self, result: dict) -> list[str]:
        recommendations: list[str] = []
        tuples = result.get("tuples", {})
        free_space = result.get("free_space", {})
        table_name = result.get("table_name", "")
        schema = result.get("schema", "public")
        full_name = f"{schema}.{table_name}"
        dead_percent = tuples.get("dead_tuple_percent", 0)
        free_percent = free_space.get("free_percent", 0) or free_space.get("approx_free_percent", 0)
        tuple_percent = tuples.get("live_tuple_percent", 0) or tuples.get("approx_live_tuple_percent", 0)
        if dead_percent > 30:
            recommendations.append(
                f"CRITICAL: Dead tuple percent ({dead_percent:.1f}%) is very high. "
                f"Manual VACUUM recommended: VACUUM ANALYZE {full_name};"
            )
            recommendations.append(
                "Consider tuning autovacuum settings:\n"
                f"  ALTER TABLE {full_name} SET (\n"
                "    autovacuum_vacuum_scale_factor = 0.05,\n"
                "    autovacuum_vacuum_threshold = 50\n"
                "  );"
            )
        elif dead_percent > 10:
            recommendations.append(
                f"WARNING: Dead tuple percent ({dead_percent:.1f}%) indicates autovacuum lag. "
                "Tune autovacuum settings:\n"
                f"  ALTER TABLE {full_name} SET (autovacuum_vacuum_scale_factor = 0.1);"
            )
        if free_percent > 30:
            recommendations.append(
                f"CRITICAL: Free space ({free_percent:.1f}%) indicates severe page fragmentation. "
                f"Strongly recommend one of:\n"
                f"  - VACUUM FULL {full_name}; (requires exclusive lock)\n"
                f"  - CLUSTER {full_name} USING <primary_key>; (requires exclusive lock)\n"
                f"  - pg_repack -t {full_name} (online, no locks)"
            )
        elif free_percent > 20:
            recommendations.append(
                f"WARNING: Free space ({free_percent:.1f}%) indicates page fragmentation. "
                f"Consider during maintenance window:\n"
                f"  - VACUUM FULL {full_name};\n"
                f"  - Or use pg_repack for online compaction: pg_repack -t {full_name}"
            )
        if 0 < tuple_percent < 50:
            recommendations.append(
                f"CRITICAL: Only {tuple_percent:.1f}% of table contains live data. "
                f"~{100 - tuple_percent:.1f}% of space is wasted. VACUUM FULL strongly recommended:\n"
                f"  VACUUM FULL {full_name};"
            )
        elif 0 < tuple_percent < 70:
            recommendations.append(
                f"WARNING: Tuple density ({tuple_percent:.1f}%) is low. "
                f"~{100 - tuple_percent:.1f}% of space is wasted (dead tuples + free space). "
                "VACUUM FULL likely needed."
            )
        if not recommendations:
            if dead_percent < 10 and free_percent <= 20 and tuple_percent >= 70:
                recommendations.append(
                    f"Table {full_name} is healthy. No bloat concerns detected."
                )
            else:
                recommendations.append(
                    f"Table {full_name} has minimal bloat. Continue monitoring."
                )
        return recommendations

    def _schema_recommendations(self, tables: list[dict]) -> list[str]:
        recommendations: list[str] = []
        critical_tables = [t for t in tables if t.get("bloat_severity") == "critical"]
        high_tables = [t for t in tables if t.get("bloat_severity") == "high"]
        if critical_tables:
            table_list = ", ".join(t["table_name"] for t in critical_tables[:5])
            recommendations.append(
                f"CRITICAL: {len(critical_tables)} tables have critical bloat levels. "
                f"Priority tables: {table_list}"
            )
            recommendations.append(
                "Schedule VACUUM FULL or pg_repack for critical tables during maintenance window."
            )
        if high_tables:
            table_list = ", ".join(t["table_name"] for t in high_tables[:5])
            recommendations.append(
                f"HIGH: {len(high_tables)} tables have high bloat levels. Tables: {table_list}"
            )
        high_dead_tables = [t for t in tables if t.get("dead_tuple_percent", 0) > 10]
        if high_dead_tables:
            recommendations.append(
                f"{len(high_dead_tables)} tables have >10% dead tuples (autovacuum lag). "
                "Review autovacuum settings and ensure it's running properly. "
                "Consider lowering autovacuum_vacuum_scale_factor."
            )
        fragmented_tables = [t for t in tables if t.get("free_percent", 0) > 20]
        if fragmented_tables:
            table_list = ", ".join(t["table_name"] for t in fragmented_tables[:5])
            recommendations.append(
                f"{len(fragmented_tables)} tables have >20% free space (page fragmentation). "
                f"Tables: {table_list}. Consider VACUUM FULL or pg_repack to compact pages."
            )
        low_density_tables = [t for t in tables if 0 < t.get("tuple_percent", 100) < 70]
        if low_density_tables:
            table_list = ", ".join(t["table_name"] for t in low_density_tables[:5])
            recommendations.append(
                f"{len(low_density_tables)} tables have <70% tuple density (heavy bloat). "
                f"Tables: {table_list}. VACUUM FULL strongly recommended for these tables."
            )
        return recommendations

    async def _analyze_single_index(
        self, schema_name: str, index_name: str
    ) -> dict[str, Any]:
        info_query = """
            SELECT
                i.relname as index_name, t.relname as table_name,
                am.amname as index_type,
                pg_relation_size(i.oid) as index_size,
                idx.indisunique as is_unique,
                idx.indisprimary as is_primary,
                pg_get_indexdef(i.oid) as definition
            FROM pg_class i
            JOIN pg_namespace n ON n.oid = i.relnamespace
            JOIN pg_am am ON am.oid = i.relam
            JOIN pg_index idx ON idx.indexrelid = i.oid
            JOIN pg_class t ON t.oid = idx.indrelid
            WHERE i.relname = %s AND n.nspname = %s
        """
        info_result = await self.driver.execute_query(
            info_query, (index_name, schema_name)
        )
        if not info_result:
            return {"error": f"Index {schema_name}.{index_name} not found"}
        info = info_result[0]
        index_type = info["index_type"]
        if index_type == "btree":
            stats = await self._btree_stats(schema_name, index_name)
        elif index_type == "gin":
            stats = await self._gin_stats(schema_name, index_name)
        elif index_type == "hash":
            stats = await self._hash_stats(schema_name, index_name)
        else:
            stats = {"note": f"pgstattuple does not support {index_type} indexes directly"}
        return {
            "schema": schema_name, "index_name": index_name,
            "table_name": info["table_name"], "index_type": index_type,
            "is_unique": info["is_unique"], "is_primary": info["is_primary"],
            "size": {"bytes": info["index_size"], "pretty": _format_bytes(info["index_size"])},
            "definition": info["definition"],
            "statistics": stats,
            "recommendations": self._index_recommendations(stats, index_type, info),
        }

    async def _btree_stats(self, schema_name: str, index_name: str) -> dict[str, Any]:
        query = "SELECT * FROM pgstatindex(quote_ident(%s) || '.' || quote_ident(%s))"
        result = await self.driver.execute_query(query, (schema_name, index_name))
        if not result:
            return {"error": "Could not get index statistics"}
        stats = result[0]
        avg_density = stats.get("avg_leaf_density", 90) or 90
        leaf_pages = stats.get("leaf_pages", 1) or 1
        empty_pages = stats.get("empty_pages", 0) or 0
        deleted_pages = stats.get("deleted_pages", 0) or 0
        free_percent = round(100.0 * (empty_pages + deleted_pages) / leaf_pages, 2) if leaf_pages > 0 else 0
        bloat_analysis = self._index_bloat_severity(avg_density, free_percent)
        return {
            "version": stats.get("version"),
            "tree_level": stats.get("tree_level"),
            "index_size": stats.get("index_size"),
            "root_block_no": stats.get("root_block_no"),
            "internal_pages": stats.get("internal_pages"),
            "leaf_pages": leaf_pages,
            "empty_pages": empty_pages,
            "deleted_pages": deleted_pages,
            "avg_leaf_density": avg_density,
            "leaf_fragmentation": stats.get("leaf_fragmentation"),
            "free_percent": free_percent,
            "estimated_bloat_percent": bloat_analysis["estimated_bloat_percent"],
            "bloat_severity": bloat_analysis["overall_severity"],
            "density_status": bloat_analysis["density_status"],
            "issues": bloat_analysis["issues"],
        }

    async def _gin_stats(self, schema_name: str, index_name: str) -> dict[str, Any]:
        query = "SELECT * FROM pgstatginindex(quote_ident(%s) || '.' || quote_ident(%s))"
        result = await self.driver.execute_query(query, (schema_name, index_name))
        if not result:
            return {"error": "Could not get GIN index statistics"}
        stats = result[0]
        return {
            "version": stats.get("version"),
            "pending_pages": stats.get("pending_pages"),
            "pending_tuples": stats.get("pending_tuples"),
            "note": "GIN indexes with many pending tuples may need VACUUM to merge pending entries",
        }

    async def _hash_stats(self, schema_name: str, index_name: str) -> dict[str, Any]:
        query = "SELECT * FROM pgstathashindex(quote_ident(%s) || '.' || quote_ident(%s))"
        result = await self.driver.execute_query(query, (schema_name, index_name))
        if not result:
            return {"error": "Could not get Hash index statistics"}
        stats = result[0]
        return {
            "version": stats.get("version"),
            "bucket_pages": stats.get("bucket_pages"),
            "overflow_pages": stats.get("overflow_pages"),
            "bitmap_pages": stats.get("bitmap_pages"),
            "unused_pages": stats.get("unused_pages"),
            "live_items": stats.get("live_items"),
            "dead_items": stats.get("dead_items"),
            "free_percent": stats.get("free_percent"),
        }

    async def _analyze_table_indexes(
        self, schema_name: str, table_name: str, min_size_gb: float, min_bloat_percent: float
    ) -> dict[str, Any]:
        min_size_bytes = gb_to_bytes(min_size_gb)
        indexes_query = """
            SELECT i.relname as index_name, am.amname as index_type,
                pg_relation_size(i.oid) as index_size
            FROM pg_class i
            JOIN pg_namespace n ON n.oid = i.relnamespace
            JOIN pg_am am ON am.oid = i.relam
            JOIN pg_index idx ON idx.indexrelid = i.oid
            JOIN pg_class t ON t.oid = idx.indrelid
            WHERE t.relname = %s AND n.nspname = %s
              AND pg_relation_size(i.oid) >= %s::bigint
            ORDER BY pg_relation_size(i.oid) DESC
        """
        indexes = await self.driver.execute_query(
            indexes_query, (table_name, schema_name, min_size_bytes)
        )
        if not indexes:
            return {
                "schema": schema_name, "table_name": table_name,
                "message": f"No indexes found with size >= {min_size_gb}GB",
                "indexes": [],
            }
        results: list[dict[str, Any]] = []
        for idx in indexes:
            try:
                idx_result = await self._analyze_single_index(
                    schema_name, idx["index_name"]
                )
                if "error" not in idx_result:
                    stats = idx_result.get("statistics", {})
                    bloat_pct = stats.get("estimated_bloat_percent", 0)
                    if bloat_pct >= min_bloat_percent or idx["index_type"] != "btree":
                        results.append(idx_result)
            except Exception as e:
                results.append({"index_name": idx["index_name"], "error": str(e)})
        return {
            "schema": schema_name, "table_name": table_name,
            "indexes_analyzed": len(indexes),
            "indexes_with_bloat": len(results),
            "min_bloat_threshold": min_bloat_percent,
            "indexes": results,
        }

    async def _analyze_schema_indexes(
        self, schema_name: str, min_size_gb: float, min_bloat_percent: float
    ) -> dict[str, Any]:
        min_size_bytes = gb_to_bytes(min_size_gb)
        indexes_query = f"""
            SELECT i.relname as index_name, t.relname as table_name,
                am.amname as index_type,
                pg_relation_size(i.oid) as index_size
            FROM pg_class i
            JOIN pg_namespace n ON n.oid = i.relnamespace
            JOIN pg_am am ON am.oid = i.relam
            JOIN pg_index idx ON idx.indexrelid = i.oid
            JOIN pg_class t ON t.oid = idx.indrelid
            WHERE n.nspname = %s
              AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND am.amname = 'btree'
              AND pg_relation_size(i.oid) >= %s::bigint
            ORDER BY pg_relation_size(i.oid) DESC
            LIMIT 50
        """
        indexes = await self.driver.execute_query(
            indexes_query, (schema_name, min_size_bytes)
        )
        if not indexes:
            return {
                "schema": schema_name,
                "message": f"No B-tree indexes found with size >= {min_size_gb}GB",
                "indexes": [],
            }
        results: list[dict[str, Any]] = []
        total_size = 0
        total_bloated_size = 0
        for idx in indexes:
            try:
                stats = await self._btree_stats(schema_name, idx["index_name"])
                if "error" not in stats:
                    bloat_pct = stats.get("estimated_bloat_percent", 0)
                    idx_size = idx["index_size"]
                    total_size += idx_size
                    if bloat_pct >= min_bloat_percent:
                        bloated_size = int(idx_size * bloat_pct / 100)
                        total_bloated_size += bloated_size
                        results.append({
                            "index_name": idx["index_name"],
                            "table_name": idx["table_name"],
                            "index_size": idx_size,
                            "index_size_pretty": _format_bytes(idx_size),
                            "avg_leaf_density": stats.get("avg_leaf_density"),
                            "leaf_fragmentation": stats.get("leaf_fragmentation"),
                            "estimated_bloat_percent": bloat_pct,
                            "bloat_severity": stats.get("bloat_severity"),
                            "estimated_wasted_space": _format_bytes(bloated_size),
                        })
            except Exception:
                pass
        results.sort(key=lambda x: x.get("estimated_bloat_percent", 0), reverse=True)
        return {
            "schema": schema_name,
            "indexes_analyzed": len(indexes),
            "indexes_with_bloat": len(results),
            "min_bloat_threshold": min_bloat_percent,
            "summary": {
                "total_index_size": _format_bytes(total_size),
                "estimated_bloated_space": _format_bytes(total_bloated_size),
            },
            "indexes": results,
            "recommendations": self._schema_index_recommendations(results),
        }

    def _index_bloat_severity(self, avg_leaf_density: float, free_percent: float = 0) -> dict[str, Any]:
        severity_result: dict[str, Any] = {
            "overall_severity": "low",
            "density_status": "normal",
            "issues": [],
        }
        score = 0
        estimated_bloat = max(0, 90 - avg_leaf_density)
        if avg_leaf_density < 50:
            severity_result["density_status"] = "critical"
            severity_result["issues"].append(
                f"Leaf density ({avg_leaf_density:.1f}%) is critically low (<50%). "
                "Index is heavily fragmented. REINDEX required."
            )
            score += 3
        elif avg_leaf_density < 70:
            severity_result["density_status"] = "warning"
            severity_result["issues"].append(
                f"Leaf density ({avg_leaf_density:.1f}%) indicates fragmentation (<70%). "
                "Consider REINDEX to improve performance."
            )
            score += 2
        if free_percent > 30:
            severity_result["issues"].append(
                f"Free space ({free_percent:.1f}%) is very high (>30%). "
                "Many empty index pages. REINDEX recommended."
            )
            score += 2
        elif free_percent > 20:
            severity_result["issues"].append(
                f"Free space ({free_percent:.1f}%) is elevated (>20%). "
                "Index may benefit from REINDEX."
            )
            score += 1
        if score >= 4 or estimated_bloat >= 40:
            severity_result["overall_severity"] = "critical"
        elif score >= 3 or estimated_bloat >= 30:
            severity_result["overall_severity"] = "high"
        elif score >= 2 or estimated_bloat >= 20:
            severity_result["overall_severity"] = "moderate"
        severity_result["estimated_bloat_percent"] = round(estimated_bloat, 2)
        return severity_result

    def _index_recommendations(self, stats: dict, index_type: str, info: dict) -> list[str]:
        recommendations: list[str] = []
        if index_type == "btree":
            avg_density = stats.get("avg_leaf_density", 90)
            free_percent = stats.get("free_percent", 0)
            index_name = info.get("index_name", "")
            schema = info.get("schema", "public") if "schema" in info else "public"
            full_name = f"{schema}.{index_name}" if schema else index_name
            if avg_density < 50:
                recommendations.append(
                    f"CRITICAL: Leaf density ({avg_density:.1f}%) is very low (<50%). "
                    f"Index is heavily fragmented. Run:\n"
                    f"  REINDEX INDEX CONCURRENTLY {full_name};"
                )
            elif avg_density < 70:
                recommendations.append(
                    f"WARNING: Leaf density ({avg_density:.1f}%) indicates fragmentation (<70%). "
                    f"Consider:\n  REINDEX INDEX CONCURRENTLY {full_name};"
                )
            if free_percent > 30:
                recommendations.append(
                    f"CRITICAL: Free space ({free_percent:.1f}%) is very high (>30%). "
                    "Many empty index pages. REINDEX strongly recommended."
                )
            elif free_percent > 20:
                recommendations.append(
                    f"WARNING: Free space ({free_percent:.1f}%) is elevated (>20%). "
                    "Index may benefit from REINDEX."
                )
            frag = stats.get("leaf_fragmentation", 0)
            if frag and frag > 30:
                recommendations.append(
                    f"Index has {frag:.1f}% leaf fragmentation. "
                    "This can slow sequential index scans. Consider REINDEX."
                )
            deleted_pages = stats.get("deleted_pages", 0)
            if deleted_pages and deleted_pages > 10:
                recommendations.append(
                    f"Index has {deleted_pages} deleted pages. "
                    "These will be reclaimed by future index operations or REINDEX."
                )
            if not recommendations and avg_density >= 70 and free_percent <= 20:
                recommendations.append(
                    f"Index {full_name} is healthy. Leaf density ({avg_density:.1f}%) is good."
                )
        elif index_type == "gin":
            pending = stats.get("pending_tuples", 0)
            if pending and pending > 1000:
                recommendations.append(
                    f"GIN index has {pending} pending tuples. "
                    "Run VACUUM to merge pending entries into main index."
                )
            elif pending and pending > 100:
                recommendations.append(
                    f"GIN index has {pending} pending tuples. "
                    "Consider running VACUUM if this continues to grow."
                )
        elif index_type == "hash":
            dead_items = stats.get("dead_items", 0)
            if dead_items and dead_items > 100:
                recommendations.append(
                    f"Hash index has {dead_items} dead items. "
                    "Run VACUUM to clean up dead entries."
                )
        return recommendations

    def _schema_index_recommendations(self, indexes: list[dict]) -> list[str]:
        recommendations: list[str] = []
        critical = [i for i in indexes if i.get("bloat_severity") == "critical"]
        high = [i for i in indexes if i.get("bloat_severity") == "high"]
        if critical:
            idx_list = ", ".join(i["index_name"] for i in critical[:5])
            recommendations.append(
                f"CRITICAL: {len(critical)} indexes have critical bloat. "
                f"Priority indexes: {idx_list}"
            )
            recommendations.append(
                "Run REINDEX INDEX CONCURRENTLY for these indexes to reclaim space."
            )
        if high:
            idx_list = ", ".join(i["index_name"] for i in high[:5])
            recommendations.append(
                f"HIGH: {len(high)} indexes have high bloat levels. Indexes: {idx_list}"
            )
        low_density_indexes = [i for i in indexes if i.get("avg_leaf_density", 100) < 70]
        if low_density_indexes:
            idx_list = ", ".join(i["index_name"] for i in low_density_indexes[:5])
            recommendations.append(
                f"{len(low_density_indexes)} indexes have leaf density <70% (fragmented). "
                f"Indexes: {idx_list}. Consider REINDEX."
            )
        return recommendations

# ----------------------------------------------------------------------
# Shared helpers (module-private)
# ----------------------------------------------------------------------

def _format_bytes(size: int | None) -> str:
    if size is None:
        return "0 B"
    val: float = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(val) < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"

