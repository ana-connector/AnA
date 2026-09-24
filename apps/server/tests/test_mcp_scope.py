"""Scope partitioning: which MCP tool calls the gate lets through."""
import pytest

from packages.mcp.scope import scope_denial, server_for_tool

KNOWN = ("git", "postgres", "github")


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("mcp__git__git_log", "git"),
        ("mcp__postgres__query", "postgres"),
        ("mcpgitgit_log", "git"),  # punctuation-stripped hook form
        ("git__git_log", "git"),  # no mcp prefix
        ("github__create_pr", "github"),
        ("Bash", None),
        ("Read", None),
        ("", None),
    ],
)
def test_server_for_tool(tool_name, expected):
    assert server_for_tool(tool_name, KNOWN) == expected


def test_native_tools_are_not_this_check_s_business():
    assert scope_denial("Bash", "worker", ["git"], KNOWN) is None


def test_scoped_server_is_allowed():
    assert scope_denial("mcp__git__git_log", "worker", ["git"], KNOWN) is None


def test_server_outside_the_role_scope_is_denied():
    reason = scope_denial("mcp__github__create_pr", "worker", ["git"], KNOWN)
    assert reason is not None and "github" in reason


def test_server_nobody_registered_is_denied():
    """A host-level MCP config or a resumed session can surface a server ANA
    never granted; it is refused rather than judged by the command rules."""
    reason = scope_denial("mcp__slack__post", "worker", ["git"], KNOWN)
    assert reason is not None and "unregistered" in reason


def test_lead_gets_no_mcp_tools():
    assert scope_denial("mcp__git__git_log", "lead", [], KNOWN) is not None
