"""Conversational data analyst agent for CSV files in Google Drive.

The agent mounts the workspace's Google Drive connector and exposes a fixed,
small set of analysis tools implemented with pandas. There is no sandbox, no
shell and no file tools: the model can only load a Drive file, describe its
columns, filter rows and compute group-by aggregates - no arbitrary code can
run against the data.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import Field

from mistralai import agents

_DRIVE_CONNECTOR = "google_drive_mcp"
_MAX_BYTES = 150 * 1024 * 1024
_MAX_GROUPS = 100

_DRIVE_ID_PATTERNS = (
    re.compile(r"/(?:file|document|spreadsheets|presentation|drawings)/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
)

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

_CACHE_DIR = Path(os.environ.get("DRIVE_ANALYST_CACHE_DIR", "/tmp/drive_analyst"))


@dataclass
class Dataset:
    file_id: str
    title: str
    load_time: str


@dataclass
class SessionState:
    dataset: Dataset
    full: pd.DataFrame
    active: pd.DataFrame
    active_description: str = "all rows of the loaded file"
    history: list[dict[str, Any]] = field(default_factory=list)


_sessions: dict[str, SessionState] = {}


def _safe_session_id(ctx: agents.ToolCallbackContext | None) -> str:
    session_id = ctx.session.id if ctx is not None else "local"
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:128]


def _cache_paths(session_id: str) -> tuple[Path, Path]:
    return _CACHE_DIR / f"{session_id}.parquet", _CACHE_DIR / f"{session_id}.meta.json"


def _persist(state: SessionState, session_id: str) -> None:
    try:
        parquet_path, meta_path = _cache_paths(session_id)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        state.full.to_parquet(parquet_path)
        meta_path.write_text(
            json.dumps(
                {
                    "file_id": state.dataset.file_id,
                    "title": state.dataset.title,
                    "load_time": state.dataset.load_time,
                    "full_rows": int(state.full.shape[0]),
                    "scoped": state.active_description != "all rows of the loaded file",
                },
                ensure_ascii=False,
            )
        )
    except OSError:
        pass


def _persist_scope(state: SessionState, session_id: str) -> None:
    try:
        parquet_path, _ = _cache_paths(session_id)
        scoped_path = parquet_path.with_suffix(".scope.parquet")
        if state.active_description != "all rows of the loaded file":
            state.active.to_parquet(scoped_path)
        elif scoped_path.exists():
            scoped_path.unlink()
        _persist(state, session_id)
    except OSError:
        pass


def _rehydrate_from_cache(session_id: str) -> SessionState | None:
    try:
        parquet_path, meta_path = _cache_paths(session_id)
        if not parquet_path.exists() or not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text())
        full = pd.read_parquet(parquet_path)
        scoped = meta.get("scoped", False)
        scoped_path = parquet_path.with_suffix(".scope.parquet")
        if scoped and scoped_path.exists():
            active = pd.read_parquet(scoped_path)
        else:
            scoped = False
            active = full
        dataset = Dataset(
            file_id=meta["file_id"],
            title=meta["title"],
            load_time=meta["load_time"],
        )
        active_description = (
            f"the narrowed selection held over from earlier ({meta['title']})"
            if scoped
            else "all rows of the loaded file"
        )
        return SessionState(
            dataset=dataset, full=full, active=active, active_description=active_description
        )
    except Exception:
        return None


def _get_state(ctx: agents.ToolCallbackContext | None) -> SessionState:
    raw_session_id = str(ctx.session.id) if ctx is not None else "local"
    state = _sessions.get(raw_session_id)
    if state is None:
        state = _rehydrate_from_cache(_safe_session_id(ctx))
        if state is not None:
            _sessions[raw_session_id] = state
    if state is None:
        raise agents.ToolError(
            "No dataset is loaded in this conversation yet. Ask the user for the Google "
            "Drive link of their CSV, then call load_csv_from_drive."
        )
    return state


# ---------------------------------------------------------------------------
# Google Drive access through the workspace connector
# ---------------------------------------------------------------------------


def _build_client():
    from mistralai.client import Mistral
    from mistralai.workflows.client import get_mistral_client

    try:
        from mistralai.workflows.core.auth import RunAs

        return get_mistral_client(run_as=RunAs.AUTO)
    except Exception:
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise agents.ToolError(
                "Could not authenticate to Google Drive. In a managed deployment this "
                "should never happen; locally, set MISTRAL_API_KEY."
            )
        return Mistral(api_key=api_key)


async def _drive_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    client = _build_client()
    try:
        result = await client.beta.connectors.call_tool_async(
            connector_id_or_name=_DRIVE_CONNECTOR,
            tool_name=tool_name,
            arguments=arguments,
        )
    except agents.ToolError:
        raise
    except Exception as exc:
        raise agents.ToolError(
            f"Google Drive call '{tool_name}' failed: {type(exc).__name__}. Ask the user to "
            "check their Google Drive connection and the file link."
        ) from exc

    if result.metadata and result.metadata.mcp_meta and result.metadata.mcp_meta.is_error:
        raise agents.ToolError(
            f"Google Drive call '{tool_name}' returned an error. Check the file link and "
            "that the user has access with their Google Drive connection."
        )

    for block in result.content:
        dumped = block.model_dump(exclude_none=True)
        kind = dumped.get("type")
        if kind == "text":
            try:
                data = json.loads(dumped.get("text", ""))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        elif kind == "resource":
            resource = dumped.get("resource") or {}
            if resource.get("text"):
                try:
                    data = json.loads(resource["text"])
                except json.JSONDecodeError:
                    return {"file_content": resource["text"]}
                if isinstance(data, dict):
                    return data
            if resource.get("blob"):
                return {"content": resource["blob"]}
    return {}


def _parse_drive_link(link: str) -> str:
    text = str(link).strip()
    for pattern in _DRIVE_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", text):
        return text
    raise agents.ToolError(
        f"Could not read a Google Drive file ID out of {text[:200]!r}. Ask the user for the "
        "file's share link (https://drive.google.com/file/d/... or https://docs.google.com/spreadsheets/d/...)."
    )


def _decode_content(b64: str) -> bytes:
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception as exc:
        raise agents.ToolError("Google Drive returned unreadable file content.") from exc
    if len(raw) > _MAX_BYTES:
        raise agents.ToolError(
            f"The file is larger than the {_MAX_BYTES // (1024 * 1024)} MB this analyst supports."
        )
    return raw


def _parse_tabular(raw: bytes, title: str, source_mime: str) -> pd.DataFrame:
    try:
        if source_mime == "xlsx":
            return pd.read_excel(io.BytesIO(raw))
        try:
            return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding="latin-1")
    except agents.ToolError:
        raise
    except Exception as exc:
        raise agents.ToolError(
            f"Downloaded '{title}' but could not parse it as tabular data ({type(exc).__name__})."
        ) from exc


async def _fetch_dataset(file_id: str) -> tuple[Dataset, pd.DataFrame]:
    metadata = await _drive_tool("get_file_metadata", {"file_id": file_id})
    title = str(metadata.get("title") or file_id)
    mime = str(metadata.get("mime_type") or "")

    if mime == "application/vnd.google-apps.spreadsheet":
        download = await _drive_tool(
            "download_file_content",
            {"file_id": file_id, "export_mime_type": "text/csv"},
        )
        source_mime = "text/csv"
    elif mime in {"text/csv", "application/vnd.ms-excel", "text/plain", ""}:
        download = await _drive_tool("download_file_content", {"file_id": file_id})
        source_mime = "text/csv"
    elif mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    }:
        download = await _drive_tool("download_file_content", {"file_id": file_id})
        source_mime = "xlsx"
    else:
        raise agents.ToolError(
            f"'{title}' has MIME type '{mime}'. This analyst reads CSV files, Excel/ODS "
            "workbooks and Google Sheets. Ask the user for one of those."
        )

    b64 = download.get("content")
    if not b64 or not isinstance(b64, str):
        raise agents.ToolError(
            f"Google Drive returned no content for '{title}'. The file may be empty."
        )
    raw = _decode_content(b64)
    df = _parse_tabular(raw, title, source_mime)
    if df.shape[1] == 0:
        raise agents.ToolError(f"'{title}' appears to be empty; there is nothing to analyze.")

    dataset = Dataset(
        file_id=file_id,
        title=title,
        load_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return dataset, df


# ---------------------------------------------------------------------------
# Helpers for filters and values
# ---------------------------------------------------------------------------


def _coerce_value(series: pd.Series, text: str, column: str) -> Any:
    if pd.api.types.is_bool_dtype(series):
        lowered = text.strip().lower()
        if lowered in {"true", "1"}:
            return True
        if lowered in {"false", "0"}:
            return False
        raise agents.ToolError(f"Column '{column}' holds true/false; got {text!r}.")
    if pd.api.types.is_numeric_dtype(series):
        try:
            return float(text)
        except ValueError:
            raise agents.ToolError(f"Column '{column}' is numeric; {text!r} is not a number.")
    if pd.api.types.is_datetime64_any_dtype(series):
        parsed = pd.to_datetime(text.strip(), errors="coerce")
        if parsed is pd.NaT:
            raise agents.ToolError(f"Column '{column}' holds dates; could not read {text!r} as a date.")
        return parsed
    return text


def _text_lower(series: pd.Series) -> pd.Series:
    return series.astype("string").str.lower()


def _apply_operation(df: pd.DataFrame, column: str, operation: str, value: Any, second_value: Any) -> pd.Series:
    if column not in df.columns:
        available = ", ".join(map(str, df.columns))
        raise agents.ToolError(f"No column named '{column}'. Available columns: {available}.")

    series = df[column]

    if operation in {"is_null", "not_null"}:
        return series.isna() if operation == "is_null" else series.notna()

    if operation == "between":
        if value is None or second_value is None:
            raise agents.ToolError("Operation 'between' needs both value and second_value.")
        low = _coerce_value(series, str(value), column)
        high = _coerce_value(series, str(second_value), column)
        if pd.api.types.is_numeric_dtype(series):
            return series.between(low, high)
        return (series >= low) & (series <= high)

    if value is None:
        raise agents.ToolError(f"Operation '{operation}' needs a value for column '{column}'.")
    text = str(value)

    if operation in {"gt", "gte", "lt", "lte"}:
        target = _coerce_value(series, text, column)
        comparisons = {
            "gt": series > target,
            "gte": series >= target,
            "lt": series < target,
            "lte": series <= target,
        }
        return comparisons[operation]

    if operation in {"eq", "ne"}:
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            predicate = (_text_lower(series) == text.strip().lower())
        else:
            target = _coerce_value(series, text, column)
            predicate = series == target
        return predicate if operation == "eq" else ~predicate.fillna(False)

    if operation in {"contains", "starts_with", "ends_with"}:
        as_text = series.astype("string")
        if operation == "contains":
            return as_text.str.contains(text, case=False, na=False)
        if operation == "starts_with":
            return _text_lower(series).str.startswith(text.lower(), na=False)
        return _text_lower(series).str.endswith(text.lower(), na=False)

    if operation in {"in", "not_in"}:
        items = [item.strip().lower() for item in text.split(",") if item.strip()]
        if not items:
            raise agents.ToolError(f"Operation '{operation}' needs at least one comma-separated item in value.")
        in_mask = _text_lower(series).isin(items)
        return in_mask if operation == "in" else ~in_mask.fillna(False)

    raise agents.ToolError(
        f"Unknown operation '{operation}'. Use one of: eq, ne, gt, gte, lt, lte, between, "
        "contains, starts_with, ends_with, in, not_in, is_null, not_null."
    )


def _require_columns(df: pd.DataFrame, columns: list[str], purpose: str) -> None:
    missing = [column for column in columns if column not in df.columns]
    if missing:
        available = ", ".join(map(str, df.columns))
        raise agents.ToolError(
            f"No column named {missing} to use for {purpose}. Available columns: {available}."
        )


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    view = df if limit is None else df.head(limit)
    if view.empty:
        return []
    return json.loads(view.to_json(orient="records", date_format="iso"))


# ---------------------------------------------------------------------------
# Fixed tools: the only capabilities the model has
# ---------------------------------------------------------------------------


@agents.tool
async def load_csv_from_drive(
    ctx: agents.ToolCallbackContext,
    /,
    drive_link: Annotated[str, Field(description="Google Drive link (or file ID) of the CSV/Sheet.")],
) -> dict[str, Any]:
    """Load a tabular file from Google Drive into the analyst's memory.

    Accepts a CSV, an Excel/ODS workbook, or a Google Sheet; a Google Sheet is
    read as the values of its first tab. Returns the file's title, shape, and
    column list with types; rows are never returned here. Loading a new file
    replaces the previous one for this conversation.
    """
    file_id = _parse_drive_link(drive_link)
    dataset, df = await _fetch_dataset(file_id)
    session_id = str(ctx.session.id) if ctx is not None else "local"
    state = SessionState(dataset=dataset, full=df, active=df)
    _sessions[session_id] = state
    _persist(state, _safe_session_id(ctx))
    return {
        "status": "loaded",
        "file_title": dataset.title,
        "file_id": file_id,
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "column_names": [str(c) for c in df.columns],
        "column_types": {str(c): str(t) for c, t in df.dtypes.items()},
    }


@agents.tool
async def describe_columns(
    ctx: agents.ToolCallbackContext,
    /,
    columns: Annotated[
        list[str] | None, Field(description="Subset of columns to describe; all when omitted.")
    ] = None,
    sample_values: Annotated[
        int, Field(description="How many most frequent values to report per column.", ge=1, le=10)
    ] = 5,
) -> dict[str, Any]:
    """Describe the columns of the loaded file.

    For each selected column: data type, non-null and null counts, number of
    distinct values, and either summary statistics (min, max, mean, median,
    quartiles) for numeric columns or the most frequent values for categories.
    Call this before any other analysis so later answers use real column names.
    """
    state = _get_state(ctx)
    view = state.active
    if columns:
        _require_columns(view, columns, "description")
        view = view.loc[:, [c for c in columns if c in view.columns]]
    result: dict[str, Any] = {
        "file_title": state.dataset.title,
        "scope": state.active_description,
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


@agents.tool
async def filter_rows(
    ctx: agents.ToolCallbackContext,
    /,
    column: Annotated[str, Field(description="Column to filter on.")],
    operation: Annotated[FilterOperation, Field(description="Comparison to apply.")],
    value: Annotated[
        str | None, Field(description="Comparison value; comma-separated items for in/not_in.")
    ] = None,
    second_value: Annotated[str | None, Field(description="Upper bound for 'between'.")] = None,
    set_as_active: Annotated[
        bool, Field(description="Narrow every later question to the matching rows.")
    ] = False,
    limit: Annotated[int, Field(description="Sample rows to return.", ge=1, le=_MAX_GROUPS)] = 10,
) -> dict[str, Any]:
    """Filter rows of the loaded file and return matching samples.

    Returns the total number of matching rows and up to ``limit`` sample rows of
    real data. With ``set_as_active`` every later question runs against only the
    matching rows, until reset_scope is called. Filtering never changes the file.
    """
    state = _get_state(ctx)
    scope_rows = int(state.active.shape[0])
    description_before = state.active_description
    mask = _apply_operation(state.active, column, operation, value, second_value)
    matched = state.active[mask]
    if set_as_active:
        state.active = matched
        state.active_description = (
            f"the narrowed selection: rows of '{state.dataset.title}' where {column} {operation}"
            + (f" {value}" if value is not None else "")
        )
        _persist_scope(state, _safe_session_id(ctx))
    return {
        "file_title": state.dataset.title,
        "scope_before": description_before,
        "scope_rows_before": scope_rows,
        "matching_rows": int(matched.shape[0]),
        "evaluated_columns": {},
        "sample_rows": _records(matched, limit),
        "scope_is_narrowed": bool(set_as_active),
        "scope_after": state.active_description,
    }


@agents.tool
async def group_by_aggregate(
    ctx: agents.ToolCallbackContext,
    /,
    group_by: Annotated[str, Field(description="Column, or comma-separated columns, to group by.")],
    metrics: Annotated[
        dict[str, str],
        Field(description="Mapping column -> aggregate; aggregates: " + ", ".join(_AGG_FUNCS) + ". Use \"*\" to count rows."),
    ],
    order_by: Annotated[str | None, Field(description="Aggregate column to sort the result by.")] = None,
    ascending: Annotated[bool, Field(description="Sort ascending (default) or descending.")] = True,
    limit: Annotated[int, Field(description="Groups to return.", ge=1, le=_MAX_GROUPS)] = 50,
) -> dict[str, Any]:
    """Compute group-by aggregates over the loaded file.

    Groups the current scope by one or more columns and computes the requested
    aggregates, e.g. ``{"revenue": "sum", "price": "mean", "*": "count"}``.
    Use ``order_by`` to rank groups. Returns the aggregate table with real
    numbers and the total number of groups.
    """
    state = _get_state(ctx)
    keys = [key.strip() for key in group_by.split(",") if key.strip()]
    if not keys:
        raise agents.ToolError("Pass at least one column in group_by.")
    _require_columns(state.active, keys, "grouping")

    aggregations: dict[str, str] = {}
    row_count = False
    for metric_column, func in metrics.items():
        func_name = str(func).lower().strip()
        if func_name not in _AGG_FUNCS:
            raise agents.ToolError(
                f"Unsupported aggregate '{func}'. Use one of: {', '.join(_AGG_FUNCS)}."
            )
        if metric_column == "*":
            row_count = True
            continue
        _require_columns(state.active, [metric_column], "aggregation")
        if metric_column in aggregations and aggregations[metric_column] != func_name:
            raise agents.ToolError(
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
        while "row_count" in keys or "row_count" in named:
            raise agents.ToolError("Rename a column to 'row_count' first; it is reserved for row counts.")
        named["row_count"] = pd.NamedAgg(column=keys[0], aggfunc="count")

    result = state.active.groupby(keys, dropna=False).agg(**named).reset_index()
    if order_by:
        _require_columns(result, [order_by], "ordering")
        result = result.sort_values(order_by, ascending=ascending)
    return {
        "file_title": state.dataset.title,
        "scope": state.active_description,
        "total_groups": int(len(result)),
        "aggregate_columns": [str(c) for c in result.columns],
        "table": _records(result, limit),
    }


@agents.tool
async def reset_scope(ctx: agents.ToolCallbackContext, /) -> dict[str, Any]:
    """Restore the full dataset after a narrowed scope.

    Cancels any ``set_as_active`` narrowing from filter_rows so later questions
    look at every row again.
    """
    state = _get_state(ctx)
    state.active = state.full
    state.active_description = "all rows of the loaded file"
    _persist_scope(state, _safe_session_id(ctx))
    return {
        "file_title": state.dataset.title,
        "scope": "all rows of the loaded file",
        "rows": int(state.full.shape[0]),
    }


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

INSTRUCTIONS = """You are a conversational data analyst. The user gives you a Google Drive link
to a CSV (or a Google Sheet) and asks questions about it in plain language. Your conversation
memory is durable: everything loaded or discussed earlier in this conversation still holds.

