"""Skill definitions and their schemas.

A skill is a named, parameterized instruction: the wording a sub-agent should
follow for a recurring job, plus the inputs that wording needs and the shape
of the answer it should come back with. Roadmap stage 6 fixes the format as
YAML validated by Pydantic, deliberately close to how Claude Code's own
skills work — a catalog the agent reads, not a function ANA calls on its
behalf.

Validation is strict on purpose: a skill with a typo'd field is dropped with a
message rather than silently shipped into an agent's system prompt, where the
failure would look like the model ignoring instructions.
"""
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "integer", "number", "boolean", "array", "object"] = "string"
    description: str = ""
    required: bool = True
    default: Optional[Any] = None


class SkillOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "number", "boolean", "array", "object"] = "string"
    description: str = ""


class SkillDefinition(BaseModel):
    """One `packages/skills/library/*.yaml` file."""

    model_config = ConfigDict(extra="forbid")

    name: str
    title: str = ""
    description: str
    # Which agent roles may use it. The lead plans and delegates, so skills
    # are worker-scoped unless a file says otherwise.
    roles: list[str] = Field(default_factory=lambda: ["worker"])
    # MCP servers the instructions assume. A skill whose servers are not
    # scoped for the role is left out of that role's catalog rather than
    # offered and then denied at the gate (see packages/mcp/scope.py).
    mcp: list[str] = Field(default_factory=list)
    inputs: list[SkillInput] = Field(default_factory=list)
    output: SkillOutput = Field(default_factory=SkillOutput)
    instructions: str

    def model_post_init(self, _context: Any) -> None:
        if not NAME_RE.match(self.name):
            raise ValueError(
                f"skill name {self.name!r} must be lowercase kebab-case"
            )
        declared = {field.name for field in self.inputs}
        used = set(_PLACEHOLDER_RE.findall(self.instructions))
        unknown = used - declared
        if unknown:
            # Caught here rather than at render time: an instruction referring
            # to an input nobody declared would reach the agent as a literal
            # "{{path}}", which reads as a bug in ANA, not in the skill file.
            raise ValueError(
                f"skill {self.name!r} uses undeclared inputs: {', '.join(sorted(unknown))}"
            )

    def input_model(self) -> type[BaseModel]:
        """A Pydantic model for this skill's inputs, built from the YAML.

        This is what "Pydantic으로 입출력 스키마 검증" means in practice: the
        arguments an agent or the planner supplies are checked against the
        declared schema before they are pasted into a prompt."""
        fields: dict[str, Any] = {}
        for field in self.inputs:
            annotation = TYPES[field.type]
            if field.required and field.default is None:
                fields[field.name] = (annotation, ...)
            else:
                fields[field.name] = (Optional[annotation], field.default)
        return create_model(  # type: ignore[call-overload]
            f"{self.name.replace('-', '_')}_inputs",
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )

    def render(self, inputs: Optional[dict] = None) -> str:
        """The instructions with inputs substituted, after validation.

        Raises pydantic.ValidationError when the inputs do not fit the schema —
        the caller decides whether that is a bad plan or a bad request."""
        validated = self.input_model()(**(inputs or {}))
        values = validated.model_dump()

        def substitute(match: re.Match) -> str:
            value = values.get(match.group(1))
            return "" if value is None else str(value)

        return _PLACEHOLDER_RE.sub(substitute, self.instructions).strip()

    def summary(self) -> str:
        """The one-line form used in a catalog."""
        required = [field.name for field in self.inputs if field.required]
        arguments = f" (입력: {', '.join(required)})" if required else ""
        servers = f" [MCP: {', '.join(self.mcp)}]" if self.mcp else ""
        return f"- {self.name}: {self.description}{arguments}{servers}"


__all__ = [
    "SkillDefinition",
    "SkillInput",
    "SkillOutput",
    "ValidationError",
]
