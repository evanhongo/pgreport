"""Main CLI entry point for pgreport harness.

Provides Click-based CLI with REPL mode and --json output. The ``source``
group manages database connection aliases.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import click

from .utils.config import (
    get_active_dsn,
    add_source,
    remove_source,
    list_sources,
    set_active_source,
)
from .utils.connection import ConnectionManager
from .utils.formatting import format_json
from .utils.sql_driver import obfuscate_password
from .core.health import HealthAnalyzer
from .core.session import SessionAnalyzer
from .core.performance import PerformanceAnalyzer
from .core.replication import ReplicationAnalyzer
from .core.maintenance import MaintenanceAnalyzer


def _wrap_envelope(data: Any) -> dict[str, Any]:
    """Wrap analyzer output in the ``{"status": ..., "data": ...}`` envelope.

    Mirrors the SKILL.md ``{"skill", "status", "data"}`` shape with the ``skill``
    field dropped. Only ``status="skipped"`` from an analyzer is treated as an
    envelope marker (used when a required PG view is missing); other ``status``
    values (e.g. ``"healthy"`` inside ``health_check`` output) are treated as
    domain data and stay nested under ``data``.
    """
    if isinstance(data, dict):
        if data.get("status") == "skipped":
            if "data" not in data:
                data["data"] = None
            return data
        if "error" in data:
            return {"status": "error", "error": data["error"], "data": None}
    return {"status": "success", "data": data}


def _output(data: Any, as_json: bool):
    """Output data as JSON (for dicts/json_mode) or plain text."""
    if as_json or isinstance(data, (dict, list)):
        click.echo(format_json(_wrap_envelope(data)))
    else:
        click.echo(data)


class CliState:
    """Per-invocation state: the live database connection and output preferences.

    Analyzer instances are created on demand inside each command (rather than
    pre-loaded here) so the state class does not have to track every new
    analyzer category. Each command does ``HealthAnalyzer(state.conn.driver)``
    etc. directly.
    """

    def __init__(self):
        self.conn: ConnectionManager | None = None
        self.json_mode: bool = False
        self.loop: asyncio.AbstractEventLoop | None = None

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is None or self.loop.is_closed():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        return self.loop

    def run(self, coro):
        return self.ensure_loop().run_until_complete(coro)

    def connect(self, database_uri: str | None = None):
        self.conn = ConnectionManager(database_uri)
        self.run(self.conn.connect())

    def disconnect(self):
        if self.conn:
            self.run(self.conn.disconnect())

    def output(self, data: Any):
        _output(data, self.json_mode)


def _exit_no_uri():
    """Print missing-URI error and exit. Extracted to avoid duplicated messages."""
    click.echo("Error: No database URI provided.", err=True)
    click.echo("Provide --database-uri or add a source:", err=True)
    click.echo("  pgreport source add <DATABASE_URI> <alias>", err=True)
    sys.exit(1)


pass_state = click.make_pass_decorator(CliState, ensure=True)


@click.group(invoke_without_command=True)
@click.version_option(package_name="pgreport")
@click.option("--database-uri", help="PostgreSQL database URI. Enters REPL mode if provided without a subcommand")
@click.option("--json", "json_mode", is_flag=True, help="Output all results in JSON format")
@click.pass_context
def cli(ctx, database_uri, json_mode):
    """pgreport CLI - PostgreSQL diagnostic reporting."""
    state = ctx.ensure_object(CliState)
    state.json_mode = json_mode
    ctx.call_on_close(state.disconnect)

    if not database_uri:
        database_uri = get_active_dsn()

    # source group manages config and does not require a DB connection
    if ctx.invoked_subcommand == "source":
        return

    if not database_uri and ctx.invoked_subcommand is not None:
        _exit_no_uri()

    if database_uri:
        try:
            state.connect(database_uri)
        except Exception as e:
            click.echo(f"Error connecting: {e}", err=True)
            sys.exit(1)

    if ctx.invoked_subcommand is None:
        if database_uri:
            repl(ctx)
        else:
            _exit_no_uri()


def _parse_index_spec(spec: str) -> dict[str, Any]:
    """Parse index spec 'table:col1,col2[:type][:unique]' into a dict."""
    parts = spec.split(":")
    if len(parts) < 2:
        raise click.BadParameter(
            f"Invalid index spec '{spec}'. Expected format: table:col1,col2[:type][:unique]"
        )
    table = parts[0]
    columns = parts[1].split(",")
    index_type = "btree"
    unique = False
    for part in parts[2:]:
        if part.lower() == "unique":
            unique = True
        else:
            index_type = part
    return {"table": table, "columns": columns, "index_type": index_type, "unique": unique}


# =====================================================================
# 1. Core Health & Availability
# =====================================================================

@cli.command("wraparound-risk", short_help="Monitor XID + MultiXactId wraparound")
@click.option("--type", "type_", type=click.Choice(["xid", "mxid", "both"]), default="both", show_default=True, help="Which wraparound view to run")
@pass_state
def wraparound_risk(state, type_):
    """Per-database XID and/or MultiXactId age and wraparound risk."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.wraparound_risk(type_)))


