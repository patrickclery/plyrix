"""Hold the governance files and issue forms to D-15: their content and one private contact."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no stubs

ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
SECURITY = ROOT / "SECURITY.md"
SUPPORT = ROOT / "SUPPORT.md"
CONDUCT = ROOT / "CODE_OF_CONDUCT.md"
GOVERNANCE = [CONTRIBUTING, SECURITY, SUPPORT, CONDUCT]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
BUG = TEMPLATES / "bug.yml"
FEATURE = TEMPLATES / "feature.yml"
QUESTION = TEMPLATES / "question.yml"
CONFIG = TEMPLATES / "config.yml"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
FORM_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}

# The one private contact route. Patrick chose GitHub only in 05-06, and no takedown route: plyrix
# ships no lyrics, so questions and concerns go in a normal issue.
CONTACT = "https://github.com/patrickclery/plyrix/security/advisories/new"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def flat(path: Path) -> str:
    """The text with line breaks folded, so a phrase wrapped across lines still matches."""
    return " ".join(text(path).split())


@pytest.mark.parametrize("path", GOVERNANCE, ids=lambda p: p.name)
def test_each_file_exists_as_utf8_text(path: Path) -> None:
    assert path.is_file()
    assert "\0" not in text(path)
    assert text(path).strip()


@pytest.mark.parametrize("path", GOVERNANCE, ids=lambda p: p.name)
def test_no_file_holds_an_email_address(path: Path) -> None:
    assert EMAIL.findall(text(path)) == []


def test_email_rule_control() -> None:
    assert EMAIL.findall("write to someone@example.org today") != []


@pytest.mark.parametrize("path", [SECURITY, SUPPORT, CONDUCT], ids=lambda p: p.name)
def test_the_contact_route_is_the_same_everywhere(path: Path) -> None:
    assert CONTACT in text(path)


def test_contributing_names_the_one_test_command_and_the_gates() -> None:
    body = text(CONTRIBUTING)
    for command in [
        "uv sync",
        "uv run pytest",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src tests",
        "uv run lint-imports",
        "scripts/install-hooks.sh",
        "scripts/make_fixtures.sh",
    ]:
        assert f"`{command}`" in body, command


def test_contributing_states_the_safety_rules() -> None:
    body = flat(CONTRIBUTING).lower()
    assert "never a live library" in body
    assert "copies" in body
    assert "no lyrics" in body
    assert "hosted ci" in body
    assert "tag-writing" in body
    for marker in ["real_samples", "docker_image"]:
        assert marker in body


def test_security_names_private_reporting_and_the_supported_release() -> None:
    body = flat(SECURITY).lower()
    assert "private vulnerability reporting" in body
    assert "latest minor release" in body
    assert "plex.tv" in body
    assert "token" in body


def test_support_is_best_effort_for_the_latest_minor_release() -> None:
    body = flat(SUPPORT).lower()
    assert "best effort" in body
    assert "one maintainer" in body
    assert "latest minor release" in body
    assert "plyrix doctor" in body
    assert "--verbose" in body


def test_the_code_of_conduct_is_contributor_covenant_2_1_with_no_placeholder() -> None:
    body = text(CONDUCT)
    assert body.lstrip().startswith("# Contributor Covenant Code of Conduct")
    assert "version 2.1" in body
    assert "https://www.contributor-covenant.org/version/2/1/code_of_conduct.html" in body
    assert "[INSERT" not in body


# Issue forms and the PR template (D-15)


def form(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(text(path))
    assert isinstance(data, dict)
    return data


def body_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    body = data["body"]
    assert isinstance(body, list)
    for item in body:
        assert item["type"] in FORM_TYPES, item
        if item["type"] != "markdown":
            assert item.get("id"), item
        assert "attributes" in item, item
    return body


def by_id(body: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in body if "id" in item}


def required(item: dict[str, Any]) -> bool:
    return bool((item.get("validations") or {}).get("required"))


def test_blank_issues_are_off_with_private_and_legal_routes() -> None:
    data = form(CONFIG)
    assert data["blank_issues_enabled"] is False
    urls = {link["url"] for link in data["contact_links"]}
    assert CONTACT in urls
    assert "https://github.com/patrickclery/plyrix#lyrics-and-legal" in urls
    assert "https://github.com/patrickclery/plyrix/blob/main/SUPPORT.md" in urls
    for link in data["contact_links"]:
        assert link["name"] and link["about"]


def test_the_bug_form_warns_and_asks_for_doctor_and_counts() -> None:
    data = form(BUG)
    assert data["name"] and data["description"]
    assert "bug" in data["labels"]
    body = body_of(data)
    warnings = " ".join(i["attributes"]["value"] for i in body if i["type"] == "markdown").lower()
    for word in ["lyrics", "tokens", "private paths", "library listings"]:
        assert word in warnings, word

    fields = by_id(body)
    for name in ["doctor", "counts"]:
        assert fields[name]["type"] == "textarea"
        assert fields[name]["attributes"]["render"] == "text"
        assert required(fields[name])
    assert "plyrix doctor" in fields["doctor"]["attributes"]["label"]
    assert fields["install"]["type"] == "dropdown"
    assert fields["install"]["attributes"]["options"] == [
        "container image",
        "PyPI package",
        "checkout",
    ]
    assert fields["version"]["type"] == "input"
    confirm = fields["removed"]
    assert confirm["type"] == "checkboxes"
    assert all(option["required"] is True for option in confirm["attributes"]["options"])


def test_the_feature_form_keeps_plyrix_local_and_read_only() -> None:
    data = form(FEATURE)
    assert "enhancement" in data["labels"]
    body = body_of(data)
    words = " ".join(
        str(i["attributes"].get("value", "")) + str(i["attributes"].get("description", ""))
        for i in body
    ).lower()
    assert "local-only" in words
    assert "never writes audio" in words


def test_the_pr_template_holds_the_checklist() -> None:
    body = flat(PR_TEMPLATE).lower()
    assert "- [ ]" in body
    assert "`uv run pytest`" in body
    assert "no lyrics" in body
    assert "private data" in body
    assert "audio" in body
    assert "measured evidence" in body


def test_the_question_form_takes_questions_and_concerns() -> None:
    data = form(QUESTION)
    assert data["name"] and data["description"]
    assert "question" in data["labels"]
    body = body_of(data)
    warnings = " ".join(i["attributes"]["value"] for i in body if i["type"] == "markdown")
    assert "https://lrclib.net/db-dumps" in warnings
    assert CONTACT in warnings
    for word in ["lyrics", "tokens", "private paths", "library listings"]:
        assert word in warnings.lower(), word
    assert required(by_id(body)["question"])


def test_support_points_questions_at_the_question_form() -> None:
    assert "question form" in flat(SUPPORT)


ALL_TEMPLATES = [BUG, FEATURE, QUESTION, CONFIG, PR_TEMPLATE]


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.name)
def test_no_template_holds_an_email_address(path: Path) -> None:
    assert EMAIL.findall(text(path)) == []


@pytest.mark.parametrize(
    "path", [*GOVERNANCE, *ALL_TEMPLATES, ROOT / "README.md"], ids=lambda p: p.name
)
def test_no_public_file_offers_a_takedown_route(path: Path) -> None:
    # 05-06 decision B: plyrix ships no lyrics, so it has no takedown section, contact or label.
    assert "takedown" not in text(path).lower()
