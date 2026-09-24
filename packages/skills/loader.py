"""Loading the skill library.

Two directories, in order: the ones shipped with ANA
(`packages/skills/library/`) and the user's own (`$ANA_STATE_DIR/skills/`),
where a later file with the same name replaces an earlier one — so a shipped
skill can be overridden without editing the repo.

Two formats, because skills exist in the world already. `*.yaml` is ANA's own
form, where the procedure is a field. `*.md` is the Claude Code / Agent Skills
form — YAML frontmatter, then the procedure as the body — so a published
SKILL.md can be dropped in as it was written rather than summarized into a YAML
string, which is how a vendored skill quietly drifts from the one it came from.
The two share the same schema: everything the frontmatter does not say gets the
same default a YAML file would.

Broken files never take the library down: they are collected as errors and
reported (the settings page shows them), while everything else loads.
"""
import os
import re
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


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<meta>.*?)\n---\s*\n?(?P<body>.*)\Z", re.S)


def _parse_markdown(text: str) -> dict:
    """A SKILL.md as the same mapping a YAML skill file is.

    The frontmatter carries the fields; the body *is* the instructions, kept
    exactly as written. A file with no frontmatter is not a skill — returning
    an empty mapping makes the caller report it as one, rather than shipping a
    nameless procedure."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    meta = yaml.safe_load(match.group("meta")) or {}
    if not isinstance(meta, dict):
        return {}
    body = match.group("body").strip()
    # An explicit `instructions:` in the frontmatter wins, so a wrapper file can
    # keep the body as documentation.
    meta.setdefault("instructions", body)
    return meta


def _skill_files(directory: Path) -> list[Path]:
    return sorted(
        [*directory.glob("*.y*ml"), *directory.glob("*.md")], key=lambda path: path.stem
    )


def _load_dir(directory: Path) -> tuple[list[SkillDefinition], list[SkillError]]:
    skills: list[SkillDefinition] = []
    errors: list[SkillError] = []
    if not directory.is_dir():
        return skills, errors
    for path in _skill_files(directory):
        try:
            text = path.read_text(encoding="utf-8")
            raw = _parse_markdown(text) if path.suffix == ".md" else yaml.safe_load(text)
        except (OSError, yaml.YAMLError) as exc:
            errors.append(SkillError(str(path), f"파일을 읽지 못함: {exc}"))
            continue
        if not isinstance(raw, dict) or not raw:
            errors.append(
                SkillError(
                    str(path),
                    "스킬 파일은 YAML 매핑이거나, 프론트매터가 있는 마크다운이어야 한다",
                )
            )
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
    return tuple((path.name, path.stat().st_mtime) for path in _skill_files(directory))


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