@cli.command("blocking-locks", short_help="Detect lock contention")
@pass_state
def blocking_locks(state):
    """Identifies active blocking lock situations where one session holds a lock."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.blocking_locks()))


@cli.command("deadlock-detection", short_help="Check for past deadlocks")
@pass_state
def deadlock_detection(state):
    """Checks for deadlocks that have occurred since the last stats reset."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.deadlock_detection()))


@cli.command("connection-security-status", short_help="Check SSL/GSSAPI encryption status")
@click.option("--limit", type=int, default=50, show_default=True, help="Maximum connections to return")
@pass_state
def connection_security_status(state, limit):
    """Checks SSL/GSSAPI encryption status for active connections."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.connection_security_status(limit)))


@cli.command("health-check", short_help="Comprehensive health audit")
@click.option("--verbose", is_flag=True, help="Include detailed statistics for each health check")
@click.option("--no-recommendations", is_flag=True, help="Skip actionable recommendations in the output")
@pass_state
def health_check(state, verbose, no_recommendations):
    """Comprehensive multi-area database health check with score & recommendations."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.health_check(not no_recommendations, verbose)))


@cli.command("index-health", short_help="Index health overview")
@click.option("--schema", default="public", show_default=True, help="Schema name to inspect")
@click.option("--min-size-mb", type=float, default=0.0, show_default=True, help="Minimum index size (MB) to flag as unused")
@click.option("--no-duplicates", is_flag=True, help="Skip duplicate / overlapping index detection")
@pass_state
def index_health(state, schema, min_size_mb, no_duplicates):
    """Health check for indexes: invalid + unused + duplicate/overlapping with score and DROP/REINDEX recommendations."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.index_health(schema, min_size_mb, not no_duplicates)))


@cli.command("wait-events", short_help="Current wait event analysis")
@click.option("--all-states", is_flag=True, help="Include non-active (idle) sessions in addition to active queries")
@pass_state
def wait_events(state, all_states):
    """Shows current wait events for active sessions (what resources they are waiting on)."""
    analyzer = HealthAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.wait_events(not all_states)))


# =====================================================================
# 2. Session & Connection Monitoring
# =====================================================================

@cli.command("long-running-queries", short_help="Find long-running queries")
@click.option("--threshold-minutes", type=int, default=5, show_default=True, help="Only return active queries running longer than this many minutes")
@pass_state
def long_running_queries(state, threshold_minutes):
    """Finds active queries running longer than threshold (default 5 minutes)."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.long_running_queries(threshold_minutes)))


@cli.command("idle-in-transaction-sessions", short_help="Find idle-in-transaction sessions")
@click.option("--threshold-minutes", type=int, default=1, show_default=True, help="Only return idle-in-transaction sessions older than this")
@pass_state
def idle_in_transaction_sessions(state, threshold_minutes):
    """Detects sessions 'idle in transaction' longer than threshold (default 1 minute)."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.idle_in_transaction_sessions(threshold_minutes)))


@cli.command("long-running-transactions", short_help="Find long transactions")
@click.option("--threshold-hours", type=int, default=1, show_default=True, help="Only return non-idle transactions older than this many hours")
@pass_state
def long_running_transactions(state, threshold_hours):
    """Finds non-idle transactions running longer than threshold (default 1 hour)."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.long_running_transactions(threshold_hours)))


