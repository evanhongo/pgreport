"""
SQL Driver for PostgreSQL connections using psycopg connection pool.

Standalone module - no external dependencies required.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

logger = logging.getLogger(__name__)


def obfuscate_password(connection_string: str | None) -> str | None:
    if not connection_string:
        return None
    try:
        parsed = urlparse(connection_string)
        if parsed.password:
            netloc = f"{parsed.username}:****@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return connection_string
    except Exception:
        return "****"


class DbConnPool:
    """Database connection manager using psycopg's connection pool."""

    def __init__(self, connection_url: str | None = None):
        self.connection_url = connection_url
        self.pool: AsyncConnectionPool | None = None
        self._is_valid = False
        self._last_error: str | None = None

    async def connect(self, connection_url: str | None = None) -> AsyncConnectionPool:
        if self.pool and self._is_valid:
            return self.pool

        url = connection_url or self.connection_url
        self.connection_url = url

        if not url:
            self._is_valid = False
            self._last_error = "Database connection URL not provided"
            raise ValueError(self._last_error)

        await self.close()

        try:
            self.pool = AsyncConnectionPool(
                conninfo=url,
                min_size=1,
                max_size=5,
                open=False,
            )
            await self.pool.open()

            async with self.pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT 1")

            self._is_valid = True
            self._last_error = None
            logger.info(f"Connected to database: {obfuscate_password(url)}")
            return self.pool

        except Exception as e:
            self._is_valid = False
            self._last_error = str(e)
            await self.close()
            raise ValueError(f"Connection attempt failed: {obfuscate_password(str(e))}") from e

    async def close(self) -> None:
        if self.pool:
            try:
                await self.pool.close()
            except Exception as e:
                logger.warning(f"Error closing pool: {e}")
            finally:
                self.pool = None
                self._is_valid = False

    @property
    def is_valid(self) -> bool:
        return self._is_valid

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def reconnect(self) -> bool:
        if self._is_valid and self.pool:
            return True
        if not self.connection_url:
            logger.error("Cannot reconnect: no connection URL stored")
            return False
        try:
            await self.connect(self.connection_url)
            return True
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            return False


class SqlDriver:
    """Adapter class that wraps a PostgreSQL connection pool."""

    def __init__(self, pool: DbConnPool):
        self.pool = pool

    async def execute_query(
        self,
        query: str,
        params: list[Any] | None = None,
        force_readonly: bool = True,
    ) -> list[dict[str, Any]] | None:
        if not self.pool.pool or not self.pool.is_valid:
            logger.warning("Pool not connected, attempting to reconnect...")
            if await self.pool.reconnect():
                logger.info("Reconnection successful")
            else:
                raise ValueError(
                    f"Database pool not connected and reconnection failed. "
                    f"Last error: {self.pool.last_error}"
                )

        try:
            async with self.pool.pool.connection() as connection:
                return await self._execute_with_connection(
                    connection, query, params, force_readonly
                )
        except (ConnectionError, OSError, TimeoutError, OperationalError, PoolTimeout) as e:
            self.pool._is_valid = False
            self.pool._last_error = str(e)
            logger.error(f"Connection error, marking pool as invalid: {e}")
            raise
        except Exception as e:
            logger.error(f"Error executing query: {e}")
            raise

    async def _execute_with_connection(
        self, connection, query: str, params: list[Any] | None, force_readonly: bool
    ) -> list[dict[str, Any]] | None:
        transaction_started = False
        try:
            async with connection.cursor(row_factory=dict_row) as cursor:
                if force_readonly:
                    await cursor.execute("BEGIN TRANSACTION READ ONLY")
                    transaction_started = True

                if params:
                    await cursor.execute(query, params)
                else:
                    await cursor.execute(query)

                while cursor.nextset():
                    pass

                if cursor.description is None:
                    if not force_readonly:
                        await cursor.execute("COMMIT")
                    elif transaction_started:
                        await cursor.execute("ROLLBACK")
                        transaction_started = False
                    return None

                rows = await cursor.fetchall()

                if not force_readonly:
                    await cursor.execute("COMMIT")
                elif transaction_started:
                    await cursor.execute("ROLLBACK")
                    transaction_started = False

                return [dict(row) for row in rows]

        except Exception as e:
            if transaction_started:
                try:
                    await connection.rollback()
                except Exception as rollback_error:
                    logger.error(f"Error rolling back transaction: {rollback_error}")
            logger.error(f"Error executing query ({query[:100]}...): {e}")
            raise


async def check_extension_installed(driver: SqlDriver, extension_name: str) -> bool:
    try:
        result = await driver.execute_query(
            "SELECT 1 FROM pg_extension WHERE extname = %s", [extension_name]
        )
        return result is not None and len(result) > 0
    except Exception as e:
        logger.warning("Could not check if extension %s is installed: %s", extension_name, e)
        return False


async def check_extension_available(driver: SqlDriver, extension_name: str) -> bool:
    try:
        result = await driver.execute_query(
            "SELECT 1 FROM pg_available_extensions WHERE name = %s", [extension_name]
        )
        return result is not None and len(result) > 0
    except Exception as e:
        logger.warning("Could not check if extension %s is available: %s", extension_name, e)
        return False


async def get_postgres_version(driver: SqlDriver) -> int:
    try:
        result = await driver.execute_query("SHOW server_version")
        if result:
            version_string = result[0]["server_version"]
            major_version = version_string.split(".")[0]
            return int(major_version)
    except Exception as e:
        logger.warning(f"Could not determine PostgreSQL version: {e}")
    return 0