How to work:

1. To load data you need a Google Drive link or file ID. If the user shared one earlier in this
   conversation, reuse it - never ask again for something already given. Call load_csv_from_drive
   with the link; it returns the file title, row count, and column list with types, but no rows.
2. Before answering, call describe_columns to learn the real column names, types and value
   distributions. Never invent column names.
3. Answer every question with tool results: describe_columns for distributions, filter_rows for
   matching rows (set set_as_active when the user narrows the subject, e.g. "now only EU
   customers"), group_by_aggregate for totals, averages, counts and rankings by category, and
   reset_scope when the user asks about all rows again. Chain steps when one is not enough.
4. Every number you state must come from a tool result in this conversation. If the fixed tools
   cannot answer a question (free text judgment, cross-file joins, statistics beyond the listed
   aggregates), say so plainly and offer the closest available analysis.
5. Be concise and conversational. Report real numbers with units, name the columns used, state
   the row count any percentage is based on, and round sensibly.
6. A new load_csv_from_drive replaces the previously loaded file. If a question mentions a
   column that does not exist, list available columns instead of guessing.

Your only capabilities are the five fixed tools. You cannot run code, open a shell, edit files,
or fetch the web. Stay within the tools and never promise otherwise.
"""


agent = agents.Agent(
    model="mistral-medium-latest",
    name="drive-data-analyst",
    description="Answers questions with real numbers from a CSV the user shares from Google Drive.",
    instructions=INSTRUCTIONS,
    tools=[
        load_csv_from_drive,
        describe_columns,
        filter_rows,
        group_by_aggregate,
        reset_scope,
    ],
    connectors=[agents.connector(_DRIVE_CONNECTOR)],
)


async def main() -> None:
    link = input("Google Drive link: ")
    question = input("Question: ") or "Describe this dataset."
    state = await agent.run(f"Analyze this Google Drive file: {link}\n\nQuestion: {question}")
    print(state.text)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
