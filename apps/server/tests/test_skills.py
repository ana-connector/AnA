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

    assert "SKILLS YOUR SUB-AGENTS CAN USE" in catalog
    assert "git-commit" in catalog
    assert "Do not carry one out yourself" in catalog


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


def test_the_working_style_skills_render_without_arguments():
    """caveman, ponytail and kill-ai-slop are vendored SKILL.md files: they
    declare no inputs, so a plan that names one of them gets the whole procedure
    with nothing left to substitute."""
    for name in ("caveman", "ponytail", "kill-ai-slop"):
        rendered = render_skill(name, {})
        assert "{{" not in rendered
        assert len(rendered) > 2000


def test_a_skill_md_loads_with_its_body_as_the_procedure(tmp_path, monkeypatch):
    """Skills exist in the world as SKILL.md — frontmatter plus a body. Reading
    that form directly is what keeps a vendored skill from drifting into a
    summary of itself."""
    monkeypatch.setenv("ANA_SKILLS_DIR", str(tmp_path))
    (tmp_path / "vendored.md").write_text(
        textwrap.dedent(
            """
            ---
            name: vendored
            title: 가져온 스킬
            description: one line for the catalog
            roles: [worker]
            ---

            # Vendored

            Step one. Step two.
            """
        ).strip(),
        encoding="utf-8",
    )

    library = load_library()
    skill = library.get("vendored")

    assert skill is not None
    assert skill.title == "가져온 스킬"
    assert skill.instructions.startswith("# Vendored")
    assert "Step two." in skill.instructions


def test_markdown_without_frontmatter_is_reported_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("ANA_SKILLS_DIR", str(tmp_path))
    (tmp_path / "loose.md").write_text("just some notes", encoding="utf-8")

    library = load_library()

    assert library.get("loose") is None
    assert any("loose.md" in error.path for error in library.errors)


def test_the_vendored_skills_keep_their_upstream_bodies():
    """caveman, ponytail and kill-ai-slop are real published skills. They are
    shipped as their own SKILL.md rather than a paraphrase, so this checks the
    body is the upstream text and not an ANA summary of it."""
    library = load_library()

    ponytail = library.get("ponytail")
    caveman = library.get("caveman")
    slop = library.get("kill-ai-slop")

    assert ponytail is not None and "The shortest path to done" in ponytail.instructions
    assert caveman is not None and "Respond terse like smart caveman" in caveman.instructions
    assert slop is not None and "Decide before you decorate" in slop.instructions
    # The upstream bodies are long; that is the point — the catalog only carries
    # the one-line description, and the body is pulled in when a task names it.
    assert len(ponytail.instructions) > 3000
