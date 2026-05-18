"""Tests for config module and source CLI commands."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import pgreport.utils.config as config_module
from pgreport.pgreport_cli import cli


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect config to a temp directory so tests never touch real config."""
    config_dir = tmp_path / ".config" / "pgreport"
    config_file = config_dir / "config.toml"
    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)
    return config_dir, config_file


class TestConfigFunctions:
    """Tests for all config utility functions."""

    def test_load_empty_config(self):
        result = config_module._load_config()
        assert result == {}

    def test_add_and_load_source(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        config = config_module._load_config()
        assert len(config["sources"]) == 1
        assert config["sources"][0]["name"] == "dev"
        assert config["sources"][0]["dsn"] == "postgres://localhost/db1"

    def test_add_duplicate_raises(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        with pytest.raises(ValueError, match="already exists"):
            config_module.add_source("postgres://localhost/db2", "dev")

    def test_get_active_dsn_none(self):
        assert config_module.get_active_dsn() is None

    def test_get_active_dsn_with_source(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        assert config_module.get_active_dsn() == "postgres://localhost/db1"

    def test_remove_source(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        config_module.add_source("postgres://localhost/db2", "staging")
        config_module.remove_source("dev")
        sources = config_module.list_sources()
        assert len(sources) == 1
        assert sources[0]["name"] == "staging"

    def test_remove_nonexistent_raises(self):
        with pytest.raises(ValueError, match="not found"):
            config_module.remove_source("nope")

    def test_set_active_source(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        config_module.add_source("postgres://localhost/db2", "staging")
        config_module.set_active_source("staging")
        assert config_module.get_active_dsn() == "postgres://localhost/db2"

    def test_set_active_nonexistent_raises(self):
        with pytest.raises(ValueError, match="not found"):
            config_module.set_active_source("nope")

    def test_list_sources_empty(self):
        assert config_module.list_sources() == []

    def test_list_sources_with_data(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        config_module.add_source("postgres://localhost/db2", "staging")
        config_module.set_active_source("staging")
        sources = config_module.list_sources()
        assert len(sources) == 2
        dev = next(s for s in sources if s["name"] == "dev")
        staging = next(s for s in sources if s["name"] == "staging")
        assert dev["active"] is False
        assert dev["dsn"] == "postgres://localhost/db1"
        assert staging["active"] is True
        assert staging["dsn"] == "postgres://localhost/db2"

    def test_add_first_source_sets_active(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        config = config_module._load_config()
        assert config["active_source"] == "dev"

    def test_remove_active_switches_to_next(self):
        config_module.add_source("postgres://localhost/db1", "dev")
        config_module.add_source("postgres://localhost/db2", "staging")
        # dev is active (first added)
        assert config_module.get_active_dsn() == "postgres://localhost/db1"
        config_module.remove_source("dev")
        # should switch to staging
        assert config_module.get_active_dsn() == "postgres://localhost/db2"


class TestSourceCLI:
    """Tests for CLI source subcommands via CliRunner."""

    def test_source_add_success(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "add", "postgres://localhost/db1", "dev"])
        assert result.exit_code == 0
        assert "Added source 'dev'" in result.output

    def test_source_add_duplicate_error(self):
        runner = CliRunner()
        runner.invoke(cli, ["source", "add", "postgres://localhost/db1", "dev"])
        result = runner.invoke(cli, ["source", "add", "postgres://localhost/db2", "dev"])
        assert result.exit_code != 0
        assert "already exists" in result.output + (result.stderr or "")

    def test_source_rm_success(self):
        runner = CliRunner()
        runner.invoke(cli, ["source", "add", "postgres://localhost/db1", "dev"])
        result = runner.invoke(cli, ["source", "rm", "dev"])
        assert result.exit_code == 0
        assert "Removed source 'dev'" in result.output

    def test_source_rm_nonexistent_error(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "rm", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output + (result.stderr or "")

    def test_source_use_success(self):
        runner = CliRunner()
        runner.invoke(cli, ["source", "add", "postgres://localhost/db1", "dev"])
        runner.invoke(cli, ["source", "add", "postgres://localhost/db2", "staging"])
        result = runner.invoke(cli, ["source", "use", "staging"])
        assert result.exit_code == 0
        assert "Active source set to 'staging'" in result.output

    def test_source_use_nonexistent_error(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "use", "nope"])
        assert result.exit_code != 0
        assert "not found" in result.output + (result.stderr or "")

    def test_source_ls_empty(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "ls"], input="q\n")
        assert result.exit_code == 0
        assert "No sources configured" in result.output

    def test_source_ls_with_sources(self):
        runner = CliRunner()
        runner.invoke(cli, ["source", "add", "postgres://localhost/db1", "dev"])
        runner.invoke(cli, ["source", "add", "postgres://localhost/db2", "staging"])
        result = runner.invoke(cli, ["source", "ls"], input="q\n")
        assert result.exit_code == 0
        assert "dev" in result.output
        assert "staging" in result.output
        assert "(active)" in result.output
