"""Result type and number formatting shared by every analysis module."""

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class AnalysisResult:
    key: str
    title: str
    table: pd.DataFrame  # already formatted for display
    facts: list[str] = field(default_factory=list)
    note: str = ""  # definition of the metric, shown under the table
    extra_tables: list[tuple[str, pd.DataFrame]] = field(default_factory=list)


def pct(x, digits=2) -> str:
    return "-" if pd.isna(x) else f"{x * 100:.{digits}f}%"


def pp(x) -> str:
    """Percentage-point difference."""
    return f"{x * 100:+.2f}个百分点"


def money(x) -> str:
    if pd.isna(x):
        return "-"
    if abs(x) >= 1e4:
        return f"{x / 1e4:,.1f}万"
    return f"{x:,.0f}"


def _q(conn, sql, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)
