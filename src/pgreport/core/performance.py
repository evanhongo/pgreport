"""Performance, activity, query, table and index-design analyzers.

Houses single-shot performance probes (cache hit, rollback rate, table
hotspots, bgwriter / checkpointer / WAL / SLRU / I/O statistics, progress
views) together with the richer EXPLAIN, table-stats, disk-I/O and index
recommendation tooling.
"""

from __future__ import annotations

import json
from typing import Any

from ..utils.constants import (
    SYSTEM_SCHEMAS_SQL,
    gb_to_bytes,
    sql_hit_ratio,
    sql_pct_ratio,
)
from ..utils.formatting import extract_explain_plan
from ..utils.sql_driver import SqlDriver, check_extension_installed

# ----------------------------------------------------------------------
# Constants reused from the previous QueryAnalyzer
# ----------------------------------------------------------------------
SEQ_SCAN_ROW_WARNING = 10_000
ROW_ESTIMATE_MISMATCH_RATIO = 10
HASH_BATCH_SPILL_THRESHOLD = 1
NESTED_LOOP_ITERATION_WARNING = 1_000

# ----------------------------------------------------------------------
# ----------------------------------------------------------------------

SQL_CACHE_HIT_RATE = """
    SELECT
        datname, blks_read, blks_hit,
        CASE WHEN (blks_hit + blks_read) = 0 THEN 0
             ELSE ROUND((blks_hit::numeric * 100) / (blks_hit + blks_read), 2)
        END AS hit_rate_percentage
    FROM pg_stat_database
    WHERE (blks_hit + blks_read) > 0 AND datname = current_database()
"""

SQL_ROLLBACK_RATE = """
    SELECT
        datname, xact_commit, xact_rollback,
        CASE WHEN (xact_commit + xact_rollback) = 0 THEN 0
             ELSE TRUNC((xact_rollback::numeric /
                    (xact_commit + xact_rollback)) * 100, 2)::numeric
        END AS rollback_percentage
    FROM pg_stat_database
"""

SQL_TABLE_HOTSPOTS = """
    SELECT
        schemaname, relname,
        (n_tup_ins + n_tup_upd + n_tup_del) as total_dml,
        (seq_scan + idx_scan) as total_scans,
        n_dead_tup
    FROM pg_stat_user_tables
    ORDER BY (n_tup_ins + n_tup_upd + n_tup_del) DESC,
        (seq_scan + idx_scan) DESC
    LIMIT %s
"""

SQL_BGWRITER_STATS = """
    SELECT buffers_clean, maxwritten_clean, buffers_alloc
    FROM pg_stat_bgwriter
"""

SQL_TEMP_FILE_USAGE = """
    SELECT
        datname, temp_files, temp_bytes,
        pg_size_pretty(temp_bytes) AS temp_bytes_pretty,
        (temp_bytes / (1024.0 * 1024 * 1024))::numeric(10,2) AS temp_bytes_gb,
        temp_files::float / NULLIF((
            SELECT SUM(temp_files) FROM pg_stat_database WHERE temp_files > 0
        ), 0) AS temp_files_ratio
    FROM pg_stat_database
    WHERE temp_files > 0
      AND temp_bytes >= (%s * 1024 * 1024 * 1024)::bigint
    ORDER BY temp_bytes DESC
    LIMIT %s
"""

SQL_IO_STATISTICS = """
    SELECT
        datname, temp_files, temp_bytes,
        pg_size_pretty(temp_bytes) as temp_bytes_pretty,
        blks_read, blks_hit, blks_read + blks_hit as total_blks,
        blk_read_time, blk_write_time
    FROM pg_stat_database
    WHERE datname = current_database()
"""

SQL_ANALYZE_PROGRESS = """
    SELECT
        p.pid, d.datname, c.relname, p.phase,
        p.sample_blks_total, p.sample_blks_scanned,
        ROUND((p.sample_blks_scanned::numeric /
            NULLIF(p.sample_blks_total, 0) * 100), 2) AS scan_progress_pct,
        p.child_tables_total, p.child_tables_done, p.delay_time
    FROM pg_stat_progress_analyze p
    JOIN pg_database d ON p.datid = d.oid
    LEFT JOIN pg_class c ON p.relid = c.oid
"""

SQL_CREATE_INDEX_PROGRESS = """
    SELECT
        p.pid, d.datname, c.relname AS table_name,
        i.relname AS index_name, p.command, p.phase,
        p.blocks_total, p.blocks_done, p.tuples_total, p.tuples_done,
        p.partitions_total, p.partitions_done
    FROM pg_stat_progress_create_index p
    JOIN pg_database d ON p.datid = d.oid
    LEFT JOIN pg_class c ON p.relid = c.oid
    LEFT JOIN pg_class i ON p.index_relid = i.oid
"""

SQL_CLUSTER_PROGRESS = """
    SELECT
        p.pid, d.datname, c.relname, p.command, p.phase,
        p.heap_blks_total, p.heap_tuples_scanned, p.heap_tuples_written,
        p.cluster_index_relid
    FROM pg_stat_progress_cluster p
    JOIN pg_database d ON p.datid = d.oid
    LEFT JOIN pg_class c ON p.relid = c.oid
"""

SQL_WAL_STATISTICS = """
    SELECT wal_records, wal_fpi, wal_bytes,
        pg_size_pretty(wal_bytes) AS wal_bytes_pretty, wal_buffers_full
    FROM pg_stat_wal
"""

SQL_SLRU_STATS = """
    SELECT name, blks_zeroed, blks_hit, blks_read, blks_written,
        blks_exists, flushes, truncates
    FROM pg_stat_slru
    WHERE blks_read > 0 OR blks_written > 0 OR flushes > 0
    ORDER BY blks_read DESC
    LIMIT %s
"""