@cli.command("long-running-prepared-transactions", short_help="Find stuck 2PC transactions")
@click.option("--threshold-hours", type=int, default=1, show_default=True, help="Only return prepared transactions older than this many hours")
@pass_state
def long_running_prepared_transactions(state, threshold_hours):
    """Finds prepared transactions (2PC) older than threshold."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.long_running_prepared_transactions(threshold_hours)))


@cli.command("connection-usage", short_help="Check connection pool usage")
@pass_state
def connection_usage(state):
    """Reports current connection count vs max_connections."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.connection_usage()))


@cli.command("lock-waiters", short_help="Detailed lock wait analysis")
@click.option("--limit", type=int, default=20, show_default=True, help="Maximum lock-waiter pairs to return")
@pass_state
def lock_waiters(state, limit):
    """Detailed view of all sessions waiting for locks."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.lock_waiters(limit)))


@cli.command("active-queries", short_help="Active query inspection")
@click.option("--min-duration", type=int, default=0, show_default=True, help="Minimum query duration in seconds to include")
@click.option("--include-idle", is_flag=True, help="Include idle connections in results")
@click.option("--include-system", is_flag=True, help="Include system / background processes")
@click.option("--database", default=None, help="Filter results by a specific database name")
@pass_state
def active_queries(state, min_duration, include_idle, include_system, database):
    """Detailed view of currently active queries, blocked queries, and per-state summary."""
    analyzer = SessionAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.active_queries(min_duration, include_idle, include_system, database)))


# =====================================================================
# 3. Performance & Activity
# =====================================================================

@cli.command("cache-hit-rate", short_help="Cache efficiency metric")
@pass_state
def cache_hit_rate(state):
    """Calculates block cache hit rate. Low rate indicates memory pressure or bad queries."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.cache_hit_rate()))


@cli.command("rollback-rate", short_help="Transaction rollback ratio")
@pass_state
def rollback_rate(state):
    """Calculates transaction rollback percentage."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.rollback_rate()))


@cli.command("top-sql-by-time", short_help="Most expensive queries")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum slow queries to return")
@click.option("--min-calls", type=int, default=1, show_default=True, help="Minimum number of calls for inclusion")
@click.option("--min-mean-time", type=float, default=0.0, show_default=True, help="Minimum mean execution time (ms)")
@click.option("--order-by", type=click.Choice(["mean_time", "calls", "rows"]), default="mean_time", show_default=True, help="Column to order results by")
@pass_state
def top_sql_by_time(state, limit, min_calls, min_mean_time, order_by):
    """Top 5 queries by total execution time from pg_stat_statements."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.top_sql_by_time(limit, min_calls, min_mean_time, order_by)))


@cli.command("table-hotspots", short_help="Most active tables")
@click.option("--limit", type=int, default=5, show_default=True, help="Maximum hot-tables to return")
@pass_state
def table_hotspots(state, limit):
    """Tables with highest DML and scan activity."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.table_hotspots(limit)))


@cli.command("bgwriter-stats", short_help="Background writer metrics")
@pass_state
def bgwriter_stats(state):
    """Background writer statistics and buffer allocation."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.bgwriter_stats()))


@cli.command("temp-file-usage", short_help="Temporary file usage")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum databases to return")
@click.option("--threshold-gb", type=float, default=0.0, show_default=True, help="Only include databases with temp_bytes above this many GB")
@pass_state
def temp_file_usage(state, limit, threshold_gb):
    """Shows databases with temporary file usage."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.temp_file_usage(limit, threshold_gb)))


@cli.command("io-statistics", short_help="I/O statistics (summary + pg_stat_io detail)")
@pass_state
def io_statistics(state):
    """I/O statistics for the current database.

    Returns both the high-level ``pg_stat_database`` summary (block
    reads/hits, temp files, I/O timing) and, on PostgreSQL 16+, the
    detailed ``pg_stat_io`` breakdown by backend_type / object / context.
    On older servers the detail block is reported as skipped.
    """
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.io_statistics()))


@cli.command("pg-progress", short_help="Live progress of running PG operations")
@click.option("--operation", type=click.Choice(["analyze", "create-index", "cluster", "vacuum", "all"]), default="all", show_default=True, help="Which pg_stat_progress_* view to query")
@click.option("--include-toast", is_flag=True, help="Include TOAST tables (only affects vacuum)")
@pass_state
def pg_progress(state, operation, include_toast):
    """Active operations from pg_stat_progress_* views: ANALYZE, CREATE INDEX / REINDEX, CLUSTER / VACUUM FULL, and VACUUM."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.pg_progress(operation, include_toast)))


