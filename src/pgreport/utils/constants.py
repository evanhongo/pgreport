"""Shared constants for pgreport CLI harness."""

# System schemas excluded from all user-facing queries.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

# SQL-ready literal for use in WHERE clauses: column NOT IN (...)
SYSTEM_SCHEMAS_SQL = "('pg_catalog', 'information_schema', 'pg_toast')"

# Default schema used when none is specified.
DEFAULT_SCHEMA = "public"

BYTES_PER_GB = 1024 * 1024 * 1024


def gb_to_bytes(gb: float) -> int:
    """Convert gigabytes to bytes."""
    return int(gb * BYTES_PER_GB)


def sql_hit_ratio(hits: str, reads: str, alias: str = "hit_ratio") -> str:
    """SQL CASE expression for cache hit ratio: 100 * hits / (hits + reads).

    Works with plain columns (e.g. 'heap_blks_hit') or aggregates (e.g. 'sum(heap_blks_hit)').
    """
    return (
        f"CASE WHEN {hits} + {reads} > 0 "
        f"THEN ROUND(100.0 * {hits} / (({hits}) + ({reads})), 2) "
        f"ELSE 100 END as {alias}"
    )


def sql_pct_ratio(numerator: str, denominator: str, alias: str) -> str:
    """SQL CASE expression for a percentage: 100 * numerator / denominator, defaulting to 0."""
    return (
        f"CASE WHEN {denominator} > 0 "
        f"THEN ROUND(100.0 * {numerator} / ({denominator}), 2) "
        f"ELSE 0 END as {alias}"
    )
