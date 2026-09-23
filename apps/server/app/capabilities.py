"""Read-only endpoints for what agents can reach: MCP servers and skills.

Both lists are decided by files (config/mcp.yaml, packages/skills/library/)
that are re-read when they change, so this is the same view the next turn
gets — not a snapshot taken at startup. No write endpoints: editing a server
or a skill is a file edit, and the settings page links to the file rather
than pretending the API owns it.
"""
from fastapi import APIRouter, HTTPException, Request

from packages.governance.secrets import load_secrets
from packages.mcp.wiring import describe
from packages.skills.loader import load_library

router = APIRouter()


async def _secret_env(request: Request) -> dict:
    """Registered secret names and values, as the adapters would inject them.

    Needed because whether a server counts as available depends on whether its
    `requires_env` secrets exist. A failure here means "no secrets", never a
    500: the list is still useful without them, with those servers shown as
    inactive for exactly that reason."""
    try:
        return {
            secret.name: secret.value
            for secret in await load_secrets(
                request.app.state.pool, request.app.state.secret_cipher
            )
        }
    except Exception:  # noqa: BLE001 - see docstring
        return {}


@router.get("/mcp/servers")
async def mcp_servers(request: Request, role: str = "worker") -> dict:
    return describe(role, env=await _secret_env(request))


@router.get("/skills")
async def skills(role: str = "") -> dict:
    library = load_library()
    selected = library.for_role(role) if role else library.skills
    return {
        "skills": [
            {
                "name": skill.name,
                "title": skill.title,
                "description": skill.description,
                "roles": skill.roles,
                "mcp": skill.mcp,
                "inputs": [field.model_dump() for field in skill.inputs],
                "output": skill.output.model_dump(),
            }
            for skill in selected
        ],
        # Shown rather than swallowed: a skill file that fails validation is
        # invisible to the agent, and the only way anyone finds out is here.
        "errors": [{"path": error.path, "message": error.message} for error in library.errors],
    }


@router.get("/skills/{name}")
async def skill_detail(name: str) -> dict:
    skill = load_library().get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail="unknown skill")
    return skill.model_dump()
