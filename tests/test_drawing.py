"""Tests of the drawing ability: standalone/pc.py draw_image tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from standalone import agent as agent_mod
from standalone import memory, pc
from standalone.pc import PCError

pytest.importorskip("PIL")


@pytest.fixture(autouse=True)
def isolated_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(tmp_path / "no-skills"))
    monkeypatch.delenv("ANALYST_PC_TOOLS", raising=False)
    monkeypatch.delenv("ANALYST_PC_CONFIRM", raising=False)


@pytest.fixture()
def conv(tmp_path: Path) -> memory.Conversation:
    return memory.Conversation("art", tmp_path).load()


@pytest.fixture()
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "Desktop"
    folder.mkdir()
    monkeypatch.setattr(pc, "desktop_dir", lambda: folder)
    return folder


def test_draw_image_renders_png(conv: memory.Conversation, tmp_path: Path):
    target = tmp_path / "out.png"
    result = pc.draw_image(
        conv,
        str(target),
        [
            {"shape": "rect", "xy": [0, 0, 1000, 1000], "fill": "#112233"},
            {"shape": "line", "xy": [0, 0, 1000, 1000], "width": 3, "fill": "#ffffff"},
            {"shape": "ellipse", "xy": [400, 400, 600, 600], "fill": "#ff0000"},
            {"shape": "polygon", "points": [100, 900, 300, 900, 200, 750], "fill": "#00ff00"},
            {"shape": "text", "xy": [50, 50], "text": "Привет ActualONE", "size": 42, "fill": "#ffffff"},
        ],
        width=512,
        height=384,
    )
    assert result["size"] == [512, 384]
    assert result["shapes"] == 5
    assert Path(result["path"]).is_file()
    from PIL import Image

    with Image.open(target) as image:
        assert image.size == (512, 384)
        assert image.getpixel((10, 90)) == (17, 34, 51)
        assert image.getpixel((255, 191)) == (255, 0, 0)
    audit = (conv.root / pc.AUDIT_NAME).read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(audit[-1])["tool"] == "draw_image"


def test_draw_image_normalizes_grid_and_forces_png(conv: memory.Conversation, tmp_path: Path):
    target = tmp_path / "picture" / "logo"
    result = pc.draw_image(
        conv,
        str(target),
        [{"shape": "rect", "xy": [500, 0, 1000, 1000], "fill": "#000000"}],
        width=200,
        height=100,
        background="#ffffff",
    )
    suffix = Path(result["path"])
    assert suffix.suffix == ".png"
    assert suffix.is_file()
    from PIL import Image

    with Image.open(suffix) as image:
        assert image.size == (200, 100)
        assert image.getpixel((50, 50)) == (255, 255, 255)
        assert image.getpixel((150, 50)) == (0, 0, 0)


def test_draw_image_accepts_json_string(conv: memory.Conversation, tmp_path: Path):
    target = tmp_path / "via-json.png"
    commands = json.dumps([{"shape": "rect", "xy": [0, 0, 100, 100], "fill": "#123456"}])
    result = pc.draw_image(conv, str(target), commands)
    assert Path(result["path"]).is_file()


def test_draw_image_errors(conv: memory.Conversation, tmp_path: Path):
    target = tmp_path / "bad.png"
    with pytest.raises(PCError):
        pc.draw_image(conv, str(target), [{"shape": "spiral", "xy": [1, 2, 3, 4]}])
    with pytest.raises(PCError):
        pc.draw_image(conv, str(target), "not-json")
    with pytest.raises(PCError):
        pc.draw_image(conv, str(target), [])
    with pytest.raises(PCError):
        pc.draw_image(conv, str(target), [{"shape": "rect", "xy": [1, 2]}])
    with pytest.raises(PCError):
        pc.draw_image(conv, str(target), [{"shape": "text", "xy": [1, 2], "text": ""}])
    assert not target.exists()


def test_draw_image_gated_and_schema(monkeypatch: pytest.MonkeyPatch, conv: memory.Conversation, tmp_path: Path):
    payload = json.loads(
        agent_mod.execute_tool(conv, {"name": "draw_image", "arguments": {"path": str(tmp_path / "a.png"), "commands": []}})
    )
    assert "error" in payload
    monkeypatch.setenv("ANALYST_PC_TOOLS", "1")
    schema_names = [schema["name"] for schema in agent_mod._system_state()[1]]
    assert "draw_image" in schema_names
    assert "draw_image" in pc.PC_IMPLS
    assert any("draw_image" in str(schema) for schema in pc.PC_TOOL_SCHEMAS)
    payload = json.loads(
        agent_mod.execute_tool(
            conv,
            {
                "name": "draw_image",
                "arguments": {
                    "path": str(tmp_path / "b.png"),
                    "commands": [{"shape": "rect", "xy": [0, 0, 1000, 1000], "fill": "#204060"}],
                    "width": 64,
                    "height": 64,
                },
            },
        )
    )
    assert Path(payload["path"]).is_file()


def test_draw_image_desktop_path(conv: memory.Conversation, desktop: Path):
    result = pc.draw_image(
        conv,
        "Desktop/art.png",
        [{"shape": "rect", "xy": [0, 0, 100, 100], "fill": "#cc0000"}],
        width=100,
        height=100,
    )
    assert desktop in Path(result["path"]).parents
    assert Path(result["path"]).is_file()


def test_draw_image_scales_overflowing_pixel_coords(conv: memory.Conversation, tmp_path: Path):
    from PIL import Image

    commands = [
        {"shape": "rect", "xy": [0, 0, 4096, 4096], "fill": "#0a0a12"},
        {"shape": "text", "xy": [200, 3200], "text": "ActualONE", "size": 480, "fill": "#ffffff"},
    ]
    result = pc.draw_image(conv, str(tmp_path / "scaled.png"), commands)
    assert result["size"] == [1024, 1024]
    assert result["scaled"] == pytest.approx(1024 / 4096, abs=1e-3)
    with Image.open(tmp_path / "scaled.png") as image:
        assert image.size == (1024, 1024)
        assert image.getpixel((500, 500)) == (10, 10, 18)


def test_draw_image_4k_canvas_keeps_scale_one(conv: memory.Conversation, tmp_path: Path):
    from PIL import Image

    commands = [
        {"shape": "rect", "xy": [0, 0, 1000, 1000], "fill": "#0a0a12"},
        {"shape": "rect", "xy": [400, 400, 600, 600], "fill": "#ff004c"},
    ]
    result = pc.draw_image(conv, str(tmp_path / "four-k.png"), commands, width=4096, height=4096)
    assert result["size"] == [4096, 4096]
    assert result["scaled"] == 1.0
    with Image.open(tmp_path / "four-k.png") as image:
        assert image.size == (4096, 4096)
        assert image.getpixel((100, 100)) == (10, 10, 18)
        assert image.getpixel((2048, 2048)) == (255, 0, 76)
