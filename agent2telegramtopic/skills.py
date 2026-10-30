"""Install the bundled ``telegram-topic`` agent skill for the chosen agent (and remove it again)."""
from __future__ import annotations

from pathlib import Path

SKILL_NAME = "telegram-topic"
_SOURCE = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"
_TARGET_DIRS = {"claude-code": ".claude", "codex": ".codex"}


def skill_target(agent: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / _TARGET_DIRS[agent] / "skills" / SKILL_NAME / "SKILL.md"


def install_skill(agent: str, home: Path | None = None) -> str:
    """Copy the skill to the agent's skills directory; returns a one-line message for the user."""
    target = skill_target(agent, home)
    content = _SOURCE.read_text("utf-8")
    if target.exists():
        if target.read_text("utf-8") == content:
            return f"topic-naming skill already installed: {target}"
        return f"A different {SKILL_NAME} skill already exists at {target}; left unchanged."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, "utf-8")
    return f"installed the topic-naming skill: {target}"


def remove_skills(home: Path | None = None) -> list[Path]:
    """Delete every installed copy that is byte-identical to the bundled skill; returns those removed."""
    content = _SOURCE.read_text("utf-8")
    removed = []
    for agent in _TARGET_DIRS:
        target = skill_target(agent, home)
        try:
            if target.is_file() and target.read_text("utf-8") == content:
                target.unlink()
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
                removed.append(target)
        except (OSError, UnicodeDecodeError):
            pass
    return removed