SQL_IO_STATISTICS_V2 = """
    SELECT backend_type, object, context, reads, read_bytes,
        pg_size_pretty(read_bytes) AS read_bytes_pretty,
        writes, write_bytes,
        pg_size_pretty(write_bytes) AS write_bytes_pretty,
        extends, extend_bytes,
        pg_size_pretty(extend_bytes) AS extend_bytes_pretty,
        hits, evictions, reuses, fsyncs, fsync_time
    FROM pg_stat_io
    WHERE backend_type IS NOT NULL
    ORDER BY (reads + writes) DESC
"""

SQL_USER_FUNCTION_STATS = """
    SELECT funcid, schemaname, funcname, calls, total_time, self_time,
        ROUND((total_time / NULLIF(calls, 0))::numeric, 2) AS avg_time_ms
    FROM pg_stat_user_functions
    WHERE calls > 0
    ORDER BY total_time DESC
    LIMIT %s
"""

SQL_DATABASE_CONFLICT_STATS = """
    SELECT d.datname, c.confl_tablespace, c.confl_lock, c.confl_snapshot,
        c.confl_bufferpin, c.confl_deadlock, c.confl_active_logicalslot
    FROM pg_stat_database_conflicts c
    JOIN pg_database d ON c.datid = d.oid
    WHERE c.confl_tablespace + c.confl_lock + c.confl_snapshot +
        c.confl_bufferpin + c.confl_deadlock > 0
    ORDER BY (c.confl_tablespace + c.confl_lock + c.confl_snapshot +
        c.confl_bufferpin + c.confl_deadlock) DESC
"""

SQL_CHECKPOINTER_STATS = """
    SELECT num_timed, num_requested, num_done,
        ROUND(write_time::numeric, 2) AS write_time_ms,
        ROUND(sync_time::numeric, 2) AS sync_time_ms,
        buffers_written, slru_written,
        CASE
            WHEN num_done > 0 THEN ROUND((write_time / num_done)::numeric, 2)
            ELSE 0
        END AS avg_write_time_per_checkpoint_ms,
        CASE
            WHEN num_done > 0 THEN ROUND((sync_time / num_done)::numeric, 2)
            ELSE 0
        END AS avg_sync_time_per_checkpoint_ms,
        CASE
            WHEN num_timed > 0 AND num_requested > num_timed * 2 THEN 'WARNING'
            WHEN write_time > 10000 OR sync_time > 10000 THEN 'WARNING'
            ELSE 'OK'
        END AS checkpointer_status
    FROM pg_stat_checkpointer
"""

