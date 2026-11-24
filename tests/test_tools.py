"""Tests of the fixed analysis tools, without a model, key or network."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

import main


def _make_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["EU", "EU", "US", "APAC", "EU"],
            "revenue": [100.0, 250.5, 80.0, 320.0, 190.25],
            "units": [10, 25, 8, 32, 19],
            "active": [True, True, False, True, True],
            "signup": pd.to_datetime(["2024-01-05", "2024-02-11", "2024-01-09", "2024-03-01", "2024-02-20"]),
        }
    )


def _register_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session_id = "test-session"
    main._CACHE_DIR = tmp_path
    main._sessions.clear()
    df = _make_frame()
    monkeypatch.setitem(
        main._sessions,
        session_id,
        main.SessionState(dataset=main.Dataset("file_123", "sales.csv", "2024-01-01T00:00:00+00:00"), full=df, active=df),
    )
    return None


def _run(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


def test_parse_drive_link_variants():
    link = "https://drive.google.com/file/d/1AbC_deF-1234567890/view?usp=sharing"
    assert main._parse_drive_link(link) == "1AbC_deF-1234567890"
    sheet = "https://docs.google.com/spreadsheets/d/1abcdefghij0123456789/edit#gid=0"
    assert main._parse_drive_link(sheet) == "1abcdefghij0123456789"
    query = "https://drive.google.com/open?id=1xyzabc1234567890123"
    assert main._parse_drive_link(query) == "1xyzabc1234567890123"
    with pytest.raises(main.agents.ToolError):
        main._parse_drive_link("not a link")


def test_parse_tabular_csv_and_xlsx_bytes():
    csv_text = "a,b\n1,x\n2,y\n"
    frame = main._parse_tabular(csv_text.encode(), "t.csv", "text/csv")
    assert list(frame.columns) == ["a", "b"]
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:  # type: ignore[abstract]
        _make_frame().to_excel(writer, index=False)
    frame = main._parse_tabular(buffer.getvalue(), "t.xlsx", "xlsx")
    assert len(frame) == 5
    with pytest.raises(main.agents.ToolError):
        main._parse_tabular(b"\x00\x01\x02", "broken.xlsx", "xlsx")


def test_require_columns_reports_available():
    df = _make_frame()
    with pytest.raises(main.agents.ToolError, match="Available columns"):
        main._require_columns(df, ["not_here"], "test")


def test_apply_operation_numeric_and_string():
    df = _make_frame()
    assert main._apply_operation(df, "revenue", "gt", "150", None).sum() == 3
    assert main._apply_operation(df, "revenue", "between", "80", "200", ).sum() == 3
    assert main._apply_operation(df, "region", "eq", "eu", None).sum() == 3
    assert main._apply_operation(df, "region", "in", "US,APAC", None).sum() == 2
    assert main._apply_operation(df, "region", "contains", "pa", None).sum() == 1
    assert main._apply_operation(df, "active", "eq", "true", None).sum() == 4
    assert main._apply_operation(df, "units", "is_null", None, None).sum() == 0
    with pytest.raises(main.agents.ToolError):
        main._apply_operation(df, "revenue", "gt", "abc", None)


def test_apply_operation_dates():
    df = _make_frame()
    assert main._apply_operation(df, "signup", "gte", "2024-02-01", None).sum() == 3


def test_tools_use_session_state():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        main._CACHE_DIR = Path(tmp)
        main._sessions.clear()
        main._sessions["local"] = main.SessionState(
            dataset=main.Dataset("file_123", "sales.csv", "2024-01-01T00:00:00+00:00"),
            full=_make_frame(),
            active=_make_frame(),
        )
        described = _run(main.describe_columns, columns=["revenue"])
        entry = described["evaluated_columns"]["revenue"]
        assert entry["non_null"] == 5
        assert entry["stats"]["mean"] == pytest.approx((100.0 + 250.5 + 80.0 + 320.0 + 190.25) / 5)

        filtered = _run(
            main.filter_rows,
            column="region",
            operation="eq",
            value="EU",
            set_as_active=True,
            limit=5,
        )
        assert filtered["matching_rows"] == 3

        grouped = _run(
            main.group_by_aggregate,
            group_by="region",
            metrics={"revenue": "sum", "*": "count"},
            order_by="revenue",
            ascending=False,
        )
        top = grouped["table"][0]
        assert top["region"] == "EU"
        assert top["revenue"] == pytest.approx(540.75)
        assert top["row_count"] == 3

        scoped_group = _run(main.group_by_aggregate, group_by="active", metrics={"units": "sum"})
        assert scoped_group["scope"].startswith("the narrowed selection")
        assert len(scoped_group["table"]) == 1

        reset = _run(main.reset_scope)
        assert reset["rows"] == 5
        assert reset["scope"] == "all rows of the loaded file"

        narrow_after_reset = _run(
            main.filter_rows, column="units", operation="gte", value="19", limit=10
        )
        assert narrow_after_reset["matching_rows"] == 3
        assert narrow_after_reset["sample_rows"][0]["region"] in {"EU", "APAC"}


def test_describe_unknown_column_is_tool_error():
    with tempfile.TemporaryDirectory() as tmp:
        main._CACHE_DIR = Path(tmp)
        main._sessions.clear()
        main._sessions["local"] = main.SessionState(
            dataset=main.Dataset("file_123", "sales.csv", "t"),
            full=_make_frame(),
            active=_make_frame(),
        )
        with pytest.raises(main.agents.ToolError, match="Available columns"):
            _run(main.describe_columns, columns=["nope"])


def test_group_by_bad_aggregate_is_tool_error():
    main._sessions.clear()
    main._sessions["local"] = main.SessionState(
        dataset=main.Dataset("file_123", "sales.csv", "t"),
        full=_make_frame(),
        active=_make_frame(),
    )
    with pytest.raises(main.agents.ToolError, match="Unsupported aggregate"):
        _run(main.group_by_aggregate, group_by="region", metrics={"revenue": "variance"})


def test_no_dataset_loaded_is_tool_error():
    main._sessions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        main._CACHE_DIR = Path(tmp)
        with pytest.raises(main.agents.ToolError, match="No dataset is loaded"):
            _run(main.describe_columns)


def test_decode_content_guard(monkeypatch: pytest.MonkeyPatch):
    small = base64.b64encode(b"a,b\n1,2\n").decode()
    assert main._decode_content(small) == b"a,b\n1,2\n"
    monkeypatch.setattr(main, "_MAX_BYTES", 10)
    with pytest.raises(main.agents.ToolError, match="larger than"):
        main._decode_content(base64.b64encode(b"x" * 20).decode())


def test_records_serializes_datetimes_and_nan():
    df = pd.DataFrame(
        {"when": pd.to_datetime(["2024-01-01T00:00:00"]), "value": [float("nan")]}
    )
    rows = main._records(df)
    assert rows[0]["when"] == "2024-01-01T00:00:00.000"
    assert rows[0]["value"] is None
