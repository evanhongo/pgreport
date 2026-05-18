"""E2E subprocess tests for pgreport.

PostgreSQL-diagnostic commands are exposed at the top level; the
``source`` group is reserved for managing database connection aliases.
"""

import os
import shutil
import subprocess
import pytest


class TestCLISubprocess:
    """Tests that run the CLI as a subprocess, verifying the installed entry point."""

    @staticmethod
    def _resolve_cli() -> str:
        """Resolve the CLI command, preferring installed entry point."""
        if os.environ.get("PGREPORT_FORCE_INSTALLED"):
            path = shutil.which("pgreport")
            if path:
                return path
            pytest.skip("pgreport not found in PATH")

        path = shutil.which("pgreport")
        if path:
            return path
        return None

    def _run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        cli_path = self._resolve_cli()
        if cli_path:
            cmd = [cli_path, *args]
        else:
            cmd = ["python", "-m", "pgreport.pgreport_cli", *args]

        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, **env} if env else None,
        )

    def test_help_output(self):
        result = self._run("--help")
        assert result.returncode == 0
        assert "pgreport CLI" in result.stdout

    def test_help_lists_commands(self):
        """A representative subset of commands must show up in --help."""
        result = self._run("--help")
        assert result.returncode == 0
        for cmd in [
            "index-health",
            "long-running-queries",
            "top-sql-by-time",
            "replication-status",
            "freeze-prediction",
            "health-check",
            "source",
        ]:
            assert cmd in result.stdout, f"Missing command in --help: {cmd}"

    def test_old_groups_absent(self):
        """Legacy command groups should no longer be invokable as subcommands."""
        for legacy in ["query", "table", "index", "bloat", "health", "vacuum"]:
            result = self._run(legacy, "--help")
            # Either the command is unrecognized (exit != 0) or its help no longer
            # lists the old child commands like "slow", "stats", "recommend", etc.
            combined = result.stdout + result.stderr
            assert result.returncode != 0 or (
                "slow" not in combined and "recommend" not in combined
            ), f"Legacy group '{legacy}' appears to still be invokable"

    def test_long_running_queries_help(self):
        result = self._run("long-running-queries", "--help")
        assert result.returncode == 0
        assert "--threshold-minutes" in result.stdout

    def test_top_sql_by_time_help(self):
        result = self._run("top-sql-by-time", "--help")
        assert result.returncode == 0
        assert "--limit" in result.stdout
        assert "--order-by" in result.stdout

    def test_explain_help(self):
        result = self._run("explain", "--help")
        assert result.returncode == 0
        assert "--sql" in result.stdout
        assert "--analyze" in result.stdout
        assert "--index" in result.stdout
        assert "--buffers" in result.stdout
        assert "--format" in result.stdout

    def test_table_bloat_help(self):
        result = self._run("table-bloat", "--help")
        assert result.returncode == 0
        assert "--min-bloat" in result.stdout

    def test_hypothetical_index_help(self):
        result = self._run("hypothetical-index", "--help")
        assert result.returncode == 0
        assert "--unique" in result.stdout
        assert "--action" in result.stdout

    def test_temp_file_usage_help(self):
        result = self._run("temp-file-usage", "--help")
        assert result.returncode == 0
        assert "--threshold-gb" in result.stdout
        assert "--limit" in result.stdout

    def test_source_help(self):
        result = self._run("source", "--help")
        assert result.returncode == 0
        for cmd in ["add", "rm", "ls", "use"]:
            assert cmd in result.stdout

    def test_invalid_uri_error(self):
        result = self._run(
            "--database-uri",
            "postgresql://bad:bad@127.0.0.1:19999/nope?connect_timeout=2",
            "health-check",
        )
        assert result.returncode != 0 or "Error" in result.stderr

    def test_json_flag_with_help(self):
        result = self._run("--json", "--help")
        assert result.returncode == 0
        assert "pgreport CLI" in result.stdout

    def test_no_uri_error(self, tmp_path):
        # Override HOME to prevent reading real config sources
        result = self._run("health-check", env={"HOME": str(tmp_path)})
        combined = result.stdout + result.stderr
        assert result.returncode != 0 or "Error" in combined or "No database URI" in combined

    def test_source_add_and_rm(self):
        result = self._run("source", "add", "postgresql://test:test@localhost/testdb", "e2e_test_src")
        assert result.returncode == 0
        assert "Added" in result.stdout or "added" in result.stdout.lower()
        result = self._run("source", "rm", "e2e_test_src")
        assert result.returncode == 0
        assert "Removed" in result.stdout or "removed" in result.stdout.lower()

    def test_source_use_nonexistent(self):
        result = self._run("source", "use", "nonexistent_alias_xyz_12345")
        combined = result.stdout + result.stderr
        assert result.returncode != 0 or "Error" in combined or "not found" in combined.lower()