@cli.command("wal-statistics", short_help="WAL activity statistics")
@pass_state
def wal_statistics(state):
    """Reports WAL activity statistics including records, FPIs, bytes written, and buffer fullness."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.wal_statistics()))


@cli.command("slru-stats", short_help="SLRU cache statistics")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum SLRU caches to return")
@pass_state
def slru_stats(state, limit):
    """Reports SLRU (Simple Least-Recently-Used) cache statistics for internal subsystems."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.slru_stats(limit)))


@cli.command("user-function-stats", short_help="UDF performance")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum user functions to return")
@pass_state
def user_function_stats(state, limit):
    """Reports user-defined function performance statistics including call counts and execution time."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.user_function_stats(limit)))


@cli.command("database-conflict-stats", short_help="Recovery conflicts")
@pass_state
def database_conflict_stats(state):
    """Reports query cancellations due to conflicts on standby servers."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.database_conflict_stats()))


@cli.command("checkpointer-stats", short_help="Checkpointer activity + I/O time analysis")
@pass_state
def checkpointer_stats(state):
    """Reports checkpointer activity (timed vs requested checkpoints) plus write/sync time analysis with status assessment."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.checkpointer_stats()))


@cli.command("explain", short_help="EXPLAIN a SQL query, optionally with hypothetical indexes")
@click.option("--sql", required=True, help="SQL query to EXPLAIN")
@click.option("--index", "indexes", multiple=True, help="Hypothetical index spec table:col1,col2[:type][:unique] (repeatable; triggers HypoPG cost comparison)")
@click.option("--analyze/--no-analyze", default=True, show_default=True, help="Execute the query with EXPLAIN ANALYZE (auto-disabled when --index is given)")
@click.option("--buffers/--no-buffers", default=True, show_default=True, help="Include buffer usage statistics")
@click.option("--verbose", is_flag=True, help="Include verbose plan details")
@click.option("--settings", is_flag=True, help="Include configuration settings affecting the plan")
@click.option("--format", "fmt", type=click.Choice(["json", "text", "yaml", "xml"]), default="json", show_default=True, help="EXPLAIN output format (must be json when --index is given)")
@pass_state
def explain(state, sql, indexes, analyze, buffers, verbose, settings, fmt):
    """Run EXPLAIN with optional plan-quality analysis, optionally against HypoPG hypothetical indexes."""
    if indexes and fmt != "json":
        raise click.BadParameter("--index requires --format json")
    hypo = [_parse_index_spec(spec) for spec in indexes] if indexes else None
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(
        analyzer.explain(sql, hypo, analyze, buffers, verbose, settings, fmt)
    ))


@cli.command("table-stats", short_help="Detailed table statistics")
@click.option("--schema", default="public", show_default=True, help="Schema name to analyze")
@click.option("--table", "table_name", default=None, help="Specific table name (analyzes all tables in schema if omitted)")
@click.option("--include-indexes/--no-include-indexes", default=True, show_default=True, help="Include per-index statistics when --table is given")
@click.option("--order-by", type=click.Choice(["size", "rows", "dead_tuples", "seq_scans", "last_vacuum"]), default="size", show_default=True, help="Order metric")
@pass_state
def table_stats(state, schema, table_name, include_indexes, order_by):
    """Detailed user-table statistics with vacuum / scan / dead-tuple analysis."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.table_stats(schema, table_name, include_indexes, order_by)))


