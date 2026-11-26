"""Tests of the standalone (Mistral-free) agent: tools, memory, providers, loop.

The end-to-end tests run a local mock LLM server in-process, so they verify the
complete autonomy of the loop - model calls, tool execution, persistent memory -
without any external service or API key.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pandas as pd
import pytest

from standalone import agent as agent_mod
from standalone import llm
from standalone import memory
from standalone import tools


@pytest.fixture(autouse=True)
def isolated_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))


def make_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region": ["EU", "EU", "US", "APAC", "EU"],
            "revenue": [100.0, 250.5, 80.0, 320.0, 190.25],
            "units": [10, 25, 8, 32, 19],
            "active": [True, True, False, True, True],
        }
    )


@pytest.fixture()
def conv(tmp_path: Path) -> memory.Conversation:
    conversation = memory.Conversation("test", tmp_path)
    conversation.set_dataset(
        tools.Dataset(source="local", title="sales.csv", load_time="2024-01-01T00:00:00+00:00"),
        make_frame(),
    )
    return conversation


# --------------------------------------------------------------------------- tools


def test_extract_drive_id_variants():
    assert tools.extract_drive_id("https://drive.google.com/file/d/1AbC_deF-1234567890/view") == "1AbC_deF-1234567890"
    assert tools.extract_drive_id("https://docs.google.com/spreadsheets/d/1abcdefghij0123456789/edit#gid=0") == "1abcdefghij0123456789"
    assert tools.extract_drive_id("1xyzabc1234567890123") == "1xyzabc1234567890123"
    with pytest.raises(tools.ToolError):
        tools.extract_drive_id("not a link")


def test_parse_tabular_csv_and_xlsx_bytes(tmp_path: Path):
    frame = tools.parse_tabular(b"a,b\n1,x\n2,y\n", "t.csv", "text/csv")
    assert list(frame.columns) == ["a", "b"]
    buffer = tmp_path / "t.xlsx"
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:  # type: ignore[abstract]
        make_frame().to_excel(writer, index=False)
    frame = tools.parse_tabular(buffer.read_bytes(), "t.xlsx", "xlsx")
    assert len(frame) == 5
    with pytest.raises(tools.ToolError):
        tools.parse_tabular(b"\x00\x01\x02", "broken.xlsx", "xlsx")


def test_require_columns_reports_available():
    with pytest.raises(tools.ToolError, match="Available columns"):
        tools.require_columns(make_frame(), ["nope"], "test")


def test_apply_operation_numeric_and_string():
    df = make_frame()
    assert tools.apply_operation(df, "revenue", "gt", "150", None).sum() == 3
    assert tools.apply_operation(df, "revenue", "between", "80", "200").sum() == 3
    assert tools.apply_operation(df, "region", "eq", "eu", None).sum() == 3
    assert tools.apply_operation(df, "region", "in", "US,APAC", None).sum() == 2
    assert tools.apply_operation(df, "active", "is_null", None, None).sum() == 0
    with pytest.raises(tools.ToolError):
        tools.apply_operation(df, "units", "between", "1", None)


def test_tool_flow_uses_scope(conv: memory.Conversation):
    described = tools.describe_columns(conv, columns=["revenue"])
    assert described["rows"] == 5
    filtered = tools.filter_rows(conv, "region", "eq", "EU", set_as_active=True)
    assert filtered["matching_rows"] == 3
    assert conv.active_description != "all rows of the loaded file"
    grouped = tools.group_by_aggregate(conv, group_by="region", metrics={"revenue": "sum", "*": "count"})
    assert grouped["total_groups"] == 1
    assert grouped["table"][0]["row_count"] == 3
    tools.reset_scope(conv)
    assert tools.describe_columns(conv)["rows"] == 5


def test_tools_require_loaded_dataset():
    conversation = memory.Conversation("empty", Path("/tmp") / "drive-analyst-test-nothing-here")
    with pytest.raises(tools.ToolError, match="No dataset"):
        tools.describe_columns(conversation)


def test_group_by_renames_and_reserved(conv: memory.Conversation):
    grouped = tools.group_by_aggregate(conv, group_by="region", metrics={"region": "count"})
    assert "region_count" in grouped["aggregate_columns"]
    clash = conv.active.rename(columns={"revenue": "row_count"})
    conv.set_scope(clash, "temporary test scope")
    with pytest.raises(tools.ToolError, match="reserved"):
        tools.group_by_aggregate(conv, group_by="row_count", metrics={"*": "count"})
    with pytest.raises(tools.ToolError, match="Unsupported aggregate"):
        tools.group_by_aggregate(conv, group_by="region", metrics={"revenue": "unsupported"})


# --------------------------------------------------------------------------- memory


def test_memory_roundtrip_with_dataset(tmp_path: Path):
    conversation = memory.Conversation("trip", tmp_path)
    conversation.append({"role": "user", "content": "hello"})
    conversation.set_dataset(
        tools.Dataset(source="https://x/y.csv", title="y.csv", load_time="now"),
        make_frame(),
    )
    tools.filter_rows(conversation, "region", "eq", "EU", set_as_active=True)
    fresh = memory.Conversation("trip", tmp_path).load()
    assert fresh.messages[0]["content"] == "hello"
    assert fresh.dataset.title == "y.csv"
    assert fresh.active_description != "all rows of the loaded file"
    assert len(fresh.active) == 3
    tools.reset_scope(fresh)
    again = memory.Conversation("trip", tmp_path).load()
    assert again.active_description == "all rows of the loaded file"
    assert len(again.active) == 5


def test_safe_name():
    cleaned = memory.safe_name("../bad name!")
    assert "/" not in cleaned and ".." not in cleaned and cleaned.strip("_") == "bad_name"


def test_conversation_accepts_str_root(tmp_path: Path):
    conv = memory.Conversation("strroot", str(tmp_path / "mem"))
    conv.append({"role": "user", "content": "hi"})
    fresh = memory.Conversation("strroot", str(tmp_path / "mem")).load()
    assert fresh.messages[0]["content"] == "hi"


# --------------------------------------------------------------------------- providers


class MockHandler(BaseHTTPRequestHandler):
    script: list[dict] = []
    requests: list[dict] = []
    extra_files: dict[str, bytes] = {}

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        MockHandler.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
        scripted = (
            MockHandler.script.pop(0)
            if MockHandler.script
            else {"choices": [{"message": {"content": "done"}}]}
        )
        payload = json.dumps(scripted).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        body = MockHandler.extra_files.get(self.path, b"")
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    MockHandler.script = []
    MockHandler.requests = []
    MockHandler.extra_files = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_skills_end_to_end_with_loop(mock_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(skills_dir))
    skills_dir.mkdir()
    (skills_dir / "check").mkdir()
    (skills_dir / "check" / "SKILL.md").write_text(
        "---\nname: check\ndescription: test skill\n---\nAnswer only with the word VERIFIED.\n",
        encoding="utf-8",
    )
    client = llm.OpenAICompatClient("test-model", mock_server, "test-key")
    conv = memory.Conversation("skills", tmp_path)
    MockHandler.script = [
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "s1", "function": {"name": "load_skill", "arguments": json.dumps({"name": "check"})}}
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "ГОТОВО: VERIFIED"}}]},
    ]
    answer = agent_mod.run_turn(conv, client, "Enable the check skill.")
    assert answer == "VERIFIED"
    completions = [request for request in MockHandler.requests if request["path"].endswith("/chat/completions")]
    tool_names = [tool["function"]["name"] for tool in completions[0]["body"]["tools"]]
    assert "load_skill" in tool_names
    assert "check: test skill" in completions[0]["body"]["messages"][0]["content"]
    tool_messages = [message for request in completions for message in request["body"]["messages"] if message.get("role") == "tool"]
    assert "VERIFIED" in tool_messages[0]["content"]


def test_openai_loop_end_to_end_and_memory(mock_server: str, tmp_path: Path):
    MockHandler.extra_files["/data.csv"] = b"region,cost\nEU,10\nUS,20\nEU,30\n"
    client = llm.OpenAICompatClient("test-model", mock_server, "test-key")
    conv = memory.Conversation("e2e", tmp_path)
    MockHandler.script = [
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "load_csv_from_drive",
                                    "arguments": json.dumps({"source": f"{mock_server}/data.csv"}),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c2",
                                "function": {
                                    "name": "group_by_aggregate",
                                    "arguments": json.dumps({"group_by": "region", "metrics": {"cost": "sum", "*": "count"}}),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "ГОТОВО: EU has 2 rows with cost 40 in total."}}]},
    ]
    answer = agent_mod.run_turn(conv, client, "Load my data and tell me about EU costs.")
    assert answer == "EU has 2 rows with cost 40 in total."
    completions = [request for request in MockHandler.requests if request["path"].endswith("/chat/completions")]
    assert len(completions) == 3
    tools_payload = completions[0]["body"]["tools"][0]
    assert tools_payload["type"] == "function"
    assert tools_payload["function"]["name"] == "load_csv_from_drive"
    tool_messages = [message for request in completions for message in request["body"]["messages"] if message.get("role") == "tool"]
    assert tool_messages, "tool results must be sent back to the model"
    assert conv.dataset.title == "data.csv"
    assert (tmp_path / "conversations" / "e2e.jsonl").exists()
    assert (tmp_path / "datasets" / "e2e.parquet").exists()

    fresh = memory.Conversation("e2e", tmp_path).load()
    assert fresh.dataset.title == "data.csv"
    MockHandler.script = [
        {"choices": [{"message": {"content": "Of course: data.csv is still loaded, 3 rows, columns region and cost."}}]},
    ]
    answer2 = agent_mod.run_turn(fresh, client, "Do you remember my dataset?")
    assert "3 rows" in answer2


def test_error_tool_result_reaches_model(mock_server: str, tmp_path: Path):
    client = llm.OpenAICompatClient("test-model", mock_server, "test-key")
    conversation = memory.Conversation("err", tmp_path)
    conversation.set_dataset(tools.Dataset(source="local", title="sales.csv", load_time="t"), make_frame())
    MockHandler.script = [
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "group_by_aggregate",
                                    "arguments": json.dumps({"group_by": "nope", "metrics": {"*": "count"}}),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": "ГОТОВО: There is no column 'nope'; available: region, revenue, units, active."}}]},
    ]
    answer = agent_mod.run_turn(conversation, client, "Group by nope")
    assert "nope" in answer
    tool_messages = [
        message
        for request in MockHandler.requests
        for message in request["body"]["messages"]
        if message.get("role") == "tool"
    ]
    assert tool_messages[0]["tool_call_id"] == "c1"
    payload = json.loads(tool_messages[0]["content"])
    assert "nope" in payload["error"] and payload["error"].startswith("No column named")


def test_anthropic_wire_format(mock_server: str, tmp_path: Path):
    MockHandler.script = [
        {
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_1", "name": "describe_columns", "input": {}},
            ]
        },
        {"content": [{"type": "text", "text": "ГОТОВО: 5 rows across 5 columns."}]},
    ]
    client = llm.AnthropicClient("claude-test", "key", base_url=mock_server)
    conv = memory.Conversation("anth", tmp_path)
    conv.set_dataset(tools.Dataset(source="local", title="t.csv", load_time="t"), make_frame())
    answer = agent_mod.run_turn(conv, client, "What is in the data?")
    assert answer == "5 rows across 5 columns."
    assert len(MockHandler.requests) == 2
    messages = MockHandler.requests[1]["body"]["messages"]
    system = MockHandler.requests[0]["body"]["system"]
    assert system.startswith("You are a conversational data analyst")
    assert [entry["role"] for entry in messages] == ["user", "assistant", "user"]
    tool_result_block = [
        block
        for entry in messages
        for block in entry.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_result_block[0]["tool_use_id"] == "toolu_1"
    tools_schema = MockHandler.requests[1]["body"]["tools"][0]
    assert set(tools_schema) == {"name", "description", "input_schema"}


# --------------------------------------------------------------------------- cli


def test_cli_lists_conversations(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    from standalone.cli import main as cli_main

    (tmp_path / "conversations").mkdir(parents=True)
    (tmp_path / "conversations" / "alpha.jsonl").write_text("{}\n")
    code = cli_main(["--list", "--memory-dir", str(tmp_path)])
    assert code == 0
    assert "alpha" in capsys.readouterr().out


def test_build_client_presets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY"):
        llm.build_client("openai", None, None, None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    client = llm.build_client("openai", "gpt-4o", None, None)
    assert client.base_url == "https://api.openai.com/v1"
    client = llm.build_client("kobold", None, None, None)
    assert client.base_url == "http://127.0.0.1:5001/v1"
    client = llm.build_client("http://127.0.0.1:9999/v1", "q", None, "k")
    assert client.base_url == "http://127.0.0.1:9999/v1"
    client = llm.build_client("anthropic", None, None, "k")
    assert isinstance(client, llm.AnthropicClient)
    with pytest.raises(llm.LLMError, match="Unknown provider"):
        llm.build_client("nope", None, None, None)


def test_gui_config_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    try:
        import tkinter
    except ImportError:
        pytest.skip("tkinter is not available in this environment")
    import standalone.gui as gui
    monkeypatch.setattr(gui, "app_dir", lambda: tmp_path)
    gui.save_config({"provider": "kobold", "model": "qwen-local"})
    stored = gui.load_config()
    assert stored["provider"] == "kobold"
    assert stored["model"] == "qwen-local"
    assert (tmp_path / "gui_config.json").exists()


def test_conversation_stats_and_search(tmp_path: Path):
    conv = memory.Conversation("stats-a", tmp_path).load()
    conv.append({"role": "user", "content": "привет, покажи продажи", "time": memory.utc_now()})
    conv.append({"role": "tool", "content": "group-by output", "time": memory.utc_now()})
    conv.append({"role": "assistant", "content": "Продажи выросли на 12%", "time": memory.utc_now()})
    other = memory.Conversation("stats-b", tmp_path).load()
    other.append({"role": "assistant", "content": "тут ничего похожего", "time": memory.utc_now()})

    stats = memory.conversation_stats(tmp_path)
    assert stats == {"stats-a": 2, "stats-b": 1}
    empty = memory.conversation_stats(tmp_path / "nowhere")
    assert empty == {}

    hits = memory.search_conversations(tmp_path, "ноутбук")
    assert hits == []
    hits = memory.search_conversations(tmp_path, "продажи")
    assert len(hits) == 2
    assert {hit["dialog"] for hit in hits} == {"stats-a"}
    assert {hit["role"] for hit in hits} == {"user", "assistant"}
    assert all(hit["excerpt"] for hit in hits)

    summary = memory.Conversation("stats-summary", tmp_path).load()
    summary.append({"role": "summary", "content": "продажи сжали историю", "time": memory.utc_now()})
    assert memory.search_conversations(tmp_path, "сжали") == []


def test_list_and_delete_conversations(tmp_path: Path):
    conv = memory.Conversation("dialog-a", tmp_path).load()
    conv.append({"role": "user", "content": "привет", "time": memory.utc_now()})
    memory.Conversation("dialog-b", tmp_path).load().append(
        {"role": "user", "content": "второй", "time": memory.utc_now()}
    )
    assert memory.list_conversations(tmp_path) == ["dialog-a", "dialog-b"]
    assert memory.delete_conversation(tmp_path, "dialog-a") is True
    assert memory.list_conversations(tmp_path) == ["dialog-b"]
    assert memory.delete_conversation(tmp_path, "nope") is False
