"""Unit tests for pgreport CLI harness core modules."""

import json
import pytest

from pgreport.utils.formatting import (
    format_json,
    format_size,
    format_duration,
    format_table,
)
from pgreport.utils.connection import ConnectionManager


# ─── Formatting Tests ───────────────────────────────────────────────────


class TestFormatJson:
    def test_valid_json_output(self):
        data = {"key": "value", "num": 42}
        result = format_json(data)
        parsed = json.loads(result)
        assert parsed == data

    def test_compact_mode(self):
        data = {"a": 1}
        result = format_json(data, compact=True)
        assert "\n" not in result
        assert json.loads(result) == data

    def test_handles_non_serializable(self):
        from datetime import datetime
        data = {"ts": datetime(2025, 1, 1)}
        result = format_json(data)
        parsed = json.loads(result)
        assert "2025" in parsed["ts"]


class TestFormatSize:
    def test_bytes(self):
        assert format_size(500) == "500.0 B"

    def test_kilobytes(self):
        result = format_size(2048)
        assert "KB" in result

    def test_megabytes(self):
        result = format_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = format_size(3 * 1024**3)
        assert "GB" in result

    def test_none(self):
        assert format_size(None) == "0 B"

    def test_zero(self):
        assert format_size(0) == "0 B"


class TestFormatDuration:
    def test_seconds(self):
        assert format_duration(45) == "45s"

    def test_minutes(self):
        assert format_duration(125) == "2m 5s"

    def test_hours(self):
        assert format_duration(3665) == "1h 1m"

    def test_zero(self):
        assert format_duration(0) == "0s"

    def test_none(self):
        assert format_duration(None) == "0s"


class TestFormatTable:
    def test_basic_table(self):
        rows = [
            {"name": "foo", "value": 1},
            {"name": "bar", "value": 2},
        ]
        result = format_table(rows)
        assert "foo" in result
        assert "bar" in result
        assert "name" in result

    def test_custom_columns(self):
        rows = [{"a": 1, "b": 2, "c": 3}]
        result = format_table(rows, columns=["a", "c"])
        assert "a" in result
        assert "c" in result
        lines = result.split("\n")
        assert "b" not in lines[0]

    def test_empty_data(self):
        assert format_table([]) == "(no data)"


# ─── ConnectionManager Tests ────────────────────────────────────────────


class TestConnectionManager:
    def test_init_with_uri(self):
        cm = ConnectionManager("postgresql://user:pass@localhost/db")
        assert cm.uri == "postgresql://user:pass@localhost/db"

    def test_init_without_uri(self):
        cm = ConnectionManager()
        assert cm.uri is None

    def test_safe_uri_obfuscates(self):
        cm = ConnectionManager("postgresql://user:secret@host:5432/db")
        safe = cm.safe_uri()
        assert "secret" not in safe
        assert "****" in safe

    def test_not_connected_initially(self):
        cm = ConnectionManager("postgresql://user:pass@host/db")
        assert cm.is_connected is False

    def test_driver_raises_when_not_connected(self):
        cm = ConnectionManager("postgresql://user:pass@host/db")
        with pytest.raises(RuntimeError, match="Not connected"):
            _ = cm.driver


# ─── CLI Structure Tests ────────────────────────────────────────────────


class TestCLIStructure:
    def test_cli_help(self):
        from click.testing import CliRunner
        from pgreport.pgreport_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "pgreport CLI" in result.output

    def test_source_group_registered(self):
        """`source` is the one nested Click group, for database aliases."""
        from pgreport.pgreport_cli import cli

        assert "source" in cli.commands

    def test_commands_registered(self):
        """A representative subset of the expected commands must be present."""
        from pgreport.pgreport_cli import cli

        expected = [
            # Core Health & Availability
            "wraparound-risk", "blocking-locks", "deadlock-detection",
            "connection-security-status", "health-check",
            "index-health", "wait-events",
            # Session & Connection Monitoring
            "long-running-queries", "idle-in-transaction-sessions",
            "long-running-transactions", "long-running-prepared-transactions",
            "connection-usage", "lock-waiters", "active-queries",
            # Performance & Activity
            "cache-hit-rate", "rollback-rate", "top-sql-by-time", "table-hotspots",
            "bgwriter-stats", "temp-file-usage", "io-statistics",
            "pg-progress",
            "wal-statistics", "slru-stats", "user-function-stats",
            "database-conflict-stats", "checkpointer-stats",
            "explain", "table-stats",
            "disk-io-analysis", "recommend-indexes",
            "hypothetical-index",
            # Replication & Archiving
            "replication-slots", "replication-status", "logical-replication-status",
            "wal-archiver-status",
            # Maintenance & Storage
            "autovacuum-status", "table-bloat", "index-bloat", "top-objects-by-size",
            "stale-statistics", "database-sizes",
            "sequence-exhaustion", "freeze-prediction", "list-indexes",
            "pending-vacuum", "vacuum-history",
        ]
        registered = set(cli.commands.keys())
        missing = [c for c in expected if c not in registered]
        assert not missing, f"Missing commands: {missing}"

    def test_old_groups_removed(self):
        """The pre-existing command groups must no longer be top-level commands."""
        from pgreport.pgreport_cli import cli

        for legacy in ["query", "table", "index", "bloat", "health", "vacuum"]:
            assert legacy not in cli.commands, (
                f"Legacy group '{legacy}' still registered as top-level command"
            )

    def test_json_flag(self):
        from click.testing import CliRunner
        from pgreport.pgreport_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--json", "--help"])
        assert result.exit_code == 0

    def test_database_uri_flag(self):
        from click.testing import CliRunner
        from pgreport.pgreport_cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert "--database-uri" in result.output