@cli.command("disk-io-analysis", short_help="Disk I/O hot-spot analysis")
@click.option("--schema", default="public", show_default=True, help="Schema name to analyze")
@click.option("--include-indexes/--no-include-indexes", default=True, show_default=True, help="Include index I/O patterns")
@click.option("--top-n", type=int, default=20, show_default=True, help="Number of top-I/O objects to show")
@click.option("--analysis-type", type=click.Choice(["all", "buffer_pool", "tables", "indexes"]), default="all", show_default=True, help="Restrict analysis to one area")
@click.option("--min-size-gb", type=float, default=1.0, show_default=True, help="Minimum table/index size (GB) to include")
@pass_state
def disk_io_analysis(state, schema, include_indexes, top_n, analysis_type, min_size_gb):
    """Comprehensive disk I/O analysis: buffer pool, tables, indexes, temp files, checkpoints."""
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.disk_io_analysis(schema, include_indexes, top_n, analysis_type, min_size_gb)))


@cli.command("recommend-indexes", short_help="Index recommendation engine")
@click.option("--max", "max_recs", type=int, default=10, show_default=True, help="Maximum number of recommendations")
@click.option("--min-improvement", type=float, default=10.0, show_default=True, help="Minimum estimated improvement (percent)")
@click.option("--tables", default=None, help="Comma-separated list of target table names to focus on")
@click.option("--queries", default=None, help="Semicolon-separated SQL queries to analyze in place of pg_stat_statements workload")
@click.option("--include-hypothetical/--no-include-hypothetical", default=True, show_default=True, help="Use HypoPG to test candidate indexes")
@pass_state
def recommend_indexes(state, max_recs, min_improvement, tables, queries, include_hypothetical):
    """AI-style index recommendations based on workload or supplied queries."""
    target_tables = tables.split(",") if tables else None
    workload_queries = [q.strip() for q in queries.split(";") if q.strip()] if queries else None
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.recommend_indexes(
        workload_queries=workload_queries,
        max_recommendations=max_recs,
        min_improvement_percent=min_improvement,
        target_tables=target_tables,
        include_hypothetical_testing=include_hypothetical,
    )))


@cli.command("hypothetical-index", short_help="HypoPG hypothetical-index toolkit")
@click.option("--action", required=True, type=click.Choice([
    "check", "create", "list", "drop", "reset",
    "estimate_size", "hide", "unhide", "list_hidden", "explain_with_index",
]), help="HypoPG action to perform")
@click.option("--table", default=None, help="Table name (for create, estimate_size, explain_with_index)")
@click.option("--columns", default=None, help="Comma-separated column names")
@click.option("--index-type", default="btree", show_default=True, help="Index access method (btree, hash, gin, gist, brin)")
@click.option("--index-id", default=None, type=int, help="Index OID (for drop, hide, unhide)")
@click.option("--sql", "sql_query", default=None, help="SQL query (for explain_with_index)")
@click.option("--schema", default=None, help="Schema name (for create, explain_with_index)")
@click.option("--where", default=None, help="Partial index WHERE condition (for create)")
@click.option("--include", default=None, help="Comma-separated INCLUDE columns (for create)")
@click.option("--unique", is_flag=True, help="Create a unique hypothetical index")
@pass_state
def hypothetical_index(state, action, table, columns, index_type, index_id, sql_query, schema, where, include, unique):
    """Manage HypoPG hypothetical indexes (check/create/list/drop/reset/etc.)."""
    kwargs: dict[str, Any] = {}
    if table:
        kwargs["table"] = table
    if columns:
        kwargs["columns"] = columns.split(",")
    if index_type:
        kwargs["index_type"] = index_type
    if index_id is not None:
        kwargs["index_id"] = index_id
    if sql_query:
        kwargs["query"] = sql_query
    if schema:
        kwargs["schema"] = schema
    if where:
        kwargs["where"] = where
    if include:
        kwargs["include"] = include.split(",")
    if unique:
        kwargs["unique"] = unique
    analyzer = PerformanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.hypothetical_index(action, **kwargs)))


# =====================================================================
# 4. Replication & Archiving
# =====================================================================

@cli.command("replication-slots", short_help="Replication slot status")
@pass_state
def replication_slots(state):
    """Checks physical and logical replication slots status."""
    analyzer = ReplicationAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.replication_slots()))


