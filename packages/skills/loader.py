"""Loading the skill library.

Two directories, in order: the ones shipped with ANA
(`packages/skills/library/`) and the user's own (`$ANA_STATE_DIR/skills/`),
where a later file with the same name replaces an earlier one — so a shipped
skill can be overridden without editing the repo.

Broken files never take the library down: they are collected as errors and
reported (the settings page shows them), while everything else loads.
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from packages.skills.models import SkillDefinition

LIBRARY_DIR = Path(__file__).resolve().parent / "library"


@dataclass(frozen=True)
class SkillError:
    path: str
    message: str


@dataclass(frozen=True)
class SkillLibrary:
    skills: tuple[SkillDefinition, ...] = ()
    errors: tuple[SkillError, ...] = ()

    def get(self, name: str) -> Optional[SkillDefinition]:
        return next((skill for skill in self.skills if skill.name == name), None)

    def for_role(self, role: str, available_servers: Optional[set[str]] = None
                 ) -> tuple[SkillDefinition, ...]:
        """The skills an agent in this role can actually carry out.

        A skill that needs an MCP server this role does not have is left out:
        offering it would put the agent on a path that ends at a gate denial."""
        selected = []
        for skill in self.skills:
            if role not in skill.roles:
                continue
            if available_servers is not None and not set(skill.mcp) <= available_servers:
                continue
            selected.append(skill)
        return tuple(selected)


def user_skills_dir() -> Path:
    state_dir = os.environ.get("ANA_STATE_DIR", os.path.expanduser("~/.ana"))
    return Path(os.environ.get("ANA_SKILLS_DIR") or Path(state_dir) / "skills")


def _load_dir(directory: Path) -> tuple[list[SkillDefinition], list[SkillError]]:
    skills: list[SkillDefinition] = []
    errors: list[SkillError] = []
    if not directory.is_dir():
        return skills, errors
    for path in sorted(directory.glob("*.y*ml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(SkillError(str(path), f"YAML을 읽지 못함: {exc}"))
            continue
        if not isinstance(raw, dict):
            errors.append(SkillError(str(path), "스킬 파일은 YAML 매핑이어야 한다"))
            continue
        try:
            skills.append(SkillDefinition(**raw))
        except ValidationError as exc:
            errors.append(SkillError(str(path), _first_error(exc)))
        except ValueError as exc:  # model_post_init's own checks
            errors.append(SkillError(str(path), str(exc)))
    return skills, errors


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"]) or "(root)"
    return f"{location}: {error['msg']}"


def _stamp(directory: Path) -> tuple:
    """A cheap fingerprint of a directory: which files it holds and when each
    changed. Keyed on this, the cache reloads exactly when something on disk
    moved — the same contract governance rules and routing already use."""
    if not directory.is_dir():
        return ()
    return tuple(
        (path.name, path.stat().st_mtime)
        for path in sorted(directory.glob("*.y*ml"))
    )


@lru_cache(maxsize=8)
def _cached_library(_library_stamp: tuple, _user_stamp: tuple, user_dir: str) -> SkillLibrary:
    skills, errors = _load_dir(LIBRARY_DIR)
    user_skills, user_errors = _load_dir(Path(user_dir))
    by_name = {skill.name: skill for skill in skills}
    by_name.update({skill.name: skill for skill in user_skills})
    return SkillLibrary(
        skills=tuple(by_name[name] for name in sorted(by_name)),
        errors=tuple(errors + user_errors),
    )


def load_library() -> SkillLibrary:
    user_dir = user_skills_dir()
    return _cached_library(_stamp(LIBRARY_DIR), _stamp(user_dir), str(user_dir))
