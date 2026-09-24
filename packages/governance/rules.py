"""Rule evaluation for the governance gate.

Turns one CLI tool call into a verdict (allow / ask / deny) using the YAML
rule set, so adding or relaxing a rule never requires a code change — see
config/governance.yaml and docs/plans/2026-09-22-governance-gate.md.
"""
import json
import os
import re
import shlex
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml

ALLOW = "allow"
ASK = "ask"
DENY = "deny"
ACTIONS = (ALLOW, ASK, DENY)

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "governance.yaml"


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    action: str
    tools: tuple[str, ...]
    patterns: tuple[re.Pattern, ...]
    # One line a human reads on the approval card: what this rule is about, in
    # the words of the person who has to answer. The rule id and category are
    # for querying the audit log; they do not explain why the machine stopped.
    description: str = ""

    def matches(self, tool_name: str, haystack: str) -> bool:
        # An empty `tools` list means the rule applies to every tool.
        if self.tools and tool_name.lower() not in self.tools:
            return False
        return any(pattern.search(haystack) for pattern in self.patterns)


@dataclass(frozen=True)
class Verdict:
    action: str
    rule_id: Optional[str] = None
    category: Optional[str] = None
    reason: str = ""
    # The matched rule's own description, passed through to the approval card.
    description: str = ""


class RuleSet:
    def __init__(self, rules: list[Rule], default_action: str = ALLOW):
        if default_action not in ACTIONS:
            raise ValueError(f"unknown default_action: {default_action!r}")
        self.rules = rules
        self.default_action = default_action

    @classmethod
    def from_yaml(cls, path: os.PathLike | str) -> "RuleSet":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        rules: list[Rule] = []
        for entry in raw.get("rules", []):
            action = entry.get("action")
            if action not in ACTIONS:
                raise ValueError(f"rule {entry.get('id')!r} has unknown action {action!r}")
            rules.append(
                Rule(
                    id=entry["id"],
                    category=entry.get("category", "uncategorized"),
                    action=action,
                    tools=tuple(t.lower() for t in entry.get("tools", [])),
                    patterns=tuple(
                        # Patterns are authored for readability, not for
                        # matching a specific letter case: a command is just
                        # as destructive in upper case.
                        re.compile(p, re.IGNORECASE)
                        for p in entry.get("patterns", [])
                    ),
                    description=str(entry.get("description", "")),
                )
            )
        return cls(rules, default_action=raw.get("default_action", ALLOW))

    def evaluate(self, tool_name: str, tool_input: dict) -> Verdict:
        haystack = _searchable_text(tool_input)
        for rule in self.rules:
            if rule.matches(tool_name, haystack):
                if rule.id == "network-egress" and _is_loopback_curl(haystack):
                    continue
                return Verdict(
                    action=rule.action,
                    rule_id=rule.id,
                    category=rule.category,
                    reason=f"{rule.id} ({rule.category})",
                    description=rule.description,
                )
        return Verdict(action=self.default_action, reason="no rule matched")


def _searchable_text(tool_input: dict) -> str:
    """The text rules are matched against: a shell tool's command on its own,
    and otherwise the whole input serialized, so a rule can still catch, say,
    a path inside a file-write tool's arguments."""
    command = tool_input.get("command")
    if isinstance(command, str):
        return command
    return json.dumps(tool_input, ensure_ascii=False)


def _is_loopback_curl(command: str) -> bool:
    """Exempt a curl call whose explicit HTTP targets stay on this machine.

    A mixed command, proxy, redirect, or curl config may contact another
    machine, so those still take the normal network approval path.
    """
    if len(re.findall(r"\b(curl|wget|nc|netcat|telnet|ssh|scp|rsync)\b", command, re.I)) != 1:
        return False
    if re.search(r"\b(?:pip3?|npm|pnpm|yarn|cargo|go)\s+(?:install|add|get)\b|\bgit\s+(?:push|clone|fetch|pull)\b", command, re.I):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if "curl" not in tokens:
        return False
    if any(
        token.startswith(("--location", "--proxy", "--config", "--connect-to", "--resolve"))
        or re.fullmatch(r"-[A-Za-z]*[LxK][A-Za-z]*", token)
        for token in tokens
    ):
        return False
    targets = [
        token.split("=", 1)[-1] if token.startswith("--url=") else token
        for token in tokens
        if token.startswith(("http://", "https://", "--url="))
    ]
    if not targets:
        return False
    for target in targets:
        try:
            host = urlsplit(target).hostname
        except ValueError:
            return False
        if host == "localhost":
            continue
        try:
            if host is None or not ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    return True


@lru_cache(maxsize=4)
def _cached_ruleset(path: str, mtime: float) -> RuleSet:
    return RuleSet.from_yaml(path)


def load_ruleset(path: os.PathLike | str | None = None) -> RuleSet:
    """Loads the rule set, re-reading it whenever the file changes.

    Keyed on mtime rather than cached once at import: the roadmap's whole
    point in putting rules in YAML is being able to add one without a code
    change, and that is worth little if the running server needs a restart to
    see it."""
    resolved = str(path or os.environ.get("ANA_GOVERNANCE_RULES") or DEFAULT_RULES_PATH)
    return _cached_ruleset(resolved, os.path.getmtime(resolved))