@cli.command("replication-status", short_help="Streaming replica lag")
@pass_state
def replication_status(state):
    """Checks streaming replication lag and status."""
    analyzer = ReplicationAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.replication_status()))


@cli.command("logical-replication-status", short_help="Logical subscription lag")
@pass_state
def logical_replication_status(state):
    """Checks logical subscription lag (send/receive delays)."""
    analyzer = ReplicationAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.logical_replication_status()))


@cli.command("wal-archiver-status", short_help="WAL archiving health")
@pass_state
def wal_archiver_status(state):
    """WAL archiving status and WAL directory size."""
    analyzer = ReplicationAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.wal_archiver_status()))


# =====================================================================
# 5. Maintenance & Storage
# =====================================================================

@cli.command("autovacuum-status", short_help="Active vacuum workers")
@pass_state
def autovacuum_status(state):
    """Checks currently running autovacuum workers."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.autovacuum_status()))


@cli.command("table-bloat", short_help="Table space bloat")
@click.option("--table", "table_name", default=None, help="Specific table name (analyzes the whole schema if omitted)")
@click.option("--schema", default="public", show_default=True, help="Schema name")
@click.option("--approx", is_flag=True, help="Use pgstattuple_approx (fast, approximate)")
@click.option("--min-table-size-gb", type=float, default=5.0, show_default=True, help="Minimum table size (GB) for schema-wide scan")
@click.option("--min-bloat", type=float, default=20.0, show_default=True, help="Only show tables above this bloat percentage")
@click.option("--include-toast", is_flag=True, help="Include TOAST table analysis")
@pass_state
def table_bloat(state, table_name, schema, approx, min_table_size_gb, min_bloat, include_toast):
    """Estimates wasted space (bloat) in top 10 largest tables."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.table_bloat(
        table_name, schema, approx, min_table_size_gb, include_toast, min_bloat
    )))


@cli.command("index-bloat", short_help="Index space bloat")
@click.option("--index", "index_name", default=None, help="Specific index name")
@click.option("--table", "table_name", default=None, help="Analyze all indexes on this table")
@click.option("--schema", default="public", show_default=True, help="Schema name")
@click.option("--min-index-size-gb", type=float, default=5.0, show_default=True, help="Minimum index size (GB) to include")
@click.option("--min-bloat", type=float, default=20.0, show_default=True, help="Only show indexes above this bloat percentage")
@pass_state
def index_bloat(state, index_name, table_name, schema, min_index_size_gb, min_bloat):
    """Estimates wasted space in top 10 largest indexes."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.index_bloat(
        index_name, table_name, schema, min_index_size_gb, min_bloat
    )))


@cli.command("top-objects-by-size", short_help="Largest objects")
@click.option("--limit", type=int, default=5, show_default=True, help="Maximum objects per type to return")
@pass_state
def top_objects_by_size(state, limit):
    """Top 5 largest tables and indexes by disk size."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.top_objects_by_size(limit)))


@cli.command("stale-statistics", short_help="Outdated table stats")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum tables to return")
@pass_state
def stale_statistics(state, limit):
    """Tables with >10% rows modified since last ANALYZE."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.stale_statistics(limit)))


@cli.command("database-sizes", short_help="Database sizes")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum databases to return")
@pass_state
def database_sizes(state, limit):
    """Size of top 10 largest databases."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.database_sizes(limit)))


@cli.command("sequence-exhaustion", short_help="Sequence value exhaustion")
@pass_state
def sequence_exhaustion(state):
    """Sequences approaching max value (>80% used)."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.sequence_exhaustion()))


@cli.command("freeze-prediction", short_help="Freeze storm prediction")
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum tables to return")
@pass_state
def freeze_prediction(state, limit):
    """Predicts which tables are approaching XID/MXID freeze thresholds."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.freeze_prediction(limit)))


