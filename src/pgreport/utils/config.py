"""Configuration file management for pgreport CLI.

Manages ~/.config/pgreport/config.toml for database source connections.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


CONFIG_DIR = Path.home() / ".config" / "pgreport"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def _load_config() -> dict:
    """Load config from TOML file, returning empty dict if not found."""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def _save_config(config: dict) -> None:
    """Save config dict as TOML (manual serialization to avoid extra deps)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if "active_source" in config:
        lines.append(f'active_source = "{config["active_source"]}"')
        lines.append("")
    for source in config.get("sources", []):
        lines.append("[[sources]]")
        lines.append(f'name = "{source["name"]}"')
        lines.append(f'dsn = "{source["dsn"]}"')
        lines.append("")
    CONFIG_FILE.write_text("\n".join(lines))


def get_active_dsn() -> str | None:
    """Return the DSN of the active source, or None."""
    config = _load_config()
    active = config.get("active_source")
    if not active:
        return None
    for source in config.get("sources", []):
        if source.get("name") == active:
            return source.get("dsn")
    return None


def add_source(dsn: str, name: str) -> None:
    """Add a new database source to the config."""
    config = _load_config()
    sources = config.get("sources", [])
    for s in sources:
        if s["name"] == name:
            raise ValueError(f"Source '{name}' already exists. Remove it first with: pgreport source rm {name}")
    sources.append({"name": name, "dsn": dsn})
    config["sources"] = sources
    if "active_source" not in config:
        config["active_source"] = name
    _save_config(config)


def remove_source(name: str) -> None:
    """Remove a database source from the config."""
    config = _load_config()
    sources = config.get("sources", [])
    new_sources = [s for s in sources if s["name"] != name]
    if len(new_sources) == len(sources):
        raise ValueError(f"Source '{name}' not found.")
    config["sources"] = new_sources
    if config.get("active_source") == name:
        if new_sources:
            config["active_source"] = new_sources[0]["name"]
        else:
            config.pop("active_source", None)
    _save_config(config)


def set_active_source(name: str) -> None:
    """Set the active source by name."""
    config = _load_config()
    sources = config.get("sources", [])
    if not any(s["name"] == name for s in sources):
        raise ValueError(f"Source '{name}' not found.")
    config["active_source"] = name
    _save_config(config)


def list_sources() -> list[dict]:
    """Return list of sources with active flag."""
    config = _load_config()
    active = config.get("active_source")
    result = []
    for source in config.get("sources", []):
        result.append({
            "name": source["name"],
            "dsn": source["dsn"],
            "active": source["name"] == active,
        })
    return result
