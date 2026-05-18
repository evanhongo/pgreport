"""Database connection management for the CLI harness."""

from __future__ import annotations

import asyncio
from typing import Any

from .sql_driver import DbConnPool, SqlDriver, obfuscate_password


class ConnectionManager:
    """Manages the database connection lifecycle for CLI sessions."""

    def __init__(self, database_uri: str | None = None):
        self._uri = database_uri
        self._pool: DbConnPool | None = None
        self._driver: SqlDriver | None = None

    @property
    def uri(self) -> str | None:
        return self._uri

    @property
    def driver(self) -> SqlDriver:
        if self._driver is None:
            raise RuntimeError("Not connected. Call connect() first.")
        return self._driver

    @property
    def is_connected(self) -> bool:
        return self._pool is not None and self._pool.is_valid

    async def connect(self, uri: str | None = None, timeout: float = 10.0) -> None:
        """Establish the database connection."""
        target = uri or self._uri
        if not target:
            raise ValueError(
                "No database URI provided. Pass --database-uri or add a source."
            )
        self._uri = target
        self._pool = DbConnPool(target)
        try:
            await asyncio.wait_for(self._pool.connect(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._pool.close()
            self._pool = None
            raise ValueError(f"Connection timed out after {timeout}s to {self.safe_uri()}")
        self._driver = SqlDriver(self._pool)

    async def disconnect(self) -> None:
        """Close the database connection."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._driver = None

    def safe_uri(self) -> str:
        """Return the connection URI with the password obfuscated."""
        return obfuscate_password(self._uri) or "(not set)"
