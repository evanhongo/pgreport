"""Mocked unit tests for pgreport CLI harness core modules.

Tests cover the 5-category analyzers (HealthAnalyzer, SessionAnalyzer,
PerformanceAnalyzer, ReplicationAnalyzer, MaintenanceAnalyzer) plus
_parse_index_spec, REPL, Config, and representative CLI invocations.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pgreport.utils.sql_driver import SqlDriver, DbConnPool, obfuscate_password
from pgreport.core.health import HealthAnalyzer
from pgreport.core.session import SessionAnalyzer
from pgreport.core.performance import PerformanceAnalyzer
from pgreport.core.replication import ReplicationAnalyzer
from pgreport.core.maintenance import MaintenanceAnalyzer
from pgreport.pgreport_cli import _parse_index_spec, cli, CliState


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_driver():
    """Create a mocked SqlDriver with an AsyncMock execute_query."""
    pool = MagicMock(spec=DbConnPool)
    pool.is_valid = True
    pool.pool = MagicMock()
    driver = SqlDriver(pool)
    driver.execute_query = AsyncMock(return_value=[])
    return driver


def _make_state(mock_driver):
    """Build a CliState wired to the mock driver, ready for runner.invoke(obj=state)."""
    state = CliState()
    state.json_mode = True
    state.loop = asyncio.new_event_loop()
    state.conn = MagicMock()
    state.conn.driver = mock_driver
    state.conn.safe_uri.return_value = "postgresql://user:****@host/db"
    # disconnect() is awaited by CliState.disconnect via ctx.call_on_close;
    # make it an AsyncMock so asyncio.run_until_complete accepts the coroutine.
    state.conn.disconnect = AsyncMock(return_value=None)
    return state


# ─── _parse_index_spec ──────────────────────────────────────────────────


class TestParseIndexSpec:
    def test_basic_spec(self):
        result = _parse_index_spec("orders:user_id")
        assert result == {"table": "orders", "columns": ["user_id"], "index_type": "btree", "unique": False}

    def test_multi_column(self):
        result = _parse_index_spec("orders:user_id,created_at")
        assert result["columns"] == ["user_id", "created_at"]

    def test_with_type(self):
        result = _parse_index_spec("orders:user_id:hash")
        assert result["index_type"] == "hash"

    def test_with_unique(self):
        result = _parse_index_spec("orders:email:btree:unique")
        assert result["unique"] is True
        assert result["index_type"] == "btree"

    def test_unique_without_type(self):
        result = _parse_index_spec("orders:email:unique")
        assert result["unique"] is True

    def test_invalid_spec_no_columns(self):
        import click
        with pytest.raises(click.BadParameter, match="Invalid index spec"):
            _parse_index_spec("orders")


# ─── obfuscate_password ────────────────────────────────────────────────


class TestObfuscatePassword:
    def test_with_password(self):
        result = obfuscate_password("postgresql://user:secret@host:5432/db")
        assert "secret" not in result
        assert "****" in result

    def test_without_password(self):
        result = obfuscate_password("postgresql://user@host/db")
        assert result == "postgresql://user@host/db"

    def test_none(self):
        assert obfuscate_password(None) is None

    def test_empty(self):
        assert obfuscate_password("") is None

    def test_malformed(self):
        result = obfuscate_password("not-a-uri")
        assert result is not None


# ─── HealthAnalyzer ─────────────────────────────────────────────────────


class TestHealthAnalyzer:
    @pytest.fixture
    def analyzer(self, mock_driver):
        return HealthAnalyzer(mock_driver)

    async def test_wraparound_risk_xid_only(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"datname": "db1", "xid_age": 100, "percentage_used": 0.05}
        ]
        result = await analyzer.wraparound_risk("xid")
        assert isinstance(result, list)
        assert result[0]["datname"] == "db1"
        assert mock_driver.execute_query.call_count == 1

    async def test_wraparound_risk_mxid_empty(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.wraparound_risk("mxid")
        assert result == []

    async def test_wraparound_risk_both(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [{"datname": "db1", "xid_age": 100, "percentage_used": 0.05}],
            [{"datname": "db1", "mxid_age": 200, "status": "OK"}],
        ]
        result = await analyzer.wraparound_risk("both")
        assert isinstance(result, dict)
        assert "xid" in result and "mxid" in result
        assert result["xid"][0]["datname"] == "db1"
        assert result["mxid"][0]["status"] == "OK"
        assert mock_driver.execute_query.call_count == 2

    async def test_blocking_locks_empty(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.blocking_locks()
        assert result == []

    async def test_deadlock_detection(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [{"datname": "db1", "deadlocks": 2}]
        result = await analyzer.deadlock_detection()
        assert result[0]["deadlocks"] == 2

    async def test_connection_security_status_passes_limit(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.connection_security_status(limit=25)
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [25]

    async def test_wait_events(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [{"wait_event_type": "Lock", "wait_event": "tuple", "count": 3, "pids": [1, 2, 3]}],
            [{"wait_event_type": "Lock", "count": 3}],
        ]
        result = await analyzer.wait_events(True)
        assert "wait_events" in result
        assert "by_type" in result

    async def test_health_check_healthy(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [{"max_conn": 100, "used": 10, "res_for_super": 3, "used_pct": 10.0}],
            [{"buffer_hit_ratio": 99.5, "index_hit_ratio": 99.8}],
            [{"total_locks": 5, "waiting_locks": 0, "waiting_processes": 0}],
            [{"blocking_count": 0}],
            [],
            [{"datname": "mydb", "xid_age": 1000, "xids_remaining": 2147482647, "pct_towards_wraparound": 0.0}],
            [{"datname": "mydb", "size": "100 MB", "size_bytes": 104857600}],
            [{"checkpoints_timed": 100, "checkpoints_req": 5, "timed_pct": 95.2, "buffers_checkpoint": 10000, "buffers_clean": 5000, "buffers_backend": 100, "backend_pct": 0.7}],
            [{"total_checkpoints": 105, "seconds_since_start": 360000, "checkpoints_per_hour": 1.05}],
            [
                {"name": "shared_buffers", "setting": "256", "unit": "MB"},
                {"name": "work_mem", "setting": "8", "unit": "MB"},
                {"name": "checkpoint_completion_target", "setting": "0.9", "unit": None},
                {"name": "autovacuum", "setting": "on", "unit": None},
            ],
        ]
        result = await analyzer.health_check(True, False)
        assert result["status"] == "healthy"
        assert result["overall_score"] >= 90
        assert "settings" in result["checks"]

    async def test_health_check_settings_autovacuum_off_critical(self, analyzer, mock_driver):
        """When autovacuum is off, _check_critical_settings emits a critical severity item."""
        mock_driver.execute_query.side_effect = [
            [{"max_conn": 100, "used": 10, "res_for_super": 3, "used_pct": 10.0}],
            [{"buffer_hit_ratio": 99.5, "index_hit_ratio": 99.8}],
            [{"total_locks": 5, "waiting_locks": 0, "waiting_processes": 0}],
            [{"blocking_count": 0}],
            [],
            [{"datname": "mydb", "xid_age": 1000, "xids_remaining": 2147482647, "pct_towards_wraparound": 0.0}],
            [{"datname": "mydb", "size": "100 MB", "size_bytes": 104857600}],
            [{"checkpoints_timed": 100, "checkpoints_req": 5, "timed_pct": 95.2, "buffers_checkpoint": 10000, "buffers_clean": 5000, "buffers_backend": 100, "backend_pct": 0.7}],
            [{"total_checkpoints": 105, "seconds_since_start": 360000, "checkpoints_per_hour": 1.05}],
            [{"name": "autovacuum", "setting": "off", "unit": None}],
        ]
        result = await analyzer.health_check(True, False)
        severities = [r["severity"] for r in result["checks"]["settings"]["recommendations"]]
        assert "critical" in severities
        assert result["checks"]["settings"]["score"] == 60

    async def test_index_health(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.index_health("public")
        assert result["schema"] == "public"
        assert result["summary"]["invalid_indexes"] == 0
        assert result["summary"]["unused_indexes"] == 0
        assert result["summary"]["duplicate_indexes"] == 0
        assert result["score"] == 100

    async def test_index_health_with_min_size(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.index_health("myschema", min_size_mb=50.0)
        # unused_query is the 2nd query (invalid → unused → duplicate)
        unused_call = mock_driver.execute_query.call_args_list[1]
        params = unused_call.args[1]
        assert params == ["myschema", 50.0]

    async def test_index_health_no_duplicates(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.index_health("public", include_duplicates=False)
        assert "duplicate_indexes" not in result
        # Only 2 queries should run: invalid + unused
        assert mock_driver.execute_query.call_count == 2


# ─── SessionAnalyzer ────────────────────────────────────────────────────


class TestSessionAnalyzer:
    @pytest.fixture
    def analyzer(self, mock_driver):
        return SessionAnalyzer(mock_driver)

    async def test_long_running_queries_passes_threshold(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.long_running_queries(threshold_minutes=10)
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [10]

    async def test_idle_in_transaction_sessions_default(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.idle_in_transaction_sessions()
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [1]

    async def test_long_running_transactions_default_hours(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.long_running_transactions()
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [1]

    async def test_long_running_prepared_transactions(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"gid": "g1", "owner": "alice", "database": "db", "duration": "1h"}
        ]
        result = await analyzer.long_running_prepared_transactions(threshold_hours=2)
        assert result[0]["gid"] == "g1"

    async def test_connection_usage(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [{"used_connections": 10, "max_connections": 100}]
        result = await analyzer.connection_usage()
        assert result[0]["used_connections"] == 10

    async def test_lock_waiters_passes_limit(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.lock_waiters(limit=15)
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [15]

    async def test_active_queries_returns_envelope(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [{"pid": 1, "database": "db", "username": "u", "state": "active",
              "duration_seconds": 5, "transaction_seconds": 5, "query": "SELECT 1"}],
            [{"state": "active", "count": 1}],
            [],
        ]
        result = await analyzer.active_queries(0, False, False, None)
        assert result["summary"]["total_connections"] == 1
        assert result["active_queries"][0]["pid"] == 1


# ─── PerformanceAnalyzer ───────────────────────────────────────────────


class TestPerformanceAnalyzer:
    @pytest.fixture
    def analyzer(self, mock_driver):
        return PerformanceAnalyzer(mock_driver)

    async def test_cache_hit_rate(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"datname": "db", "blks_read": 100, "blks_hit": 9900, "hit_rate_percentage": 99.0}
        ]
        result = await analyzer.cache_hit_rate()
        assert result[0]["hit_rate_percentage"] == 99.0

    async def test_rollback_rate(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"datname": "db", "xact_commit": 100, "xact_rollback": 5, "rollback_percentage": 4.76}
        ]
        result = await analyzer.rollback_rate()
        assert result[0]["rollback_percentage"] == 4.76

    async def test_pg_progress_no_ops(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.pg_progress("all", False)
        assert "message" in result
        assert result["summary"]["total_count"] == 0
        # 4 progress views queried in "all" mode
        assert mock_driver.execute_query.call_count == 4

    async def test_pg_progress_single_operation(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.pg_progress("vacuum", False)
        assert "vacuum" in result
        assert "analyze" not in result
        assert mock_driver.execute_query.call_count == 1

    async def test_top_sql_by_time_no_extension(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.top_sql_by_time(limit=5)
        assert "error" in result
        assert "pg_stat_statements" in result["error"]

    async def test_top_sql_by_time_with_results(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [{"?column?": 1}],
            [
                {"queryid": 1, "query_text": "SELECT 1", "calls": 10, "mean_time_ms": 50.0,
                 "min_time_ms": 5, "max_time_ms": 100, "stddev_time_ms": 10,
                 "rows": 10, "shared_blks_hit": 100, "shared_blks_read": 0,
                 "cache_hit_ratio": 100.0, "temp_blks_read": 0, "temp_blks_written": 0}
            ],
        ]
        result = await analyzer.top_sql_by_time(limit=5)
        assert "slow_queries" in result
        assert len(result["slow_queries"]) == 1

    async def test_top_sql_by_time_invalid_order_by(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [{"?column?": 1}],
            [],
        ]
        await analyzer.top_sql_by_time(order_by="bogus")
        # Should fall back to mean_time without crashing

    async def test_table_hotspots_passes_limit(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.table_hotspots(limit=3)
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [3]

    async def test_temp_file_usage_passes_threshold(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.temp_file_usage(limit=10, threshold_gb=2.5)
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [2.5, 10]

    async def test_wal_statistics_handles_missing_view(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = Exception("relation does not exist")
        result = await analyzer.wal_statistics()
        assert isinstance(result, dict)
        assert result["status"] == "skipped"

    async def test_io_statistics_merges_summary_and_detail(self, analyzer, mock_driver):
        db_row = [{"datname": "postgres", "blks_read": 1, "blks_hit": 2}]
        io_rows = [{"backend_type": "client backend", "reads": 5}]
        mock_driver.execute_query.side_effect = [db_row, io_rows]
        result = await analyzer.io_statistics()
        assert result["database"] == db_row
        assert result["by_context"] == io_rows

    async def test_io_statistics_skips_v2_on_old_postgres(self, analyzer, mock_driver):
        db_row = [{"datname": "postgres", "blks_read": 0}]
        mock_driver.execute_query.side_effect = [
            db_row,
            Exception("relation \"pg_stat_io\" does not exist"),
        ]
        result = await analyzer.io_statistics()
        assert result["database"] == db_row
        assert isinstance(result["by_context"], dict)
        assert result["by_context"]["status"] == "skipped"

    async def test_explain_json_format(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"QUERY PLAN": [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 100.0}}]}
        ]
        result = await analyzer.explain("SELECT 1", analyze=False, fmt="json")
        assert "execution_plan" in result
        assert "analysis" in result

    async def test_explain_text_format(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [{"QUERY PLAN": "Seq Scan ..."}]
        result = await analyzer.explain("SELECT 1", fmt="text")
        assert "execution_plan" in result
        assert "analysis" not in result  # text format has no heuristic analysis

    async def test_explain_no_result(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.explain("SELECT 1")
        assert "error" in result

    async def test_explain_with_indexes_rejects_non_json(self, analyzer):
        with pytest.raises(ValueError, match="format json"):
            await analyzer.explain(
                "SELECT 1",
                hypothetical_indexes=[{"table": "t", "columns": ["id"]}],
                fmt="text",
            )

    async def test_explain_with_indexes_auto_disables_analyze(self, analyzer, mock_driver):
        # Plan response for the original EXPLAIN
        mock_driver.execute_query.return_value = [
            {"QUERY PLAN": [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 100.0}}]}
        ]
        analyzer.hypopg.check_status = AsyncMock(
            return_value=MagicMock(is_installed=False)
        )
        result = await analyzer.explain(
            "SELECT 1",
            hypothetical_indexes=[{"table": "t", "columns": ["id"]}],
            analyze=True,
        )
        # analyze was True but should be flipped off + a note added
        assert result["explain_options"]["analyze"] is False
        assert "note" in result
        # hypopg not installed → returns with error after original plan
        assert "error" in result

    async def test_analyze_plan_seq_scan_warning(self, analyzer):
        plan = [{"Plan": {"Node Type": "Seq Scan", "Actual Rows": 100000, "Relation Name": "big"}}]
        analysis = analyzer._analyze_plan(plan, True)
        assert any("Sequential scan" in w for w in analysis["warnings"])

    async def test_analyze_plan_row_mismatch_warning(self, analyzer):
        plan = [{"Plan": {"Node Type": "Index Scan", "Actual Rows": 10000, "Plan Rows": 100}}]
        analysis = analyzer._analyze_plan(plan, True)
        assert any("Row estimate" in w for w in analysis["warnings"])

    async def test_table_stats_returns_tables(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"table_name": "t", "schema_name": "public", "n_live_tup": 100,
             "n_dead_tup": 0, "dead_tuple_ratio": 0, "seq_scan": 0,
             "index_scan_ratio": 100, "total_size_bytes": 1024}
        ]
        result = await analyzer.table_stats("public", None, False, "size")
        assert result["table_count"] == 1
        assert result["tables"][0]["table_name"] == "t"

    async def test_disk_io_analysis_all(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.disk_io_analysis("public", True, 5, "all", 0)
        assert "io_patterns" in result
        assert "analysis" in result

    async def test_recommend_indexes_with_queries(self, analyzer, mock_driver):
        from pgreport.utils.index_advisor import WorkloadAnalysisResult
        analyzer.advisor.analyze_queries = AsyncMock(
            return_value=WorkloadAnalysisResult(analyzed_queries=1, recommendations=[])
        )
        analyzer.advisor.hypopg.check_status = AsyncMock(
            return_value=MagicMock(is_installed=False)
        )
        result = await analyzer.recommend_indexes(
            workload_queries=["SELECT * FROM users WHERE id=1"]
        )
        assert result["summary"]["analysis_source"] == "provided_queries"

    async def test_hypothetical_index_check(self, analyzer):
        analyzer.hypopg.check_status = AsyncMock(
            return_value=MagicMock(is_installed=True, version="1.4", message="ok")
        )
        result = await analyzer.hypothetical_index("check")
        assert result["hypopg_available"] is True

    async def test_hypothetical_index_unknown_action(self, analyzer):
        result = await analyzer.hypothetical_index("nonsense")
        assert "error" in result


# ─── ReplicationAnalyzer ───────────────────────────────────────────────


class TestReplicationAnalyzer:
    @pytest.fixture
    def analyzer(self, mock_driver):
        return ReplicationAnalyzer(mock_driver)

    async def test_replication_slots_empty(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.replication_slots()
        assert result == []

    async def test_replication_status(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"application_name": "r1", "client_addr": "10.0.0.1", "state": "streaming",
             "sent_lag_bytes": 0, "replay_lag_bytes": 0}
        ]
        result = await analyzer.replication_status()
        assert result[0]["state"] == "streaming"

    async def test_logical_replication_status(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.logical_replication_status()
        assert result == []

    async def test_wal_archiver_status(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"archived_count": 100, "failed_count": 0, "wal_directory_size": "1 GB"}
        ]
        result = await analyzer.wal_archiver_status()
        assert result[0]["archived_count"] == 100


# ─── MaintenanceAnalyzer ──────────────────────────────────────────────


class TestMaintenanceAnalyzer:
    @pytest.fixture
    def analyzer(self, mock_driver):
        return MaintenanceAnalyzer(mock_driver)

    async def test_top_objects_by_size_passes_limit(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        await analyzer.top_objects_by_size(limit=7)
        _sql, params = mock_driver.execute_query.call_args.args
        # Limit appears twice (table + index branches)
        assert params == [7, 7]

    async def test_stale_statistics(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"schemaname": "public", "relname": "t", "n_live_tup": 1000,
             "modified_percent": 25.0}
        ]
        result = await analyzer.stale_statistics(limit=5)
        assert result[0]["modified_percent"] == 25.0

    async def test_database_sizes(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"datname": "db1", "size": "10 GB"}
        ]
        result = await analyzer.database_sizes(limit=10)
        assert result[0]["datname"] == "db1"

    async def test_sequence_exhaustion_empty(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.sequence_exhaustion()
        assert result == []

    async def test_freeze_prediction(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = [
            {"table_name": "public.t", "schemaname": "public",
             "total_size": "1 GB", "freeze_status": "WARNING"}
        ]
        result = await analyzer.freeze_prediction(limit=10)
        assert result[0]["freeze_status"] == "WARNING"

    async def test_autovacuum_status(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [
                {"name": "autovacuum", "setting": "on", "unit": None, "short_desc": ""},
                {"name": "autovacuum_max_workers", "setting": "3", "unit": None, "short_desc": ""},
                {"name": "autovacuum_naptime", "setting": "60", "unit": None, "short_desc": ""},
            ],
            [],
            [],
        ]
        result = await analyzer.autovacuum_status()
        assert result["autovacuum_enabled"] is True
        assert result["summary"]["max_workers"] == 3

    async def test_table_bloat_no_extension(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.table_bloat(table_name="t", schema_name="public")
        assert "error" in result
        assert "pgstattuple" in result["error"]

    async def test_index_bloat_no_extension(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.index_bloat(index_name="ix")
        assert "error" in result

    async def test_list_indexes(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [
                {"index_name": "ix1", "access_method": "btree", "columns": ["id"],
                 "is_unique": True, "is_primary": True, "is_valid": True,
                 "size": "100 KB", "size_bytes": 100000, "definition": "..."}
            ],
            [{"index_name": "ix1", "scans": 1000, "tuples_read": 5000, "tuples_fetched": 5000}],
        ]
        result = await analyzer.list_indexes("users", "public")
        assert result["index_count"] == 1
        assert result["indexes"][0]["scans"] == 1000

    async def test_pending_vacuum(self, analyzer, mock_driver):
        mock_driver.execute_query.side_effect = [
            [
                {"schema_name": "public", "table_name": "t",
                 "n_live_tup": 1000, "n_dead_tup": 2000,
                 "dead_tuple_ratio": 200, "table_size": "10 MB",
                 "exceeds_threshold": True}
            ],
            [],
        ]
        result = await analyzer.pending_vacuum("public", False, 1000)
        assert result["summary"]["tables_with_dead_tuples"] == 1

    async def test_vacuum_history_empty(self, analyzer, mock_driver):
        mock_driver.execute_query.return_value = []
        result = await analyzer.vacuum_history("public", False)
        assert result["summary"]["total_tables"] == 0


# ─── CLI integration with mocks ───────────────────────────────────────


def _invoke_cli(runner, mock_driver, state, args):
    """Invoke ``cli`` with a stub ``--database-uri`` and a no-op connect.

    The CliState already has a mocked ``conn``; we just need to keep the
    top-level group from rebuilding it via ``state.connect``.
    """
    with patch.object(CliState, "connect", lambda self, uri=None: None):
        return runner.invoke(
            cli,
            ["--database-uri", "postgresql://fake@host/db", *args],
            obj=state,
        )


class TestCLIWithMocks:
    """Verify representative commands end-to-end with mocked SqlDriver."""

    def test_top_sql_by_time_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.side_effect = [
            [{"?column?": 1}],
            [{"queryid": 1, "query_text": "SELECT 1", "calls": 10, "mean_time_ms": 50.0,
              "min_time_ms": 5, "max_time_ms": 100, "stddev_time_ms": 10,
              "rows": 10, "shared_blks_hit": 100, "shared_blks_read": 0,
              "cache_hit_ratio": 100.0, "temp_blks_read": 0, "temp_blks_written": 0}],
        ]
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state,
                              ["--json", "top-sql-by-time", "--limit", "5"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "success"
        assert "slow_queries" in output["data"]

    def test_long_running_queries_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = []
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state,
                              ["--json", "long-running-queries", "--threshold-minutes", "10"])
        assert result.exit_code == 0, result.output
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [10]

    def test_cache_hit_rate_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = [
            {"datname": "db", "blks_read": 1, "blks_hit": 99, "hit_rate_percentage": 99.0}
        ]
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state, ["--json", "cache-hit-rate"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["data"][0]["hit_rate_percentage"] == 99.0

    def test_index_health_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = []
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state,
                              ["--json", "index-health", "--min-size-mb", "10"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["data"]["score"] == 100

    def test_replication_slots_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = []
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state, ["--json", "replication-slots"])
        assert result.exit_code == 0, result.output

    def test_database_sizes_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = [{"datname": "x", "size": "10 GB"}]
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state,
                              ["--json", "database-sizes", "--limit", "5"])
        assert result.exit_code == 0, result.output
        out = json.loads(result.output)
        assert out["status"] == "success"
        assert out["data"][0]["size"] == "10 GB"

    def test_temp_file_usage_threshold_passed(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = []
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state,
                              ["--json", "temp-file-usage", "--threshold-gb", "0.5",
                               "--limit", "5"])
        assert result.exit_code == 0, result.output
        _sql, params = mock_driver.execute_query.call_args.args
        assert params == [0.5, 5]

    def test_health_check_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.side_effect = [
            [{"max_conn": 100, "used": 10, "res_for_super": 3, "used_pct": 10.0}],
            [{"buffer_hit_ratio": 99.5, "index_hit_ratio": 99.8}],
            [{"total_locks": 5, "waiting_locks": 0, "waiting_processes": 0}],
            [{"blocking_count": 0}],
            [],
            [{"datname": "mydb", "xid_age": 1000, "xids_remaining": 2147482647, "pct_towards_wraparound": 0.0}],
            [{"datname": "mydb", "size": "100 MB", "size_bytes": 104857600}],
            [{"checkpoints_timed": 100, "checkpoints_req": 5, "timed_pct": 95.2, "buffers_checkpoint": 10000, "buffers_clean": 5000, "buffers_backend": 100, "backend_pct": 0.7}],
            [{"total_checkpoints": 105, "seconds_since_start": 360000, "checkpoints_per_hour": 1.05}],
            [
                {"name": "shared_buffers", "setting": "256", "unit": "MB"},
                {"name": "work_mem", "setting": "8", "unit": "MB"},
                {"name": "checkpoint_completion_target", "setting": "0.9", "unit": None},
                {"name": "autovacuum", "setting": "on", "unit": None},
            ],
        ]
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state, ["--json", "health-check"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "success"
        assert output["data"]["status"] == "healthy"

    def test_pg_progress_via_cli(self, mock_driver):
        from click.testing import CliRunner

        mock_driver.execute_query.return_value = []
        state = _make_state(mock_driver)
        runner = CliRunner()
        result = _invoke_cli(runner, mock_driver, state,
                              ["--json", "pg-progress", "--operation", "vacuum"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["status"] == "success"
        assert "message" in output["data"]
        assert output["data"]["summary"]["total_count"] == 0


# ─── REPL ───────────────────────────────────────────────────────────────


class TestREPL:
    """Test REPL by patching CliState.connect to skip real DB connection."""

    def test_repl_help_and_quit(self, mock_driver):
        from click.testing import CliRunner

        state = _make_state(mock_driver)
        state.json_mode = False
        with patch.object(CliState, "connect", lambda self, uri=None: None):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["--database-uri", "postgresql://fake@host/db"],
                obj=state,
                input="help\nquit\n",
            )
        assert "Interactive Mode" in result.output
        assert "Goodbye" in result.output

    def test_repl_empty_line_and_exit(self, mock_driver):
        from click.testing import CliRunner

        state = _make_state(mock_driver)
        state.json_mode = False
        with patch.object(CliState, "connect", lambda self, uri=None: None):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["--database-uri", "postgresql://fake@host/db"],
                obj=state,
                input="\nexit\n",
            )
        assert "Goodbye" in result.output

    def test_repl_invalid_command(self, mock_driver):
        from click.testing import CliRunner

        state = _make_state(mock_driver)
        state.json_mode = False
        with patch.object(CliState, "connect", lambda self, uri=None: None):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["--database-uri", "postgresql://fake@host/db"],
                obj=state,
                input="this_does_not_exist\nquit\n",
            )
        # REPL should not crash on unknown command
        assert result.exit_code == 0


# ─── Config Module ─────────────────────────────────────────────────────


class TestConfigModule:
    @pytest.fixture(autouse=True)
    def isolated_config(self, tmp_path, monkeypatch):
        """Force the config module to use a temp config file."""
        monkeypatch.setattr("pgreport.utils.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr(
            "pgreport.utils.config.CONFIG_FILE",
            tmp_path / "config.toml",
        )
        yield

    def test_add_and_list_sources(self):
        from pgreport.utils.config import add_source, list_sources
        add_source("postgresql://u:p@h/db", "dev")
        sources = list_sources()
        assert any(s["name"] == "dev" for s in sources)

    def test_add_duplicate_raises(self):
        from pgreport.utils.config import add_source
        add_source("postgresql://u:p@h/db", "dev")
        with pytest.raises(ValueError):
            add_source("postgresql://u:p@h/db2", "dev")

    def test_remove_source(self):
        from pgreport.utils.config import add_source, remove_source, list_sources
        add_source("postgresql://u:p@h/db", "dev")
        remove_source("dev")
        sources = list_sources()
        assert all(s["name"] != "dev" for s in sources)

    def test_remove_nonexistent_raises(self):
        from pgreport.utils.config import remove_source
        with pytest.raises(ValueError):
            remove_source("nope")

    def test_set_active_source(self):
        from pgreport.utils.config import add_source, set_active_source, list_sources
        add_source("postgresql://u:p@h/db1", "dev")
        add_source("postgresql://u:p@h/db2", "prod")
        set_active_source("prod")
        active = next(s for s in list_sources() if s["active"])
        assert active["name"] == "prod"

    def test_set_active_nonexistent_raises(self):
        from pgreport.utils.config import set_active_source
        with pytest.raises(ValueError):
            set_active_source("ghost")

    def test_get_active_dsn_empty(self):
        from pgreport.utils.config import get_active_dsn
        assert get_active_dsn() is None

    def test_get_active_dsn_returns_value(self):
        from pgreport.utils.config import add_source, get_active_dsn
        add_source("postgresql://u:p@h/db", "dev")
        assert get_active_dsn() == "postgresql://u:p@h/db"


# ─── Source CLI commands ──────────────────────────────────────────────


class TestSourceCLICommands:
    """Source uses the nested group form for database-alias management."""

    @pytest.fixture(autouse=True)
    def isolated_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pgreport.utils.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr(
            "pgreport.utils.config.CONFIG_FILE",
            tmp_path / "config.toml",
        )
        yield

    def test_source_add(self):
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "add", "postgresql://u:p@h/db", "mydb"])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_source_add_duplicate(self):
        from click.testing import CliRunner
        from pgreport.utils.config import add_source

        add_source("postgresql://u:p@h/db", "mydb")
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "add", "postgresql://u:p@h/db2", "mydb"])
        assert result.exit_code != 0

    def test_source_rm(self):
        from click.testing import CliRunner
        from pgreport.utils.config import add_source

        add_source("postgresql://u:p@h/db", "mydb")
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "rm", "mydb"])
        assert result.exit_code == 0

    def test_source_rm_nonexistent(self):
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "rm", "nope"])
        assert result.exit_code != 0

    def test_source_use(self):
        from click.testing import CliRunner
        from pgreport.utils.config import add_source, list_sources

        add_source("postgresql://u:p@h/db1", "dev")
        add_source("postgresql://u:p@h/db2", "prod")
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "use", "prod"])
        assert result.exit_code == 0

    def test_source_use_nonexistent(self):
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "use", "nope"])
        assert result.exit_code != 0

    def test_source_ls_empty(self):
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "ls"], input="q\n")
        assert "No sources" in result.output

    def test_source_ls_shows_sources(self):
        from click.testing import CliRunner
        from pgreport.utils.config import add_source

        add_source("postgresql://u:p@h/db", "dev")
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "ls"], input="q\n")
        assert "dev" in result.output
