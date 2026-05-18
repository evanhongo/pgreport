"""Output formatting utilities for pgreport CLI."""

from __future__ import annotations

import json
from typing import Any


def format_json(data: Any, compact: bool = False) -> str:
    """Format data as JSON string."""
    if compact:
        return json.dumps(data, default=str, ensure_ascii=False)
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)


def format_size(size_bytes: int | float | None) -> str:
    """Format bytes into human-readable size."""
    if size_bytes is None or size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_duration(seconds: int | float | None) -> str:
    """Format seconds into human-readable duration."""
    if seconds is None or seconds == 0:
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"


def extract_explain_plan(result: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Extract the execution plan dict from an EXPLAIN (FORMAT JSON) result.

    Handles the various shapes returned by psycopg (string, nested list, dict).
    Used by both QueryAnalyzer and IndexManager to avoid duplicating this logic.
    """
    if not result:
        return {}

    plan_data = result[0].get("QUERY PLAN", result[0])
    if isinstance(plan_data, str):
        plan_data = json.loads(plan_data)

    if isinstance(plan_data, list) and len(plan_data) > 0:
        return plan_data[0]
    return plan_data


def format_table(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    """Format a list of dicts as an aligned text table."""
    if not rows:
        return "(no data)"

    if columns is None:
        columns = list(rows[0].keys())

    # Calculate column widths
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            val = str(row.get(col, ""))
            widths[col] = max(widths[col], len(val))

    # Header
    header = " | ".join(col.ljust(widths[col]) for col in columns)
    separator = "-+-".join("-" * widths[col] for col in columns)

    # Rows
    lines = [header, separator]
    for row in rows:
        line = " | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns)
        lines.append(line)

    return "\n".join(lines)
