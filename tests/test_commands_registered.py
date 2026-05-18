"""Registry tests for pgreport commands.

Enforces two project-wide invariants:
1. All expected commands are registered on the Click ``cli`` group.
2. No command exposes a positional argument (every parameter must be a
   ``--flag``). The ``source`` group keeps its positional args and is the
   only documented exception.
"""

import click
import pytest

from pgreport.pgreport_cli import cli


# Commands grouped by SKILL.md category.
EXPECTED_COMMANDS = {
    # 1. Core Health & Availability
    "wraparound-risk", "blocking-locks", "deadlock-detection",
    "connection-security-status", "health-check", "index-health", "wait-events",
    # 2. Session & Connection Monitoring
    "long-running-queries", "idle-in-transaction-sessions",
    "long-running-transactions", "long-running-prepared-transactions",
    "connection-usage", "lock-waiters", "active-queries",
    # 3. Performance & Activity
    "cache-hit-rate", "rollback-rate", "top-sql-by-time", "table-hotspots",
    "bgwriter-stats", "temp-file-usage", "io-statistics",
    "pg-progress",
    "wal-statistics", "slru-stats", "user-function-stats",
    "database-conflict-stats", "checkpointer-stats",
    "explain", "table-stats",
    "disk-io-analysis", "recommend-indexes",
    "hypothetical-index",
    # 4. Replication & Archiving
    "replication-slots", "replication-status", "logical-replication-status",
    "wal-archiver-status",
    # 5. Maintenance & Storage
    "autovacuum-status", "table-bloat", "index-bloat", "top-objects-by-size",
    "stale-statistics", "database-sizes",
    "sequence-exhaustion", "freeze-prediction", "list-indexes",
    "pending-vacuum", "vacuum-history",
}


def test_expected_command_count_is_47():
    """Sanity check: this set must remain at 47 entries."""
    assert len(EXPECTED_COMMANDS) == 47


def test_all_commands_registered():
    registered = set(cli.commands.keys())
    missing = EXPECTED_COMMANDS - registered
    assert not missing, f"Missing commands: {sorted(missing)}"


def test_total_top_level_command_count():
    """Expected commands + the ``source`` group sum to 48 top-level entries."""
    assert len(cli.commands) == 48
    assert "source" in cli.commands


def test_no_positional_args_on_commands():
    """Every command must use --flag options; positional args are reserved for `source`."""
    offenders: list[tuple[str, list[str]]] = []
    for name, cmd in cli.commands.items():
        if name == "source":
            continue
        positional = [p.name for p in cmd.params if isinstance(p, click.Argument)]
        if positional:
            offenders.append((name, positional))
    assert not offenders, (
        "Commands must not declare positional args. "
        f"Offenders: {offenders}"
    )


def test_legacy_groups_removed():
    for legacy in ["query", "table", "index", "bloat", "health", "vacuum"]:
        assert legacy not in cli.commands, (
            f"Legacy group '{legacy}' should not be registered as a top-level command"
        )


@pytest.mark.parametrize("cmd_name", sorted(EXPECTED_COMMANDS))
def test_each_command_has_help_text(cmd_name):
    cmd = cli.commands[cmd_name]
    help_text = cmd.help or cmd.short_help or ""
    assert help_text.strip(), f"Command '{cmd_name}' has no help text"
