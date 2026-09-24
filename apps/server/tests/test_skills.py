"""The skill library: schema validation, rendering, and the catalog an agent
sees in its system prompt."""
import textwrap

import pytest
from pydantic import ValidationError

from packages.skills.catalog import catalog_text, render_skill, with_catalog
from packages.skills.loader import SkillLibrary, load_library
from packages.skills.models import SkillDefinition

VALID = textwrap.dedent(
    """
    name: sample-skill
    title: 샘플
    description: 테스트용 스킬
    roles: [worker]
    mcp: [git]
    inputs:
      - name: target
        type: string
        required: true
      - name: limit
        type: integer
        required: false
        default: 10
    instructions: |
      {{target}} 를 {{limit}}건까지 살펴라.
    """
)


@pytest.fixture
def user_library(tmp_path, monkeypatch):
    directory = tmp_path / "skills"
    directory.mkdir()
    monkeypatch.setenv("ANA_SKILLS_DIR", str(directory))
    return directory


def _skill(**overrides) -> SkillDefinition:
    import yaml

    payload = yaml.safe_load(VALID)
    payload.update(overrides)
    return SkillDefinition(**payload)


def test_inputs_are_validated_against_the_declared_schema():
    skill = _skill()
    assert "docs 를 10건까지" in skill.render({"target": "docs"})
    with pytest.raises(ValidationError):
        skill.render({})  # target is required
    with pytest.raises(ValidationError):
        skill.render({"target": "docs", "extra": 1})  # undeclared input


def test_instructions_cannot_reference_an_undeclared_input():
    with pytest.raises(ValueError, match="undeclared"):
        _skill(instructions="{{nope}} 를 해라")


def test_skill_name_must_be_kebab_case():
    with pytest.raises(ValueError, match="kebab-case"):
        _skill(name="Sample Skill")


def test_a_broken_file_is_reported_not_fatal(user_library):
    (user_library / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
    library = load_library()
    assert library.get("broken") is None
    assert any("broken.yaml" in error.path for error in library.errors)
    # The shipped skills still loaded.
    assert library.get("code-review") is not None


def test_user_file_overrides_a_shipped_skill(user_library):
    (user_library / "code-review.yaml").write_text(
        textwrap.dedent(
            """
            name: code-review
            description: 내 방식대로 리뷰
            instructions: 그냥 읽어라
            """
        ),
        encoding="utf-8",
    )
    assert load_library().get("code-review").description == "내 방식대로 리뷰"


def test_catalog_lists_only_skills_whose_mcp_servers_are_present(user_library):
    with_git = catalog_text("worker", {"git"})
    without = catalog_text("worker", set())
    assert "git-commit" in with_git
    # Offering a skill whose server is not scoped just walks the agent into a
    # gate denial.
    assert "git-commit" not in without
    assert "code-review" in without  # needs no MCP server


def test_the_lead_sees_the_catalog_as_its_sub_agents_capabilities(user_library):
    """The CLI's native sub-agents run inside the lead's process and inherit
    its system prompt, so a catalog withheld from the lead is one no sub-agent
    ever sees. It is labeled as theirs, not the lead's own."""
    catalog = catalog_text("lead", {"git", "postgres", "github"})

    assert "하위 에이전트가 쓸 수 있는 스킬" in catalog
    assert "git-commit" in catalog
    assert "직접\n실행하지는 마라" in catalog


def test_with_catalog_appends_rather_than_replaces(user_library):
    combined = with_catalog("역할 지시", "worker", set())
    assert combined.startswith("역할 지시")
    assert "code-review" in combined


def test_with_catalog_is_a_no_op_when_nothing_applies(user_library, monkeypatch):
    """No skills in the library at all — an empty catalog is left out rather
    than added as a header with nothing under it."""
    monkeypatch.setattr("packages.skills.catalog.load_library", lambda: SkillLibrary())

    assert with_catalog("역할 지시", "worker", set()) == "역할 지시"


def test_render_skill_by_name(user_library):
    rendered = render_skill("code-review", {"target": "packages/mcp"})
    assert "packages/mcp" in rendered
    with pytest.raises(KeyError):
        render_skill("no-such-skill")


def test_shipped_skills_all_load():
    library = load_library()
    assert library.errors == ()
    assert {skill.name for skill in library.skills} >= {
        "caveman",
        "code-review",
        "db-inspect",
        "git-commit",
        "github-pr",
        "kill-ai-slop",
        "ponytail",
    }


def test_the_working_style_skills_render_with_their_defaults():
    """caveman, ponytail and kill-ai-slop take a level or a flag whose default
    is the usual one, so a plan that names the skill without arguments still
    produces complete instructions rather than a "{{level}}" literal."""
    for name, inputs in (
        ("caveman", {"subject": "테스트 결과"}),
        ("ponytail", {"task": "삭제 버그 수정"}),
        ("kill-ai-slop", {"target": "apps/web/src"}),
    ):
        rendered = render_skill(name, inputs)
        assert "{{" not in rendered
        assert len(rendered) > 200
