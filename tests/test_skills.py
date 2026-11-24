"""Tests of the user-loadable skills subsystem (skills/ + load_skill tool)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from standalone import agent as agent_mod
from standalone import memory, skills, tools
from standalone.skills import SkillError, discover_skills, load_skill, skills_root


@pytest.fixture(autouse=True)
def isolated_skills(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    monkeypatch.setenv("ANALYST_SKILLS_DIR", str(root))
    return root


def write_skill(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_discover_folder_skill_with_front_matter(isolated_skills: Path):
    write_skill(
        isolated_skills / "my-style" / "SKILL.md",
        "---\nname: my-style\ndescription: Tone of voice for the team\n---\nBe brief. Use rubles.\n",
    )
    found = discover_skills()
    assert len(found) == 1
    assert found[0].name == "my-style"
    assert found[0].description == "Tone of voice for the team"
    assert found[0].path == isolated_skills / "my-style" / "SKILL.md"


def test_discover_flat_file_and_fallbacks(isolated_skills: Path):
    write_skill(isolated_skills / "term-glossary.md", "# Привет\nОписывает термины без front matter.\n")
    found = discover_skills()
    assert [skill.name for skill in found] == ["term-glossary"]
    assert "термины" in found[0].description

    write_skill(isolated_skills / "README.md", "# not a skill\n")
    found = discover_skills()
    assert [skill.description for skill in found] == ["Описывает термины без front matter."]
    assert all(skill.path.name.lower() != "readme.md" for skill in found)


def test_skills_folder_missing_or_empty(isolated_skills: Path):
    assert discover_skills() == []
    isolated_skills.mkdir()
    assert discover_skills() == []


def test_load_skill_returns_body(isolated_skills: Path):
    body = "Правило один. Правило два."
    write_skill(isolated_skills / "rules" / "SKILL.md", f"---\nname: rules\ndescription: test\n---\n{body}\n")
    result = load_skill("rules")
    assert result["skill"] == "rules"
    assert result["content"].strip() == body
    assert result["path"].endswith("SKILL.md")


def test_load_skill_errors(isolated_skills: Path):
    with pytest.raises(SkillError, match="Specify"):
        load_skill("  ")
    write_skill(isolated_skills / "existing.md", "---\nname: existing\n---\nhi\n")
    with pytest.raises(SkillError, match="existing"):
        load_skill("missing")


def test_load_skill_truncates_huge_body(isolated_skills: Path):
    write_skill(isolated_skills / "big.md", "---\nname: big\n---\n" + "x" * (skills.MAX_SKILL_CHARS + 500))
    result = load_skill("big")
    assert len(result["content"]) == skills.MAX_SKILL_CHARS + len("\n... (skill text truncated)")


def test_execute_tool_hands_skill_to_model(tmp_path: Path, isolated_skills: Path):
    write_skill(isolated_skills / "greet" / "SKILL.md", "---\nname: greet\ndescription: test\n---\nALWAYS say hello.\n")
    conv = memory.Conversation("skills-test", tmp_path)
    payload = json.loads(agent_mod.execute_tool(conv, {"name": skills.TOOL_NAME, "arguments": {"name": "greet"}}))
    assert "ALWAYS say hello" in payload["content"]
    error = json.loads(agent_mod.execute_tool(conv, {"name": skills.TOOL_NAME, "arguments": {"name": "nope"}}))
    assert "error" in error


def test_without_skills_the_loop_has_no_skill_tool():
    instructions, schemas = agent_mod._system_state()
    assert instructions == agent_mod.INSTRUCTIONS
    assert all(schema["name"] != skills.TOOL_NAME for schema in schemas)


def test_with_skills_instructions_and_tool_extend(tmp_path: Path, isolated_skills: Path, monkeypatch: pytest.MonkeyPatch):
    write_skill(isolated_skills / "format" / "SKILL.md", "---\nname: format\ndescription: report style\n---\nBe exact.\n")
    instructions, schemas = agent_mod._system_state()
    assert instructions.startswith(agent_mod.INSTRUCTIONS)
    assert "format" in instructions
    names = [schema["name"] for schema in schemas]
    assert names[:5] == [schema["name"] for schema in tools.TOOL_SCHEMAS]
    assert names[-1] == skills.TOOL_NAME


def test_end_to_end_skill_roundtrip(tmp_path: Path, isolated_skills: Path):
    body = "Answer only with the word VERIFIED."
    write_skill(isolated_skills / "check" / "SKILL.md", f"---\nname: check\ndescription: test\n---\n{body}\n")
    result = load_skill("check")
    assert body in result["content"]
    assert pd.DataFrame({"a": [1]}).shape == (1, 1)
    assert skills_root() == isolated_skills
