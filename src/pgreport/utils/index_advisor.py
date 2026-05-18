"""
Index Advisor service for PostgreSQL index recommendations.

Standalone module - no external dependencies required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

try:
    from pglast import parse_sql
    HAS_PGLAST = True
except ImportError:
    HAS_PGLAST = False

from .hypopg_service import HypoPGService
from .sql_driver import SqlDriver

logger = logging.getLogger(__name__)


@dataclass
class IndexRecommendation:
    """Represents an index recommendation."""
    table: str
    columns: list[str]
    using: str = "btree"
    estimated_size_bytes: int = 0
    estimated_improvement: float | None = None
    reason: str = ""
    create_statement: str = ""

    @property
    def definition(self) -> str:
        if self.create_statement:
            return self.create_statement
        columns_str = ", ".join(self.columns)
        return f"CREATE INDEX ON {self.table} USING {self.using} ({columns_str})"


@dataclass
class WorkloadAnalysisResult:
    """Result of workload analysis."""
    recommendations: list[IndexRecommendation] = field(default_factory=list)
    analyzed_queries: int = 0
    total_improvement: float | None = None
    error: str | None = None


class IndexAdvisor:
    """Service for analyzing queries and recommending indexes."""

    def __init__(self, driver: SqlDriver):
        self.driver = driver
        self.hypopg = HypoPGService(driver)

    async def analyze_query(
        self, query: str, max_recommendations: int = 5
    ) -> WorkloadAnalysisResult:
        result = WorkloadAnalysisResult(analyzed_queries=1)
        try:
            columns_by_table = self._extract_columns_from_query(query)
            if not columns_by_table:
                result.error = "Could not extract columns from query"
                return result

            hypopg_status = await self.hypopg.check_status()
            candidates = self._generate_candidate_indexes(columns_by_table)

            if hypopg_status.is_installed:
                result.recommendations = await self._evaluate_with_hypopg(
                    query, candidates, max_recommendations
                )
            else:
                result.recommendations = candidates[:max_recommendations]
                for rec in result.recommendations:
                    rec.reason = "Recommended based on query structure (HypoPG not available for testing)"

            if result.recommendations:
                improvements = [r.estimated_improvement for r in result.recommendations if r.estimated_improvement]
                if improvements:
                    result.total_improvement = max(improvements)
        except Exception as e:
            logger.error(f"Error analyzing query: {e}")
            result.error = str(e)
        return result

    async def analyze_queries(
        self, queries: list[str], max_recommendations: int = 10
    ) -> WorkloadAnalysisResult:
        result = WorkloadAnalysisResult(analyzed_queries=len(queries))
        try:
            all_columns_by_table: dict[str, set[str]] = {}
            for query in queries:
                columns_by_table = self._extract_columns_from_query(query)
                for table, columns in columns_by_table.items():
                    all_columns_by_table.setdefault(table, set()).update(columns)

            if not all_columns_by_table:
                result.error = "Could not extract columns from any query"
                return result

            candidates = self._generate_candidate_indexes(
                {t: list(c) for t, c in all_columns_by_table.items()}
            )

            hypopg_status = await self.hypopg.check_status()
            if hypopg_status.is_installed:
                scored_candidates = []
                for candidate in candidates:
                    total_improvement = 0
                    queries_improved = 0
                    for query in queries:
                        try:
                            test_result = await self.hypopg.explain_with_hypothetical_index(
                                query, candidate.table, candidate.columns, candidate.using
                            )
                            if test_result.get("would_use_index") and test_result.get("improvement_percentage"):
                                total_improvement += test_result["improvement_percentage"]
                                queries_improved += 1
                        except Exception:
                            continue
                    if queries_improved > 0:
                        candidate.estimated_improvement = total_improvement / queries_improved
                        candidate.reason = f"Improves {queries_improved}/{len(queries)} queries"
                        scored_candidates.append(candidate)

                scored_candidates.sort(key=lambda x: x.estimated_improvement or 0, reverse=True)
                result.recommendations = scored_candidates[:max_recommendations]
            else:
                result.recommendations = candidates[:max_recommendations]
                for rec in result.recommendations:
                    rec.reason = "Recommended based on query structure"
        except Exception as e:
            logger.error(f"Error analyzing queries: {e}")
            result.error = str(e)
        return result

    async def analyze_workload(
        self,
        min_calls: int = 50,
        min_avg_time_ms: float = 5.0,
        limit: int = 100,
        max_recommendations: int = 10,
    ) -> WorkloadAnalysisResult:
        result = WorkloadAnalysisResult()
        try:
            check_result = await self.driver.execute_query(
                "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'"
            )
            if not check_result:
                result.error = (
                    "pg_stat_statements extension is required for workload analysis.\n"
                    "Install with: CREATE EXTENSION pg_stat_statements;"
                )
                return result
        except Exception as e:
            result.error = f"Error checking pg_stat_statements: {e}"
            return result

        try:
            base_query = r"""
                SELECT query, calls, mean_exec_time, total_exec_time
                FROM pg_stat_statements
                WHERE calls >= %s
                  AND mean_exec_time >= %s
                  AND query NOT LIKE '%%pg_catalog%%'
                  AND query NOT LIKE '%%information_schema%%'
                  AND query NOT LIKE '%%pg_toast%%'
                  AND query NOT LIKE '%%pg_%%'
                  AND query NOT LIKE '%%$%%'
                  AND query ~* '^\s*SELECT'
            """
            query = f"{base_query} ORDER BY total_exec_time DESC LIMIT %s"
            queries_result = await self.driver.execute_query(
                query, [min_calls, min_avg_time_ms, limit]
            )
            if not queries_result:
                result.error = "No queries found matching criteria"
                return result

            queries = [row.get("query") for row in queries_result if row.get("query")]
            result.analyzed_queries = len(queries)
            return await self.analyze_queries(queries, max_recommendations)
        except Exception as e:
            logger.error(f"Error analyzing workload: {e}")
            result.error = str(e)
            return result

    def _extract_columns_from_query(self, query: str) -> dict[str, list[str]]:
        if not HAS_PGLAST:
            return {}
        try:
            parsed = parse_sql(query)
            if not parsed:
                return {}
            columns_by_table: dict[str, set[str]] = {}
            table_aliases: dict[str, str] = {}
            for stmt in parsed:
                self._extract_tables_from_node(stmt, table_aliases)
                self._extract_columns_from_node(stmt, columns_by_table, table_aliases)
            return {t: list(c) for t, c in columns_by_table.items() if c}
        except Exception as e:
            logger.warning(f"Could not parse query for column extraction: {e}")
            return {}

    def _extract_tables_from_node(self, node: Any, table_aliases: dict[str, str]) -> None:
        if node is None:
            return
        node_class = type(node).__name__
        if node_class == "RangeVar":
            table_name = getattr(node, "relname", None)
            alias_node = getattr(node, "alias", None)
            if table_name:
                if alias_node:
                    alias_name = getattr(alias_node, "aliasname", None)
                    if alias_name:
                        table_aliases[alias_name] = table_name
                table_aliases[table_name] = table_name
        if hasattr(node, "__dict__"):
            for key, value in node.__dict__.items():
                if key.startswith("_"):
                    continue
                if isinstance(value, (list, tuple)):
                    for item in value:
                        self._extract_tables_from_node(item, table_aliases)
                else:
                    self._extract_tables_from_node(value, table_aliases)

    def _extract_columns_from_node(
        self, node: Any, columns: dict[str, set[str]], table_aliases: dict[str, str]
    ) -> None:
        if node is None:
            return
        node_class = type(node).__name__
        if node_class == "ColumnRef":
            fields = getattr(node, "fields", None)
            if fields:
                field_names = []
                for f in fields:
                    f_class = type(f).__name__
                    if f_class == "String":
                        field_names.append(getattr(f, "sval", ""))
                    elif f_class == "A_Star":
                        return
                if len(field_names) == 2:
                    table_or_alias, col_name = field_names
                    table_name = table_aliases.get(table_or_alias, table_or_alias)
                    if table_name and col_name:
                        columns.setdefault(table_name, set()).add(col_name)
                elif len(field_names) == 1 and table_aliases:
                    col_name = field_names[0]
                    unique_tables = set(table_aliases.values())
                    if len(unique_tables) == 1:
                        table_name = next(iter(unique_tables))
                        columns.setdefault(table_name, set()).add(col_name)
        if hasattr(node, "__dict__"):
            for key, value in node.__dict__.items():
                if key.startswith("_"):
                    continue
                if isinstance(value, (list, tuple)):
                    for item in value:
                        self._extract_columns_from_node(item, columns, table_aliases)
                else:
                    self._extract_columns_from_node(value, columns, table_aliases)

    def _generate_candidate_indexes(
        self, columns_by_table: dict[str, list[str]]
    ) -> list[IndexRecommendation]:
        candidates = []
        for table, columns in columns_by_table.items():
            if not columns:
                continue
            for col in columns:
                candidates.append(IndexRecommendation(
                    table=table, columns=[col],
                    reason="Single column index on frequently used column",
                ))
            if len(columns) > 1:
                candidates.append(IndexRecommendation(
                    table=table, columns=columns[:3],
                    reason="Composite index for multi-column filter",
                ))
        return candidates

    async def _evaluate_with_hypopg(
        self, query: str, candidates: list[IndexRecommendation], max_recommendations: int
    ) -> list[IndexRecommendation]:
        evaluated = []
        await self.hypopg.reset()
        for candidate in candidates:
            try:
                test_result = await self.hypopg.explain_with_hypothetical_index(
                    query, candidate.table, candidate.columns, candidate.using
                )
                if test_result.get("would_use_index"):
                    candidate.estimated_improvement = test_result.get("improvement_percentage")
                    candidate.estimated_size_bytes = test_result.get("hypothetical_index", {}).get("estimated_size", 0)
                    candidate.create_statement = test_result.get("hypothetical_index", {}).get("definition", "")
                    candidate.reason = (
                        f"Estimated {candidate.estimated_improvement:.1f}% improvement"
                        if candidate.estimated_improvement
                        else "Would be used by query"
                    )
                    evaluated.append(candidate)
            except Exception as e:
                logger.warning(f"Error evaluating candidate {candidate}: {e}")
        evaluated.sort(key=lambda x: x.estimated_improvement or 0, reverse=True)
        return evaluated[:max_recommendations]
