"""User-loadable skills: plain Markdown files that extend the agent.

A skill is a folder in ``skills/`` containing a ``SKILL.md`` (``name`` and
``description`` in a front-matter block, then instructions), or a single
``.md`` file directly inside ``skills/``. Skills are text only - nothing is
executed - so they can shape style, vocabulary, report formats, checklists and
domain rules without any code. They are discovered live at every turn: drop a
file in and it is picked up on the next question.

Position of the skills folder, in order: the ``ANALYST_SKILLS_DIR`` variable,
then ``skills`` next to the program (EXE or command), which for a frozen EXE is
the folder the EXE itself is in.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILLS_DIR_NAME = "skills"
SKILL_FILE_NAME = "SKILL.md"
MAX_SKILL_CHARS = 15000
TOOL_NAME = "load_skill"


class SkillError(Exception):
    """Raised when a skill cannot be located."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path


def skills_root() -> Path:
    override = os.environ.get("ANALYST_SKILLS_DIR")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / SKILLS_DIR_NAME
    return Path(__file__).resolve().parent.parent / SKILLS_DIR_NAME


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()[1:]
    for index, line in enumerate(lines):
        if line.strip() == "---":
            meta: dict[str, str] = {}
            for entry in lines[:index]:
                key, sep, value = entry.partition(":")
                if sep:
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            body = "\n".join(lines[index + 1 :]).strip()
            return meta, body
    return {}, text


def _fallback_description(body: str) -> str:
    for line in body.splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#"):
            continue
        return clean[:160]
    for line in body.splitlines():
        clean = line.lstrip("#").strip()
        if clean:
            return clean[:160]
    return ""


def _read_skill(path: Path, declared_name: str) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = _parse_front_matter(text)
    name = meta.get("name") or declared_name
    description = meta.get("description") or _fallback_description(body)
    return Skill(name=name, description=description, path=path)


def discover_skills() -> list[Skill]:
    root = skills_root()
    if not root.is_dir():
        return []
    found: list[Skill] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        if path.stem.lower().startswith("readme"):
            continue
        declared = path.stem if path.name != SKILL_FILE_NAME else path.parent.name
        skill = _read_skill(path, declared)
        if skill is None or not skill.name:
            continue
        if skill.name.lower() in seen:
            continue
        seen.add(skill.name.lower())
        found.append(skill)
    return found


EXAMPLE_SKILL_TEXT = '''---
name: report-format
description: как оформлять ответы и итоговые отчёты по данным
---

# Формат отчёта

Отвечая на итоговый вопрос пользователя, придерживайся структуры:

1. **Краткий ответ** - один-два предложения с главными числами.
2. **Таблица** - если вопрос подразумевает разбивку, приведи результат group_by.
3. **Подробности** - на какой строке данных построен ответ, какие колонки использованы.

Тон: деловой, без воды. Проценты всегда сопровождай абсолютными числами и
упоминанием размера выборки. Если данных не хватает - прямо скажи, каких.
'''


def ensure_example_skill() -> Path:
    """Create the skills folder with one example skill, when there are none."""
    root = skills_root()
    try:
        if discover_skills():
            return root
        root.mkdir(parents=True, exist_ok=True)
        example = root / "example-report" / SKILL_FILE_NAME
        example.parent.mkdir(parents=True, exist_ok=True)
        example.write_text(EXAMPLE_SKILL_TEXT, encoding="utf-8")
    except OSError:
        pass
    return root


SKILL_TOOL_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Load the full text of a user skill by name. Use it before following a skill's"
        " instructions, and whenever the user asks to apply a skill. Available skills are"
        " listed in the system instructions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name as listed in the system instructions."}
        },
        "required": ["name"],
    },
}


def _matches(skill: Skill, requested: str) -> bool:
    needle = requested.strip().lower()
    return needle in {skill.name.lower(), skill.path.parent.name.lower(), skill.path.stem.lower()}


def load_skill(requested: str) -> dict[str, str]:
    name = (requested or "").strip()
    if not name:
        raise SkillError("Specify the skill name (see the list in the system instructions).")
    for skill in discover_skills():
        if _matches(skill, name):
            try:
                text = skill.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise SkillError(f"Could not read skill '{skill.name}': {exc}.") from exc
            _, body = _parse_front_matter(text)
            if len(body) > MAX_SKILL_CHARS:
                body = body[:MAX_SKILL_CHARS] + "\n... (skill text truncated)"
            return {"skill": skill.name, "path": str(skill.path), "content": body}
    available = ", ".join(skill.name for skill in discover_skills()) or "(none)"
    raise SkillError(f"No skill called '{name}'. Available skills: {available}.")


def run_load_skill(name: str = "") -> dict[str, str]:
    return load_skill(name)


def build_section(skills: list[Skill] | None = None) -> str:
    if skills is None:
        skills = discover_skills()
    if not skills:
        return ""
    listing = "\n".join(f"- {skill.name}: {skill.description}" for skill in skills)
    return (
        "\n\nUser skills (additional instructions loaded from files, the tool load_skill is available):\n"
        f"{listing}\n"
        "When a task matches a skill, or the user names one, call load_skill with its name first"
        " and then follow the text it returns. Skills may define formatting, terminology and"
        " checklists; they never add code, and every number must still come from the data tools."
    )


__all__ = [
    "EXAMPLE_SKILL_TEXT",
    "SKILLS_DIR_NAME",
    "SKILL_FILE_NAME",
    "SKILL_TOOL_SCHEMA",
    "Skill",
    "SkillError",
    "TOOL_NAME",
    "build_section",
    "discover_skills",
    "ensure_example_skill",
    "load_skill",
    "run_load_skill",
    "skills_root",
]
