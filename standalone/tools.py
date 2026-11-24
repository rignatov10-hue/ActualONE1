"""The five fixed analysis tools, pure pandas, no AI and no network access.

The model can only call these; it can never run arbitrary code against the data.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import pandas as pd


class ToolError(Exception):
    """A model-readable tool failure."""


_MAX_BYTES = 150 * 1024 * 1024
_MAX_GROUPS = 100

_AGG_FUNCS = ("count", "sum", "mean", "median", "min", "max", "std", "nunique")

FilterOperation = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "contains",
    "starts_with",
    "ends_with",
    "in",
    "not_in",
    "is_null",
    "not_null",
]

_DRIVE_ID_PATTERNS = (
    re.compile(r"/(?:file|document|spreadsheets|presentation|drawings)/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)


@dataclass
class Dataset:
    source: str
    title: str
    load_time: str


def extract_drive_id(link: str) -> str:
    for pattern in _DRIVE_ID_PATTERNS:
        match = pattern.search(link)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", link.strip()):
        return link.strip()
    raise ToolError(
        "Could not read a Google Drive file ID from the source. Pass a normal Drive/Docs link or the raw file ID."
    )


def parse_tabular(raw: bytes, title: str, source_mime: str) -> pd.DataFrame:
    try:
        if source_mime == "xlsx":
            return pd.read_excel(io.BytesIO(raw))
        try:
            return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="latin-1")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(
            f"Downloaded '{title}' but could not parse it as tabular data ({type(exc).__name__})."
        ) from exc


def coerce_value(series: pd.Series, text: str, column: str) -> Any:
    if pd.api.types.is_bool_dtype(series):
        lowered = text.strip().lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
        raise ToolError(f"Column '{column}' holds true/false; got {text!r}.")
    if pd.api.types.is_numeric_dtype(series):
        try:
            return float(text)
        except ValueError:
            raise ToolError(f"Column '{column}' is numeric; {text!r} is not a number.")
    if pd.api.types.is_datetime64_any_dtype(series):
        parsed = pd.to_datetime(text.strip(), errors="coerce")
        if parsed is pd.NaT:
            raise ToolError(f"Column '{column}' holds dates; could not read {text!r} as a date.")
        return parsed
    return text


def text_lower(series: pd.Series) -> pd.Series:
    return series.astype("string").str.lower()


def apply_operation(df: pd.DataFrame, column: str, operation: str, value: Any, second_value: Any) -> pd.Series:
    if column not in df.columns:
        available = ", ".join(map(str, df.columns))
        raise ToolError(f"No column named '{column}'. Available columns: {available}.")

    series = df[column]

    if operation in {"is_null", "not_null"}:
        return series.isna() if operation == "is_null" else series.notna()

    if operation == "between":
        if value is None or second_value is None:
            raise ToolError("Operation 'between' needs both value and second_value.")
        low = coerce_value(series, str(value), column)
        high = coerce_value(series, str(second_value), column)
        if pd.api.types.is_numeric_dtype(series):
            return series.between(low, high)
        return (series >= low) & (series <= high)

    if value is None:
        raise ToolError(f"Operation '{operation}' needs a value for column '{column}'.")
    text = str(value)

    if operation in {"gt", "gte", "lt", "lte"}:
        target = coerce_value(series, text, column)
        comparisons = {
            "gt": series > target,
            "gte": series >= target,
            "lt": series < target,
            "lte": series <= target,
        }
        return comparisons[operation]

    if operation in {"eq", "ne"}:
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            predicate = text_lower(series) == text.strip().lower()
        else:
            target = coerce_value(series, text, column)
            predicate = series == target
        return predicate if operation == "eq" else ~predicate.fillna(False)

    if operation in {"contains", "starts_with", "ends_with"}:
        as_text = series.astype("string")
        if operation == "contains":
            return as_text.str.contains(text, case=False, na=False)
        if operation == "starts_with":
            return text_lower(series).str.startswith(text.lower(), na=False)
        return text_lower(series).str.endswith(text.lower(), na=False)

    if operation in {"in", "not_in"}:
        items = [item.strip().lower() for item in text.split(",") if item.strip()]
        if not items:
            raise ToolError(f"Operation '{operation}' needs at least one comma-separated item in value.")
        in_mask = text_lower(series).isin(items)
        return in_mask if operation == "in" else ~in_mask.fillna(False)

    raise ToolError(
        f"Unknown operation '{operation}'. Use one of: eq, ne, gt, gte, lt, lte, between, "
        "contains, starts_with, ends_with, in, not_in, is_null, not_null."
    )


def require_columns(df: pd.DataFrame, columns: list[str], purpose: str) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        available = ", ".join(map(str, df.columns))
        raise ToolError(
            f"No column named {missing} to use for {purpose}. Available columns: {available}."
        )


def records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    view = df if limit is None else df.head(limit)
    if view.empty:
        return []
    return json.loads(view.to_json(orient="records", date_format="iso"))


def _null_result(conv: Any) -> ToolError:
    return ToolError(
        "No dataset is loaded in this conversation yet. Ask the user for their data source "
        "(a Google Drive link, an https URL to a CSV, or a local file path), then call "
        "load_csv_from_drive."
    )


def load_csv_from_drive(conv: Any, source: str) -> dict[str, Any]:
    """Load a tabular file (Drive link, https URL, or local path) into memory."""
    from . import drive

    dataset, df = drive.fetch_dataset(source)
    conv.set_dataset(dataset, df)
    return {
        "status": "loaded",
        "file_title": dataset.title,
        "source": dataset.source,
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "column_names": [str(c) for c in df.columns],
        "column_types": {str(c): str(t) for c, t in df.dtypes.items()},
    }


def describe_columns(
    conv: Any,
    columns: list[str] | None = None,
    sample_values: int = 5,
) -> dict[str, Any]:
    """Describe the columns of the loaded file: types, nulls, stats or top values."""
    if conv.dataset is None:
        raise _null_result(conv)
    sample_values = max(1, min(10, int(sample_values)))
    view = conv.active
    if columns:
        require_columns(view, columns, "description")
        view = view.loc[:, [c for c in columns if c in view.columns]]
    result: dict[str, Any] = {
        "file_title": conv.dataset.title,
        "scope": conv.active_description,
        "rows": int(view.shape[0]),
        "evaluated_columns": {},
    }
    for column in view.columns:
        series = view[column]
        entry: dict[str, Any] = {
            "dtype": str(series.dtype),
            "non_null": int(series.notna().sum()),
            "nulls": int(series.isna().sum()),
            "distinct": int(series.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(series) and series.notna().any():
            entry["stats"] = json.loads(series.describe(percentiles=[0.25, 0.5, 0.75]).to_json())
        elif pd.api.types.is_datetime64_any_dtype(series) and series.notna().any():
            entry["stats"] = {"min": str(series.min()), "max": str(series.max())}
        else:
            counts = series.value_counts(dropna=True).head(sample_values)
            entry["most_frequent"] = {str(key): int(value) for key, value in counts.items()}
        result["evaluated_columns"][str(column)] = entry
    return result


def filter_rows(
    conv: Any,
    column: str,
    operation: str,
    value: str | None = None,
    second_value: str | None = None,
    set_as_active: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Filter rows; with set_as_active narrow every later question to the matches."""
    if conv.dataset is None:
        raise _null_result(conv)
    limit = max(1, min(_MAX_GROUPS, int(limit)))
    scope_rows = int(conv.active.shape[0])
    description_before = conv.active_description
    mask = apply_operation(conv.active, column, operation, value, second_value)
    matched = conv.active[mask]
    if set_as_active:
        conv.set_scope(
            matched,
            f"the narrowed selection: rows of '{conv.dataset.title}' where {column} {operation}"
            + (f" {value}" if value is not None else ""),
        )
    return {
        "file_title": conv.dataset.title,
        "scope_before": description_before,
        "scope_rows_before": scope_rows,
        "matching_rows": int(matched.shape[0]),
        "evaluated_columns": {},
        "sample_rows": records(matched, limit),
        "scope_is_narrowed": bool(set_as_active),
        "scope_after": conv.active_description,
    }