class PerformanceAnalyzer:
    """Performance, activity, and query-design observations."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver
        self._advisor = None
        self._hypopg = None

    @property
    def advisor(self):
        if self._advisor is None:
            from ..utils.index_advisor import IndexAdvisor
            self._advisor = IndexAdvisor(self.driver)
            self._hypopg = self._advisor.hypopg
        return self._advisor

    @property
    def hypopg(self):
        if self._hypopg is None:
            _ = self.advisor  # triggers init
        return self._hypopg

    async def cache_hit_rate(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_CACHE_HIT_RATE)
        return rows or []

    async def rollback_rate(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_ROLLBACK_RATE)
        return rows or []

    async def table_hotspots(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_TABLE_HOTSPOTS, [limit])
        return rows or []

    async def bgwriter_stats(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_BGWRITER_STATS)
        return rows or []

    async def temp_file_usage(
        self, limit: int = 10, threshold_gb: float = 0.0
    ) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(
            SQL_TEMP_FILE_USAGE, [threshold_gb, limit]
        )
        return rows or []

    async def io_statistics(self) -> dict[str, Any]:
        """Database I/O summary plus PG16+ ``pg_stat_io`` breakdown.

        ``database`` holds the per-database summary from ``pg_stat_database``
        (always available). ``by_context`` holds the detailed per
        backend_type / object / context breakdown from ``pg_stat_io``
        (PG16+); on older servers it is returned as
        ``{"status": "skipped", "reason": "..."}``.
        """
        db_rows = await self.driver.execute_query(SQL_IO_STATISTICS) or []
        try:
            io_rows = await self.driver.execute_query(SQL_IO_STATISTICS_V2)
            by_context: list[dict[str, Any]] | dict[str, Any] = io_rows or []
        except Exception as e:
            by_context = {
                "status": "skipped",
                "reason": f"pg_stat_io requires PG16+: {e}",
            }
        return {"database": db_rows, "by_context": by_context}

    async def pg_progress(
        self, operation: str = "all", include_toast: bool = False
    ) -> dict[str, Any]:
        """Active operations from ``pg_stat_progress_*`` views.

        ``operation`` selects one of ``analyze``, ``create-index``, ``cluster``,
        ``vacuum``, or ``all`` (default). ``include_toast`` only affects the
        vacuum branch — the other progress views never join through a table
        name that distinguishes TOAST from main heaps.
        """
        result: dict[str, list[dict[str, Any]]] = {}

        if operation in ("all", "analyze"):
            rows = await self.driver.execute_query(SQL_ANALYZE_PROGRESS) or []
            result["analyze"] = rows

        if operation in ("all", "create-index"):
            rows = await self.driver.execute_query(SQL_CREATE_INDEX_PROGRESS) or []
            result["create_index"] = rows

        if operation in ("all", "cluster"):
            rows = await self.driver.execute_query(SQL_CLUSTER_PROGRESS) or []
            result["cluster"] = rows

        if operation in ("all", "vacuum"):
            toast_filter = "" if include_toast else "AND c.relname NOT LIKE 'pg_toast%%'"
            vacuum_query = f"""
                SELECT
                    p.pid, p.datname as database,
                    n.nspname as schema_name, c.relname as table_name,
                    p.phase, p.heap_blks_total, p.heap_blks_scanned, p.heap_blks_vacuumed,
                    p.index_vacuum_count, p.max_dead_tuples, p.num_dead_tuples,
                    CASE WHEN p.heap_blks_total > 0
                        THEN ROUND(100.0 * p.heap_blks_scanned / p.heap_blks_total, 2)
                        ELSE 0 END as scan_progress_pct,
                    CASE WHEN p.heap_blks_total > 0
                        THEN ROUND(100.0 * p.heap_blks_vacuumed / p.heap_blks_total, 2)
                        ELSE 0 END as vacuum_progress_pct,
                    a.query, a.state,
                    EXTRACT(epoch FROM now() - a.xact_start)::integer as duration_seconds,
                    a.wait_event_type, a.wait_event
                FROM pg_stat_progress_vacuum p
                JOIN pg_class c ON c.oid = p.relid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_activity a ON a.pid = p.pid
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  {toast_filter}
                ORDER BY p.heap_blks_total DESC
            """
            result["vacuum"] = await self.driver.execute_query(vacuum_query) or []

        summary = {f"{key}_count": len(rows) for key, rows in result.items()}
        summary["total_count"] = sum(summary.values())

        output: dict[str, Any] = dict(result)
        output["summary"] = summary
        if summary["total_count"] == 0:
            output["message"] = "No operations currently in progress"
        return output

    async def wal_statistics(self) -> list[dict[str, Any]] | dict[str, Any]:
        try:
            rows = await self.driver.execute_query(SQL_WAL_STATISTICS)
            return rows or []
        except Exception as e:
            return {"status": "skipped", "reason": f"pg_stat_wal unavailable: {e}"}

    async def slru_stats(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_SLRU_STATS, [limit])
        return rows or []

    async def user_function_stats(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_USER_FUNCTION_STATS, [limit])
        return rows or []

    async def database_conflict_stats(self) -> list[dict[str, Any]]:
        rows = await self.driver.execute_query(SQL_DATABASE_CONFLICT_STATS)
        return rows or []

    async def checkpointer_stats(self) -> list[dict[str, Any]] | dict[str, Any]:
        try:
            rows = await self.driver.execute_query(SQL_CHECKPOINTER_STATS)
            return rows or []
        except Exception as e:
            return {"status": "skipped", "reason": f"pg_stat_checkpointer requires PG17+: {e}"}

    async def top_sql_by_time(
        self,
        limit: int = 10,
        min_calls: int = 1,
        min_mean_time_ms: float = 0.0,
        order_by: str = "mean_time",
    ) -> dict[str, Any]:
        order_map = {
            "mean_time": "mean_exec_time",
            "calls": "calls",
            "rows": "rows",
        }
        if order_by not in order_map:
            order_by = "mean_time"
        order_column = order_map[order_by]

        available = await check_extension_installed(self.driver, "pg_stat_statements")
        if not available:
            return {
                "error": (
                    "pg_stat_statements extension is not installed.\n"
                    "Install it with: CREATE EXTENSION pg_stat_statements;\n"
                    "Note: You may need to add it to shared_preload_libraries in postgresql.conf"
                )
            }

        query = f"""
            SELECT
                queryid,
                LEFT(query, 500) as query_text,
                calls,
                ROUND(mean_exec_time::numeric, 2) as mean_time_ms,
                ROUND(min_exec_time::numeric, 2) as min_time_ms,
                ROUND(max_exec_time::numeric, 2) as max_time_ms,
                ROUND(stddev_exec_time::numeric, 2) as stddev_time_ms,
                rows,
                shared_blks_hit,
                shared_blks_read,
                {sql_hit_ratio("shared_blks_hit", "shared_blks_read", "cache_hit_ratio")},
                temp_blks_read,
                temp_blks_written
            FROM pg_stat_statements
            WHERE calls >= %s
              AND mean_exec_time >= %s
              AND query NOT LIKE '%%pg_stat_statements%%'
              AND query NOT LIKE '%%pg_catalog%%'
              AND query NOT LIKE '%%information_schema%%'
              AND query NOT LIKE '%%pg_toast%%'
            ORDER BY {order_column} DESC
            LIMIT %s
        """

        results = await self.driver.execute_query(
            query, [min_calls, min_mean_time_ms, limit]
        )

        if not results:
            return {
                "message": (
                    "No slow queries found matching the criteria.\n"
                    "This could mean:\n"
                    "- pg_stat_statements has been recently reset\n"
                    "- No queries exceed the minimum thresholds\n"
                    "- The database has low query activity"
                )
            }

        return {
            "summary": {
                "total_queries_returned": len(results),
                "filters_applied": {
                    "min_calls": min_calls,
                    "min_mean_time_ms": min_mean_time_ms,
                    "order_by": order_by,
                },
            },
            "slow_queries": results,
        }

    async def explain(
        self,
        query: str,
        hypothetical_indexes: list[dict[str, Any]] | None = None,
        analyze: bool = True,
        buffers: bool = True,
        verbose: bool = False,
        settings: bool = False,
        fmt: str = "json",
    ) -> dict[str, Any]:
        """EXPLAIN a query, optionally against hypothetical indexes (HypoPG).

        When ``hypothetical_indexes`` is given, the planner is asked to consider
        them and the original/with-indexes plans + cost delta are returned. In
        that mode:

        - ``fmt`` must be ``"json"`` — cost comparison requires JSON plan
          parsing. Other formats raise ``ValueError``.
        - ``analyze`` is auto-disabled. EXPLAIN ANALYZE actually executes the
          query; HypoPG only intercepts planning, so a real run would bypass
          the hypothetical indexes entirely and report misleading actuals. A
          ``note`` field is added to the output when this happens.
        """
        valid_formats = {"text", "json", "yaml", "xml"}
        output_format = fmt.lower() if fmt.lower() in valid_formats else "json"

        note: str | None = None
        if hypothetical_indexes:
            if output_format != "json":
                raise ValueError(
                    "--index requires --format json (hypopg cost comparison "
                    "needs to parse the JSON plan)"
                )
            if analyze:
                analyze = False
                note = (
                    "--analyze was disabled: EXPLAIN ANALYZE actually executes "
                    "the query and bypasses HypoPG's planner hook, so it would "
                    "report timings without the hypothetical indexes."
                )

        options: list[str] = []
        if analyze:
            options.append("ANALYZE")
        if buffers:
            options.append("BUFFERS")
        if verbose:
            options.append("VERBOSE")
        if settings:
            options.append("SETTINGS")
        options.append(f"FORMAT {output_format.upper()}")
        explain_sql = f"EXPLAIN ({', '.join(options)}) {query}"

        results = await self.driver.execute_query(explain_sql)
        if not results:
            return {"error": "No execution plan returned"}

        output: dict[str, Any] = {
            "query": query,
            "explain_options": {
                "analyze": analyze,
                "buffers": buffers,
                "verbose": verbose,
                "settings": settings,
                "format": output_format,
            },
        }
        if note is not None:
            output["note"] = note

        if output_format != "json":
            output["execution_plan"] = "\n".join(
                str(row.get("QUERY PLAN", row)) for row in results
            )
            return output

        plan_data = results[0].get("QUERY PLAN", results)
        if isinstance(plan_data, str):
            plan_data = json.loads(plan_data)
        output["execution_plan"] = plan_data
        output["analysis"] = self._analyze_plan(plan_data, analyze)

        if not hypothetical_indexes:
            return output

        # HypoPG flow: compute plan + cost with hypothetical indexes
        original_plan_unwrapped = extract_explain_plan(results)
        output["original_cost"] = self._extract_cost(original_plan_unwrapped)

        hypo = self.hypopg
        hypopg_status = await hypo.check_status()
        if not hypopg_status.is_installed:
            output["error"] = (
                "HypoPG extension is not available. "
                "Install it with: CREATE EXTENSION hypopg;"
            )
            return output

        created_indexes: list[dict[str, Any]] = []
        try:
            for idx_spec in hypothetical_indexes:
                hypo_index = await hypo.create_index(
                    table=idx_spec["table"],
                    columns=idx_spec["columns"],
                    using=idx_spec.get("index_type", "btree"),
                )
                created_indexes.append({
                    **idx_spec,
                    "index_name": hypo_index.index_name,
                    "index_oid": hypo_index.indexrelid,
                    "estimated_size": hypo_index.estimated_size,
                })
            hypo_result = await self.driver.execute_query(explain_sql)
            hypo_plan_data = hypo_result[0].get("QUERY PLAN", hypo_result) if hypo_result else None
            if isinstance(hypo_plan_data, str):
                hypo_plan_data = json.loads(hypo_plan_data)
            hypo_plan_unwrapped = extract_explain_plan(hypo_result)
            output["hypothetical_indexes"] = created_indexes
            output["plan_with_indexes"] = hypo_plan_data
            output["cost_with_indexes"] = self._extract_cost(hypo_plan_unwrapped)
            original_cost = output["original_cost"]
            new_cost = output["cost_with_indexes"]
            if original_cost > 0:
                improvement = ((original_cost - new_cost) / original_cost) * 100
                output["estimated_improvement_percent"] = round(improvement, 2)
            output["indexes_used"] = self._find_used_indexes(
                hypo_plan_unwrapped, created_indexes
            )
            output["analysis_with_indexes"] = self._analyze_plan(
                hypo_plan_data, analyze
            )
        finally:
            await hypo.reset()
        return output

    def _analyze_plan(self, plan_data: Any, was_analyzed: bool) -> dict[str, Any]:
        analysis: dict[str, Any] = {
            "warnings": [], "recommendations": [], "statistics": {},
        }
        if not plan_data:
            return analysis
        if isinstance(plan_data, list) and len(plan_data) > 0:
            plan = plan_data[0].get("Plan", plan_data[0])
        else:
            plan = plan_data.get("Plan", plan_data)
        if was_analyzed:
            top = plan_data[0] if isinstance(plan_data, list) else plan_data
            if "Execution Time" in top:
                analysis["statistics"]["execution_time_ms"] = top.get("Execution Time", 0)
            if "Planning Time" in top:
                analysis["statistics"]["planning_time_ms"] = top.get("Planning Time", 0)
        self._analyze_node(plan, analysis)
        return analysis

    def _analyze_node(self, node: dict[str, Any], analysis: dict[str, Any], depth: int = 0) -> None:
        if not isinstance(node, dict):
            return
        node_type = node.get("Node Type", "Unknown")
        if node_type == "Seq Scan":
            rows = node.get("Actual Rows", node.get("Plan Rows", 0))
            if rows > SEQ_SCAN_ROW_WARNING:
                table = node.get("Relation Name", "unknown")
                analysis["warnings"].append(
                    f"Sequential scan on '{table}' returned {rows} rows - consider adding an index"
                )
                filter_cond = node.get("Filter")
                if filter_cond:
                    analysis["recommendations"].append(
                        f"Consider creating an index for filter condition: {filter_cond}"
                    )
        actual_rows = node.get("Actual Rows")
        plan_rows = node.get("Plan Rows")
        if actual_rows is not None and plan_rows is not None and plan_rows > 0:
            ratio = actual_rows / plan_rows
            if ratio > ROW_ESTIMATE_MISMATCH_RATIO or ratio < 1 / ROW_ESTIMATE_MISMATCH_RATIO:
                analysis["warnings"].append(
                    f"{node_type}: Row estimate mismatch - planned {plan_rows}, actual {actual_rows} "
                    f"(ratio: {ratio:.2f}). Consider running ANALYZE on the table."
                )
        if "Hash" in node_type:
            batches = node.get("Hash Batches", 1)
            if batches > HASH_BATCH_SPILL_THRESHOLD:
                analysis["warnings"].append(
                    f"{node_type} spilled to disk ({batches} batches). "
                    "Consider increasing work_mem or optimizing the query."
                )
        if node_type == "Sort":
            sort_method = node.get("Sort Method", "")
            if "external" in sort_method.lower():
                analysis["warnings"].append(
                    f"Sort operation spilled to disk ({sort_method}). "
                    "Consider increasing work_mem."
                )
        if node_type == "Nested Loop":
            actual_loops = node.get("Actual Loops", 1)
            if actual_loops > NESTED_LOOP_ITERATION_WARNING:
                analysis["warnings"].append(
                    f"Nested Loop executed {actual_loops} times - consider using a different join strategy"
                )
        for child in node.get("Plans", []):
            self._analyze_node(child, analysis, depth + 1)

    async def table_stats(
        self,
        schema_name: str = "public",
        table_name: str | None = None,
        include_indexes: bool = True,
        order_by: str = "size",
    ) -> dict[str, Any]:
        order_map = {
            "size": "total_size DESC",
            "rows": "n_live_tup DESC",
            "dead_tuples": "n_dead_tup DESC",
            "seq_scans": "seq_scan DESC",
            "last_vacuum": "last_vacuum DESC NULLS LAST",
        }
        if order_by not in order_map:
            order_by = "size"
        order_clause = order_map[order_by]

        table_filter = ""
        params: list[Any] = [schema_name]
        if table_name:
            table_filter = "AND c.relname ILIKE %s"
            params.append(table_name)

        query = f"""
            SELECT
                c.relname as table_name,
                n.nspname as schema_name,
                pg_size_pretty(pg_table_size(c.oid)) as table_size,
                pg_size_pretty(pg_indexes_size(c.oid)) as indexes_size,
                pg_size_pretty(pg_total_relation_size(c.oid)) as total_size,
                pg_total_relation_size(c.oid) as total_size_bytes,
                s.n_live_tup,
                s.n_dead_tup,
                {sql_pct_ratio("s.n_dead_tup", "s.n_live_tup", "dead_tuple_ratio")},
                s.seq_scan,
                s.seq_tup_read,
                s.idx_scan,
                s.idx_tup_fetch,
                {sql_pct_ratio("COALESCE(s.idx_scan, 0)", "s.seq_scan + COALESCE(s.idx_scan, 0)", "index_scan_ratio")},
                s.last_vacuum,
                s.last_autovacuum,
                s.last_analyze,
                s.last_autoanalyze,
                s.vacuum_count,
                s.autovacuum_count,
                s.analyze_count,
                s.autoanalyze_count
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind = 'r'
              AND n.nspname = %s
              AND n.nspname NOT IN {SYSTEM_SCHEMAS_SQL}
              {table_filter}
            ORDER BY {order_clause}
        """

        results = await self.driver.execute_query(query, params)
        if not results:
            return {"error": f"No tables found in schema '{schema_name}'"}

        output: dict[str, Any] = {
            "schema": schema_name,
            "table_count": len(results),
            "tables": results,
        }

        if include_indexes and table_name:
            index_query = """
                SELECT
                    i.indexrelname as index_name,
                    i.idx_scan as scans,
                    i.idx_tup_read as tuples_read,
                    i.idx_tup_fetch as tuples_fetched,
                    pg_size_pretty(pg_relation_size(i.indexrelid)) as size,
                    pg_relation_size(i.indexrelid) as size_bytes,
                    pg_get_indexdef(i.indexrelid) as definition
                FROM pg_stat_user_indexes i
                JOIN pg_class c ON c.oid = i.relid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                ORDER BY i.idx_scan DESC
            """
            index_results = await self.driver.execute_query(
                index_query, [schema_name, table_name]
            )
            output["indexes"] = index_results

        output["analysis"] = self._analyze_table_stats(results)
        return output

    def _analyze_table_stats(self, tables: list[dict]) -> dict[str, Any]:
        analysis: dict[str, Any] = {
            "needs_vacuum": [], "needs_analyze": [],
            "low_index_usage": [], "recommendations": [],
        }
        for table in tables:
            table_name = table.get("table_name", "unknown")
            dead_ratio = table.get("dead_tuple_ratio", 0) or 0
            if dead_ratio > 10:
                analysis["needs_vacuum"].append({
                    "table": table_name,
                    "dead_tuple_ratio": dead_ratio,
                    "dead_tuples": table.get("n_dead_tup", 0),
                })
            last_analyze = table.get("last_analyze") or table.get("last_autoanalyze")
            n_live = table.get("n_live_tup", 0) or 0
            if n_live > 1000 and not last_analyze:
                analysis["needs_analyze"].append(table_name)
            idx_ratio = table.get("index_scan_ratio", 0) or 0
            seq_scans = table.get("seq_scan", 0) or 0
            if seq_scans > 100 and idx_ratio < 50 and n_live > 10000:
                analysis["low_index_usage"].append({
                    "table": table_name,
                    "index_scan_ratio": idx_ratio,
                    "seq_scans": seq_scans,
                    "rows": n_live,
                })
        if analysis["needs_vacuum"]:
            tables_list = ", ".join(t["table"] for t in analysis["needs_vacuum"][:5])
            analysis["recommendations"].append(
                f"Run VACUUM on tables with high dead tuple ratios: {tables_list}"
            )
        if analysis["needs_analyze"]:
            tables_list = ", ".join(analysis["needs_analyze"][:5])
            analysis["recommendations"].append(
                f"Run ANALYZE on tables that haven't been analyzed: {tables_list}"
            )
        if analysis["low_index_usage"]:
            for item in analysis["low_index_usage"][:3]:
                analysis["recommendations"].append(
                    f"Table '{item['table']}' has low index usage ({item['index_scan_ratio']}% index scans). "
                    "Consider adding indexes for frequently filtered columns."
                )
        return analysis

    async def disk_io_analysis(
        self,
        schema_name: str = "public",
        include_indexes: bool = True,
        top_n: int = 20,
        analysis_type: str = "all",
        min_size_gb: float = 1.0,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {
            "schema": schema_name,
            "analysis_type": analysis_type,
            "io_patterns": {},
            "analysis": {"issues": [], "recommendations": []},
        }
        if analysis_type in ("all", "buffer_pool"):
            await self._dio_buffer_pool(output)
        if analysis_type in ("all", "tables"):
            await self._dio_tables(output, schema_name, top_n, min_size_gb)
        if analysis_type in ("all", "indexes") and include_indexes:
            await self._dio_indexes(output, schema_name, top_n, min_size_gb)
        self._dio_recommendations(output)
        return output

    async def _dio_buffer_pool(self, output: dict[str, Any]) -> None:
        query = f"""
            SELECT
                sum(heap_blks_read) as heap_blocks_read,
                sum(heap_blks_hit) as heap_blocks_hit,
                sum(idx_blks_read) as index_blocks_read,
                sum(idx_blks_hit) as index_blocks_hit,
                sum(toast_blks_read) as toast_blocks_read,
                sum(toast_blks_hit) as toast_blocks_hit,
                sum(tidx_blks_read) as toast_index_blocks_read,
                sum(tidx_blks_hit) as toast_index_blocks_hit,
                {sql_hit_ratio("sum(heap_blks_hit)", "sum(heap_blks_read)", "heap_hit_ratio")},
                {sql_hit_ratio("sum(idx_blks_hit)", "sum(idx_blks_read)", "index_hit_ratio")}
            FROM pg_statio_user_tables
        """
        result = await self.driver.execute_query(query)
        if result:
            row = result[0]
            heap_hit = row.get("heap_hit_ratio") or 100
            idx_hit = row.get("index_hit_ratio") or 100
            output["io_patterns"]["buffer_pool"] = {
                "heap_blocks_read": row.get("heap_blocks_read") or 0,
                "heap_blocks_hit": row.get("heap_blocks_hit") or 0,
                "heap_hit_ratio": heap_hit,
                "index_blocks_read": row.get("index_blocks_read") or 0,
                "index_blocks_hit": row.get("index_blocks_hit") or 0,
                "index_hit_ratio": idx_hit,
                "toast_blocks_read": row.get("toast_blocks_read") or 0,
                "toast_blocks_hit": row.get("toast_blocks_hit") or 0,
            }
            if heap_hit < 90:
                output["analysis"]["issues"].append(
                    f"Low heap buffer cache hit ratio: {heap_hit}%"
                )
                output["analysis"]["recommendations"].append(
                    "Consider increasing shared_buffers to improve cache hit ratio"
                )
            if idx_hit < 95:
                output["analysis"]["issues"].append(
                    f"Low index buffer cache hit ratio: {idx_hit}%"
                )
                output["analysis"]["recommendations"].append(
                    "Ensure frequently accessed indexes fit in buffer cache"
                )

    async def _dio_tables(
        self, output: dict[str, Any], schema_name: str, top_n: int, min_size_gb: float
    ) -> None:
        min_size_bytes = gb_to_bytes(min_size_gb)
        query = f"""
            SELECT
                s.schemaname, s.relname as table_name,
                s.heap_blks_read, s.heap_blks_hit,
                {sql_hit_ratio("s.heap_blks_hit", "s.heap_blks_read", "heap_hit_ratio")},
                s.idx_blks_read, s.idx_blks_hit,
                {sql_hit_ratio("s.idx_blks_hit", "s.idx_blks_read", "idx_hit_ratio")},
                s.heap_blks_read + COALESCE(s.idx_blks_read, 0) as total_reads,
                s.heap_blks_hit + COALESCE(s.idx_blks_hit, 0) as total_hits,
                pg_total_relation_size(c.oid) as table_size_bytes
            FROM pg_statio_user_tables s
            JOIN pg_class c ON c.oid = s.relid
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
            WHERE s.schemaname = %s
              AND s.schemaname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND pg_total_relation_size(c.oid) >= %s
            ORDER BY (s.heap_blks_read + COALESCE(s.idx_blks_read, 0)) DESC
            LIMIT %s
        """
        result = await self.driver.execute_query(
            query, [schema_name, min_size_bytes, top_n]
        )
        scan_query = f"""
            SELECT
                s.schemaname, s.relname as table_name,
                s.seq_scan, s.seq_tup_read,
                s.idx_scan, s.idx_tup_fetch,
                {sql_pct_ratio("s.seq_scan", "s.seq_scan + COALESCE(s.idx_scan, 0)", "seq_scan_ratio")},
                s.n_live_tup, s.n_dead_tup
            FROM pg_stat_user_tables s
            JOIN pg_class c ON c.oid = s.relid
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
            WHERE s.schemaname = %s
              AND s.schemaname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND pg_total_relation_size(c.oid) >= %s
            ORDER BY s.seq_tup_read DESC
            LIMIT %s
        """
        scan_result = await self.driver.execute_query(
            scan_query, [schema_name, min_size_bytes, top_n]
        )

        tables = []
        scan_data = {r["table_name"]: r for r in scan_result} if scan_result else {}
        if result:
            for row in result:
                table_name = row.get("table_name")
                scan_info = scan_data.get(table_name, {})
                tables.append({
                    "table_name": table_name,
                    "heap_blocks_read": row.get("heap_blks_read") or 0,
                    "heap_blocks_hit": row.get("heap_blks_hit") or 0,
                    "heap_hit_ratio": row.get("heap_hit_ratio") or 100,
                    "index_blocks_read": row.get("idx_blks_read") or 0,
                    "index_blocks_hit": row.get("idx_blks_hit") or 0,
                    "index_hit_ratio": row.get("idx_hit_ratio") or 100,
                    "total_physical_reads": row.get("total_reads") or 0,
                    "seq_scans": scan_info.get("seq_scan") or 0,
                    "seq_tuples_read": scan_info.get("seq_tup_read") or 0,
                    "idx_scans": scan_info.get("idx_scan") or 0,
                    "idx_tuples_fetched": scan_info.get("idx_tup_fetch") or 0,
                    "seq_scan_ratio": scan_info.get("seq_scan_ratio") or 0,
                    "live_tuples": scan_info.get("n_live_tup") or 0,
                })
                heap_hit = row.get("heap_hit_ratio") or 100
                total_reads = row.get("total_reads") or 0
                if total_reads > 1000 and heap_hit < 85:
                    output["analysis"]["issues"].append(
                        f"Table '{table_name}' has high I/O ({total_reads} reads) with low cache hit ({heap_hit}%)"
                    )
                seq_ratio = scan_info.get("seq_scan_ratio") or 0
                live_tuples = scan_info.get("n_live_tup") or 0
                if seq_ratio > 80 and live_tuples > 10000:
                    output["analysis"]["issues"].append(
                        f"Table '{table_name}' has {seq_ratio}% sequential scans with {live_tuples} rows"
                    )
                    output["analysis"]["recommendations"].append(
                        f"Consider adding indexes to table '{table_name}' to reduce sequential scans"
                    )
        output["io_patterns"]["tables"] = {
            "count": len(tables), "top_tables_by_io": tables,
        }

    async def _dio_indexes(
        self, output: dict[str, Any], schema_name: str, top_n: int, min_size_gb: float
    ) -> None:
        min_size_bytes = gb_to_bytes(min_size_gb)
        query = f"""
            SELECT
                s.schemaname, s.relname as table_name, s.indexrelname as index_name,
                s.idx_blks_read, s.idx_blks_hit,
                {sql_hit_ratio("s.idx_blks_hit", "s.idx_blks_read", "hit_ratio")},
                pg_relation_size(s.indexrelid) as index_size_bytes
            FROM pg_statio_user_indexes s
            JOIN pg_class i ON i.oid = s.indexrelid
            JOIN pg_namespace n ON n.oid = i.relnamespace AND n.nspname = s.schemaname
            WHERE s.schemaname = %s
              AND s.schemaname NOT IN {SYSTEM_SCHEMAS_SQL}
              AND pg_relation_size(s.indexrelid) >= %s
            ORDER BY s.idx_blks_read DESC
            LIMIT %s
        """
        result = await self.driver.execute_query(
            query, [schema_name, min_size_bytes, top_n]
        )
        if result:
            indexes = []
            for row in result:
                indexes.append({
                    "index_name": row.get("index_name"),
                    "table_name": row.get("table_name"),
                    "blocks_read": row.get("idx_blks_read") or 0,
                    "blocks_hit": row.get("idx_blks_hit") or 0,
                    "hit_ratio": row.get("hit_ratio") or 100,
                })
                hit_ratio = row.get("hit_ratio") or 100
                blocks_read = row.get("idx_blks_read") or 0
                if blocks_read > 1000 and hit_ratio < 90:
                    output["analysis"]["issues"].append(
                        f"Index '{row.get('index_name')}' has high I/O with low cache hit ({hit_ratio}%)"
                    )
            output["io_patterns"]["indexes"] = {
                "count": len(indexes), "top_indexes_by_io": indexes,
            }

    def _dio_recommendations(self, output: dict[str, Any]) -> None:
        unique_issues = list(dict.fromkeys(output["analysis"]["issues"]))
        unique_recs = list(dict.fromkeys(output["analysis"]["recommendations"]))
        output["analysis"]["issues"] = unique_issues
        output["analysis"]["recommendations"] = unique_recs
        io_patterns = output["io_patterns"]
        summary: dict[str, Any] = {
            "total_issues": len(unique_issues),
            "total_recommendations": len(unique_recs),
        }
        if "buffer_pool" in io_patterns:
            bp = io_patterns["buffer_pool"]
            summary["heap_cache_hit_ratio"] = bp.get("heap_hit_ratio")
            summary["index_cache_hit_ratio"] = bp.get("index_hit_ratio")
        output["summary"] = summary

    async def recommend_indexes(
        self,
        workload_queries: list[str] | None = None,
        max_recommendations: int = 10,
        min_improvement_percent: float = 10.0,
        target_tables: list[str] | None = None,
        include_hypothetical_testing: bool = True,
    ) -> dict[str, Any]:
        advisor = self.advisor
        original_check = None
        if not include_hypothetical_testing:
            original_check = advisor.hypopg.check_status
            from ..utils.hypopg_service import HypoPGStatus
            async def _disabled_status(force_refresh=False):
                return HypoPGStatus(is_installed=False, is_available=False,
                                    message="Hypothetical testing disabled by user")
            advisor.hypopg.check_status = _disabled_status

        try:
            if workload_queries:
                result = await advisor.analyze_queries(
                    workload_queries, max_recommendations=max_recommendations
                )
            else:
                result = await advisor.analyze_workload(
                    limit=50, min_calls=5, max_recommendations=max_recommendations
                )
        finally:
            if original_check is not None:
                advisor.hypopg.check_status = original_check

        if result.error:
            return {"error": result.error, "analyzed_queries": result.analyzed_queries}

        all_recommendations = [
            {
                "table": rec.table,
                "columns": rec.columns,
                "using": rec.using,
                "estimated_size_bytes": rec.estimated_size_bytes,
                "estimated_improvement_percent": rec.estimated_improvement,
                "reason": rec.reason,
                "create_statement": rec.definition,
            }
            for rec in result.recommendations
        ]
        if target_tables:
            target_set = {t.lower() for t in target_tables}
            all_recommendations = [
                r for r in all_recommendations if r.get("table", "").lower() in target_set
            ]
        recommendations = [
            r for r in all_recommendations
            if (r.get("estimated_improvement_percent") or 0) >= min_improvement_percent
        ]
        recommendations.sort(
            key=lambda x: x.get("estimated_improvement_percent") or 0, reverse=True
        )
        recommendations = recommendations[:max_recommendations]
        hypopg_status = await advisor.hypopg.check_status()

        output: dict[str, Any] = {
            "summary": {
                "total_recommendations": len(recommendations),
                "analyzed_queries": result.analyzed_queries,
                "hypopg_available": hypopg_status.is_installed,
                "analysis_source": "provided_queries" if workload_queries else "pg_stat_statements",
            },
            "recommendations": recommendations,
            "create_statements": [
                r.get("create_statement") for r in recommendations if r.get("create_statement")
            ],
        }
        if not recommendations:
            output["message"] = (
                "No index recommendations found. This could mean:\n"
                "- Your database already has optimal indexes\n"
                "- Not enough query data in pg_stat_statements\n"
                "- The improvement threshold is too high"
            )
        return output

    @staticmethod
    def _extract_cost(plan: dict) -> float:
        if not plan:
            return 0.0
        plan_node = plan.get("Plan", plan)
        return plan_node.get("Total Cost", 0.0)

    @staticmethod
    def _find_used_indexes(plan: dict, created_indexes: list[dict]) -> list[dict]:
        used: list[dict] = []
        index_names = {idx["index_name"] for idx in created_indexes if "index_name" in idx}

        def check_node(node: dict) -> None:
            if not isinstance(node, dict):
                return
            node_type = node.get("Node Type", "")
            if "Index" in node_type:
                idx_name = node.get("Index Name", "")
                if any(name in idx_name for name in index_names):
                    used.append({
                        "index_name": idx_name,
                        "scan_type": node_type,
                        "startup_cost": node.get("Startup Cost"),
                        "total_cost": node.get("Total Cost"),
                    })
            for child in node.get("Plans", []):
                check_node(child)

        check_node(plan.get("Plan", plan))
        return used

    async def hypothetical_index(self, action: str, **kwargs: Any) -> dict[str, Any]:
        handlers = {
            "check": self._hypo_check,
            "create": self._hypo_create,
            "list": self._hypo_list,
            "drop": self._hypo_drop,
            "reset": self._hypo_reset,
            "estimate_size": self._hypo_estimate_size,
            "hide": self._hypo_hide,
            "unhide": self._hypo_unhide,
            "list_hidden": self._hypo_list_hidden,
            "explain_with_index": self._hypo_explain,
        }
        handler = handlers.get(action)
        if handler is None:
            return {"error": f"Unknown action: {action}"}
        return await handler(**kwargs)

    async def _hypo_check(self, **_kwargs: Any) -> dict[str, Any]:
        status = await self.hypopg.check_status()
        return {
            "hypopg_available": status.is_installed,
            "hypopg_version": status.version,
            "message": status.message,
        }

    async def _hypo_create(self, **kwargs: Any) -> dict[str, Any]:
        hypo_index = await self.hypopg.create_index(
            table=kwargs["table"],
            columns=kwargs["columns"],
            using=kwargs.get("index_type", "btree"),
            schema=kwargs.get("schema"),
            where=kwargs.get("where"),
            include=kwargs.get("include"),
            unique=kwargs.get("unique", False),
        )
        return {
            "success": True,
            "index_oid": hypo_index.indexrelid,
            "index_name": hypo_index.index_name,
            "table": hypo_index.table_name,
            "schema": hypo_index.schema_name,
            "definition": hypo_index.definition,
            "estimated_size_bytes": hypo_index.estimated_size,
        }

    async def _hypo_list(self, **_kwargs: Any) -> dict[str, Any]:
        indexes = await self.hypopg.list_indexes()
        return {
            "count": len(indexes),
            "hypothetical_indexes": [
                {
                    "index_oid": idx.indexrelid,
                    "index_name": idx.index_name,
                    "schema_name": idx.schema_name,
                    "table_name": idx.table_name,
                    "access_method": idx.am_name,
                    "definition": idx.definition,
                    "estimated_size_bytes": idx.estimated_size,
                }
                for idx in indexes
            ],
        }

    async def _hypo_drop(self, **kwargs: Any) -> dict[str, Any]:
        success = await self.hypopg.drop_index(kwargs["index_id"])
        return {"success": success, "dropped_index_id": kwargs["index_id"]}

    async def _hypo_reset(self, **_kwargs: Any) -> dict[str, Any]:
        success = await self.hypopg.reset()
        return {
            "success": success,
            "message": "All hypothetical indexes have been removed" if success else "Failed to reset hypothetical indexes",
        }

    async def _hypo_estimate_size(self, **kwargs: Any) -> dict[str, Any]:
        hypo_index = await self.hypopg.create_index(
            table=kwargs["table"],
            columns=kwargs["columns"],
            using=kwargs.get("index_type", "btree"),
        )
        size = hypo_index.estimated_size
        await self.hypopg.drop_index(hypo_index.indexrelid)
        return {
            "table": kwargs["table"],
            "columns": kwargs["columns"],
            "index_type": kwargs.get("index_type", "btree"),
            "estimated_size_bytes": size,
        }

    async def _hypo_hide(self, **kwargs: Any) -> dict[str, Any]:
        success = await self.hypopg.hide_index(kwargs["index_id"])
        return {
            "success": success,
            "hidden_index_id": kwargs["index_id"],
            "message": "Index is now hidden from the query planner" if success else "Failed to hide index",
        }

    async def _hypo_unhide(self, **kwargs: Any) -> dict[str, Any]:
        success = await self.hypopg.unhide_index(kwargs["index_id"])
        return {
            "success": success,
            "unhidden_index_id": kwargs["index_id"],
            "message": "Index is now visible to the query planner" if success else "Failed to unhide index",
        }

    async def _hypo_list_hidden(self, **_kwargs: Any) -> dict[str, Any]:
        hidden_indexes = await self.hypopg.list_hidden_indexes()
        return {"count": len(hidden_indexes), "hidden_indexes": hidden_indexes}

    async def _hypo_explain(self, **kwargs: Any) -> dict[str, Any]:
        return await self.hypopg.explain_with_hypothetical_index(
            query=kwargs["query"],
            table=kwargs["table"],
            columns=kwargs["columns"],
            using=kwargs.get("index_type", "btree"),
        )
