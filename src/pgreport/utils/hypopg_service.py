"""
HypoPG Service for managing hypothetical indexes in PostgreSQL.

Standalone module - no external dependencies required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from psycopg import sql

from .sql_driver import (
    SqlDriver,
    check_extension_available,
    check_extension_installed,
    get_postgres_version,
)

logger = logging.getLogger(__name__)


@dataclass
class HypotheticalIndex:
    """Represents a hypothetical index created by HypoPG."""
    indexrelid: int
    index_name: str
    schema_name: str | None = None
    table_name: str | None = None
    am_name: str | None = None
    definition: str | None = None
    estimated_size: int | None = None


@dataclass
class HypoPGStatus:
    """Status of HypoPG extension."""
    is_installed: bool
    is_available: bool
    version: str | None = None
    message: str = ""


class HypoPGService:
    """Service for managing hypothetical indexes with HypoPG."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver
        self._status_cache: HypoPGStatus | None = None

    async def check_status(self, force_refresh: bool = False) -> HypoPGStatus:
        if self._status_cache and not force_refresh:
            return self._status_cache

        status = HypoPGStatus(is_installed=False, is_available=False)

        try:
            is_installed = await check_extension_installed(self.driver, "hypopg")

            if is_installed:
                result = await self.driver.execute_query(
                    "SELECT extversion FROM pg_extension WHERE extname = 'hypopg'"
                )
                if result:
                    status.version = result[0].get("extversion")
                status.is_installed = True
                status.is_available = True
                status.message = f"HypoPG extension (version {status.version}) is installed and ready."
            else:
                is_available = await check_extension_available(self.driver, "hypopg")
                status.is_available = is_available
                if is_available:
                    status.message = (
                        "HypoPG extension is available but not installed.\n"
                        "To install: CREATE EXTENSION hypopg;"
                    )
                else:
                    pg_version = await get_postgres_version(self.driver)
                    status.message = (
                        "HypoPG extension is not available on this server.\n\n"
                        f"To install HypoPG for PostgreSQL {pg_version}:\n"
                        f"- Debian/Ubuntu: sudo apt-get install postgresql-{pg_version}-hypopg\n"
                        f"- RHEL/CentOS: sudo yum install postgresql{pg_version}-hypopg\n"
                        "- MacOS: brew install hypopg\n\n"
                        "After installing the package, run: CREATE EXTENSION hypopg;"
                    )
        except Exception as e:
            logger.error(f"Error checking HypoPG status: {e}")
            status.message = f"Error checking HypoPG status: {e}"

        self._status_cache = status
        return status

    async def ensure_available(self) -> bool:
        status = await self.check_status()
        if not status.is_installed:
            raise RuntimeError(status.message)
        return True

    async def create_index(
        self,
        table: str,
        columns: list[str],
        using: str = "btree",
        schema: str | None = None,
        where: str | None = None,
        include: list[str] | None = None,
        unique: bool = False,
    ) -> HypotheticalIndex:
        await self.ensure_available()

        if schema:
            table_ident = sql.Identifier(schema, table)
        else:
            table_ident = sql.Identifier(table)

        valid_access_methods = {"btree", "hash", "brin", "bloom", "gist", "gin", "spgist"}
        if using.lower() not in valid_access_methods:
            raise ValueError(f"Invalid index access method: {using}")

        columns_ident = sql.SQL(", ").join(sql.Identifier(col) for col in columns)
        unique_clause = sql.SQL("UNIQUE ") if unique else sql.SQL("")
        create_stmt = sql.SQL("CREATE {}INDEX ON {} USING {} ({})").format(
            unique_clause, table_ident, sql.SQL(using), columns_ident
        )

        if include:
            include_ident = sql.SQL(", ").join(sql.Identifier(col) for col in include)
            create_stmt = sql.Composed([create_stmt, sql.SQL(" INCLUDE ("), include_ident, sql.SQL(")")])

        if where:
            create_stmt = sql.Composed([create_stmt, sql.SQL(" WHERE "), sql.SQL(where)])

        create_stmt_str = create_stmt.as_string()

        result = await self.driver.execute_query(
            "SELECT * FROM hypopg_create_index(%s)", [create_stmt_str]
        )

        if not result:
            raise RuntimeError(f"Failed to create hypothetical index: {create_stmt}")

        row = result[0]
        index = HypotheticalIndex(
            indexrelid=row.get("indexrelid"),
            index_name=row.get("indexname"),
            table_name=table,
            schema_name=schema,
            am_name=using,
        )
        index.definition = await self.get_index_definition(index.indexrelid)
        index.estimated_size = await self.get_index_size(index.indexrelid)
        return index

    async def list_indexes(self) -> list[HypotheticalIndex]:
        await self.ensure_available()
        result = await self.driver.execute_query("SELECT * FROM hypopg_list_indexes")
        if not result:
            return []

        indexes = []
        for row in result:
            index = HypotheticalIndex(
                indexrelid=row.get("indexrelid"),
                index_name=row.get("index_name"),
                schema_name=row.get("schema_name"),
                table_name=row.get("table_name"),
                am_name=row.get("am_name"),
            )
            index.definition = await self.get_index_definition(index.indexrelid)
            index.estimated_size = await self.get_index_size(index.indexrelid)
            indexes.append(index)
        return indexes

    async def get_index_definition(self, indexrelid: int) -> str | None:
        try:
            result = await self.driver.execute_query(
                "SELECT hypopg_get_indexdef(%s) as indexdef", [indexrelid]
            )
            if result:
                return result[0].get("indexdef")
        except Exception as e:
            logger.warning(f"Could not get index definition: {e}")
        return None

    async def get_index_size(self, indexrelid: int) -> int | None:
        try:
            result = await self.driver.execute_query(
                "SELECT hypopg_relation_size(%s) as size", [indexrelid]
            )
            if result:
                return result[0].get("size")
        except Exception as e:
            logger.warning(f"Could not get index size: {e}")
        return None

    async def drop_index(self, indexrelid: int) -> bool:
        await self.ensure_available()
        try:
            await self.driver.execute_query("SELECT hypopg_drop_index(%s)", [indexrelid])
            return True
        except Exception as e:
            logger.error(f"Failed to drop hypothetical index: {e}")
            return False

    async def reset(self) -> bool:
        await self.ensure_available()
        try:
            await self.driver.execute_query("SELECT hypopg_reset()")
            return True
        except Exception as e:
            logger.error(f"Failed to reset hypothetical indexes: {e}")
            return False

    async def hide_index(self, indexrelid: int) -> bool:
        await self.ensure_available()
        try:
            result = await self.driver.execute_query(
                "SELECT hypopg_hide_index(%s)", [indexrelid]
            )
            if result:
                return result[0].get("hypopg_hide_index", False)
        except Exception as e:
            logger.error(f"Failed to hide index: {e}")
        return False

    async def unhide_index(self, indexrelid: int) -> bool:
        await self.ensure_available()
        try:
            result = await self.driver.execute_query(
                "SELECT hypopg_unhide_index(%s)", [indexrelid]
            )
            if result:
                return result[0].get("hypopg_unhide_index", False)
        except Exception as e:
            logger.error(f"Failed to unhide index: {e}")
        return False

    async def list_hidden_indexes(self) -> list[dict[str, Any]]:
        await self.ensure_available()
        try:
            result = await self.driver.execute_query("SELECT * FROM hypopg_hidden_indexes")
            if result:
                return result
        except Exception as e:
            logger.warning(f"Could not list hidden indexes: {e}")
        return []

    async def explain_with_hypothetical_index(
        self,
        query: str,
        table: str,
        columns: list[str],
        using: str = "btree",
    ) -> dict[str, Any]:
        await self.ensure_available()

        before_result = await self.driver.execute_query(
            f"EXPLAIN (FORMAT JSON, COSTS TRUE) {query}"
        )
        before_plan = before_result[0] if before_result else {}

        hypo_index = await self.create_index(table, columns, using)

        try:
            after_result = await self.driver.execute_query(
                f"EXPLAIN (FORMAT JSON, COSTS TRUE) {query}"
            )
            after_plan = after_result[0] if after_result else {}

            before_cost = self._extract_total_cost(before_plan)
            after_cost = self._extract_total_cost(after_plan)

            improvement = None
            if before_cost and after_cost and after_cost > 0:
                improvement = ((before_cost - after_cost) / before_cost) * 100

            return {
                "hypothetical_index": {
                    "indexrelid": hypo_index.indexrelid,
                    "name": hypo_index.index_name,
                    "definition": hypo_index.definition,
                    "estimated_size": hypo_index.estimated_size,
                },
                "before": {"plan": before_plan, "total_cost": before_cost},
                "after": {"plan": after_plan, "total_cost": after_cost},
                "improvement_percentage": improvement,
                "would_use_index": self._plan_uses_index(after_plan, hypo_index.index_name),
            }
        finally:
            await self.drop_index(hypo_index.indexrelid)

    def _extract_total_cost(self, plan: dict[str, Any]) -> float | None:
        try:
            if isinstance(plan, dict):
                query_plan = plan.get("QUERY PLAN", plan)
                if isinstance(query_plan, list) and query_plan:
                    first_plan = query_plan[0]
                    if isinstance(first_plan, dict):
                        return first_plan.get("Plan", {}).get("Total Cost")
        except Exception:
            pass
        return None

    def _plan_uses_index(self, plan: dict[str, Any], index_name: str) -> bool:
        try:
            return index_name in str(plan)
        except Exception:
            return False