def group_by_aggregate(
    conv: Any,
    group_by: str,
    metrics: dict[str, str],
    order_by: str | None = None,
    ascending: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Group the current scope and compute aggregates, e.g. {"region": "sum", "*": "count"}."""
    if conv.dataset is None:
        raise _null_result(conv)
    limit = max(1, min(_MAX_GROUPS, int(limit)))
    keys = [key.strip() for key in str(group_by).split(",") if key.strip()]
    if not keys:
        raise ToolError("Pass at least one column in group_by.")
    require_columns(conv.active, keys, "grouping")

    aggregations: dict[str, str] = {}
    row_count = False
    for metric_column, func in metrics.items():
        func_name = str(func).lower().strip()
        if func_name not in _AGG_FUNCS:
            raise ToolError(
                f"Unsupported aggregate '{func}'. Use one of: {', '.join(_AGG_FUNCS)}."
            )
        if metric_column == "*":
            row_count = True
            continue
        require_columns(conv.active, [metric_column], "aggregation")
        if metric_column in aggregations and aggregations[metric_column] != func_name:
            raise ToolError(
                f"Conflicting aggregates for '{metric_column}': '{func_name}' vs "
                f"'{aggregations[metric_column]}'."
            )
        aggregations[metric_column] = func_name
    if not aggregations and not row_count:
        row_count = True

    uses: dict[str, int] = {}
    for metric_column in aggregations:
        uses[metric_column] = uses.get(metric_column, 0) + 1
    named: dict[str, pd.NamedAgg] = {}
    for metric_column, func_name in aggregations.items():
        if metric_column in keys or uses.get(metric_column, 0) > 1:
            out_name = f"{metric_column}_{func_name}"
        else:
            out_name = metric_column
        while out_name in keys or out_name in named or out_name == "row_count":
            out_name = f"{out_name}_{func_name}"
        named[out_name] = pd.NamedAgg(column=metric_column, aggfunc=func_name)
    if row_count:
        if "row_count" in keys or "row_count" in named:
            raise ToolError("Rename a column to something else; 'row_count' is reserved.")
        named["row_count"] = pd.NamedAgg(column=keys[0], aggfunc="count")

    result = conv.active.groupby(keys, dropna=False).agg(**named).reset_index()
    if order_by:
        require_columns(result, [order_by], "ordering")
        result = result.sort_values(order_by, ascending=ascending)
    return {
        "file_title": conv.dataset.title,
        "scope": conv.active_description,
        "total_groups": int(len(result)),
        "aggregate_columns": [str(c) for c in result.columns],
        "table": records(result, limit),
    }


def reset_scope(conv: Any) -> dict[str, Any]:
    """Restore the full dataset after a narrowed scope."""
    if conv.dataset is None:
        raise _null_result(conv)
    conv.clear_scope()
    return {
        "file_title": conv.dataset.title,
        "scope": "all rows of the loaded file",
        "rows": int(conv.full.shape[0]),
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "load_csv_from_drive",
        "description": (
            "Load a tabular file into memory: a Google Drive link or file ID (CSV, Excel/ODS,"
            " Google Sheet; the sheet's first tab is read), an https URL to a CSV, or a local"
            " file path. Returns the title, shape and column list with types."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Drive link or file ID, https URL, or local path.",
                }
            },
            "required": ["source"],
        },
    },
    {
        "name": "describe_columns",
        "description": (
            "Describe the loaded file's columns: dtype, non-null and null counts, distinct"
            " values, and statistics for numeric columns or the most frequent values otherwise."
            " Call this before any other analysis so answers use real column names."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subset of columns to describe; all when omitted.",
                },
                "sample_values": {
                    "type": "integer",
                    "description": "Most frequent values to report per column (1-10).",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
        },
    },
    {
        "name": "filter_rows",
        "description": (
            "Filter rows and get the match count plus up to `limit` sample rows. With"
            " set_as_active=true every later question runs on only the matching rows until"
            " reset_scope is called. Filtering never changes the file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "column": {"type": "string", "description": "Column to filter on."},
                "operation": {
                    "type": "string",
                    "enum": list(FilterOperation.__args__),  # type: ignore[attr-defined]
                    "description": "Comparison to apply.",
                },
                "value": {
                    "type": "string",
                    "description": "Comparison value; comma-separated items for in/not_in.",
                },
                "second_value": {"type": "string", "description": "Upper bound for 'between'."},
                "set_as_active": {
                    "type": "boolean",
                    "description": "Narrow every later question to the matching rows.",
                },
                "limit": {"type": "integer", "description": "Sample rows to return.", "minimum": 1},
            },
            "required": ["column", "operation"],
        },
    },
    {
        "name": "group_by_aggregate",
        "description": (
            "Group the current scope by one or more columns and compute aggregates, e.g."
            ' {"revenue": "sum", "*": "count"}. Aggregates: count, sum, mean, median, min, max,'
            " std, nunique; \"*\" counts rows. Use order_by to rank groups."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_by": {
                    "type": "string",
                    "description": "Column, or comma-separated columns, to group by.",
                },
                "metrics": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": 'Mapping column -> aggregate; use "*" to count rows.',
                },
                "order_by": {"type": "string", "description": "Aggregate column to sort by."},
                "ascending": {"type": "boolean", "description": "Sort ascending (default) or descending."},
                "limit": {"type": "integer", "description": "Groups to return.", "minimum": 1},
            },
            "required": ["group_by", "metrics"],
        },
    },
    {
        "name": "reset_scope",
        "description": "Cancel filter_rows narrowing so later questions look at every row again.",
        "parameters": {"type": "object", "properties": {}},
    },
]

TOOL_IMPLS = {
    "load_csv_from_drive": load_csv_from_drive,
    "describe_columns": describe_columns,
    "filter_rows": filter_rows,
    "group_by_aggregate": group_by_aggregate,
    "reset_scope": reset_scope,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