@cli.command("list-indexes", short_help="List indexes on a table")
@click.option("--table", "table_name", required=True, help="Table name to list indexes for")
@click.option("--schema", default="public", show_default=True, help="Schema name for the table")
@pass_state
def list_indexes(state, table_name, schema):
    """List all indexes on a specific table with scan counts and definitions."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.list_indexes(table_name, schema)))


@cli.command("pending-vacuum", short_help="Tables needing vacuum")
@click.option("--schema", default="public", show_default=True, help="Schema name")
@click.option("--include-toast", is_flag=True, help="Include TOAST tables")
@click.option("--min-dead-tuples", type=int, default=1000, show_default=True, help="Minimum dead tuples to include a table")
@pass_state
def pending_vacuum(state, schema, include_toast, min_dead_tuples):
    """Tables needing vacuum based on dead-tuple count and wraparound risk."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.pending_vacuum(schema, include_toast, min_dead_tuples)))


@cli.command("vacuum-history", short_help="Recent vacuum activity")
@click.option("--schema", default="public", show_default=True, help="Schema name")
@click.option("--include-toast", is_flag=True, help="Include TOAST tables")
@pass_state
def vacuum_history(state, schema, include_toast):
    """Recent vacuum / analyze activity, categorized by freshness."""
    analyzer = MaintenanceAnalyzer(state.conn.driver)
    state.output(state.run(analyzer.vacuum_history(schema, include_toast)))


# =====================================================================
# Source group (unchanged - kept per user direction)
# =====================================================================

@cli.group()
def source():
    """Manage database source."""
    pass


@source.command("add")
@click.argument("database_uri")
@click.argument("alias")
def source_add(database_uri, alias):
    """Add a new database source.

    Example: pgreport source add postgres://user:pass@host:5432/db mydb
    """
    try:
        add_source(database_uri, alias)
        click.echo(f"Added source '{alias}'.")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@source.command("rm")
@click.argument("alias")
def source_rm(alias):
    """Remove a database source."""
    try:
        remove_source(alias)
        click.echo(f"Removed source '{alias}'.")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@source.command("use")
@click.argument("alias")
def source_use(alias):
    """Switch the active database source (non-interactive).

    Example: pgreport source use mydb
    """
    try:
        set_active_source(alias)
        click.echo(f"Active source set to '{alias}'.")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@source.command("ls")
def source_ls():
    """List database sources and interactively switch the active one."""
    sources = list_sources()
    if not sources:
        click.echo("No sources configured.")
        click.echo("Add one with: pgreport source add <DATABASE_URI> <alias>")
        return

    click.echo("Database sources:\n")
    for i, s in enumerate(sources, 1):
        marker = " (active)" if s["active"] else ""
        dsn = obfuscate_password(s["dsn"]) or s["dsn"]
        click.echo(f"  [{i}] {s['name']}{marker}")
        click.echo(f"      {dsn}")

    click.echo("")
    choice = click.prompt(
        "Select source to activate (enter number, or 'q' to cancel)",
        default="q",
    )

    if choice.strip().lower() == "q":
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(sources):
            click.echo("Invalid selection.", err=True)
            sys.exit(1)
    except ValueError:
        click.echo("Invalid selection.", err=True)
        sys.exit(1)

    selected = sources[idx]
    if selected["active"]:
        click.echo(f"'{selected['name']}' is already the active source.")
        return

    set_active_source(selected["name"])
    click.echo(f"Active source set to '{selected['name']}'.")


# =====================================================================
# REPL
# =====================================================================

def repl(ctx):
    """Interactive REPL mode."""
    state = ctx.ensure_object(CliState)
    click.echo("pgreport CLI - Interactive Mode")
    click.echo(f"Connected to: {state.conn.safe_uri()}")
    click.echo("Type 'help' for available commands, 'quit' to exit.\n")

    while True:
        try:
            line = input("pgreport> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("\nGoodbye!")
            break

        if not line:
            continue
        if line in ("quit", "exit", "q"):
            click.echo("Goodbye!")
            break
        if line == "help":
            click.echo(ctx.get_help())
            continue

        args = line.split()
        try:
            with ctx.scope() as sub_ctx:
                cli.parse_args(sub_ctx, args)
                cli.invoke(sub_ctx)
        except SystemExit:
            pass
        except click.UsageError as e:
            click.echo(f"Usage error: {e}")
        except Exception as e:
            click.echo(f"Error: {e}", err=True)


def main():
    """Main entry point."""
    cli(obj=CliState())


if __name__ == "__main__":
    main()
