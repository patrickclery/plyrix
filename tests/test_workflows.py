"""Hold every GitHub Actions workflow to the pinning, permission and trigger rules (D-16).

Each rule is a function over a workflow's raw text or parsed YAML, so the planted-text controls
below run the same code the real files do.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no stubs

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
PYTHONS = ["3.12", "3.13", "3.14", "3.15"]

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+#\s*v\d+\.\d+\.\d+\s*$")
EXPRESSION = re.compile(r"\$\{\{")


def workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def load(text: str) -> dict[Any, Any]:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def triggers(workflow: dict[Any, Any]) -> Any:
    """PyYAML reads the bare key `on` as the boolean True, so look under both."""
    return workflow.get("on", workflow.get(True))


def trigger_names(workflow: dict[Any, Any]) -> set[str]:
    on = triggers(workflow)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    assert isinstance(on, dict), on
    return set(on)


def unpinned_uses(text: str) -> list[str]:
    """Return each `uses:` line that isn't a full-SHA pin followed by a `# vX.Y.Z` comment."""
    problems = []
    for line in text.splitlines():
        match = USES.match(line)
        if match is None:
            continue
        ref = match.group("ref").strip("'\"")
        if ref.startswith("./"):
            continue
        if not PINNED.match(ref) or not VERSION_COMMENT.match(match.group("rest")):
            problems.append(line.strip())
    return problems


def permission_problems(workflow: dict[Any, Any]) -> list[str]:
    problems = []
    if "permissions" not in workflow:
        problems.append("no top-level permissions")
    else:
        top = workflow["permissions"]
        if top not in ({}, {"contents": "read"}):
            problems.append(f"top-level permissions grant {top!r}")
    for name, job in (workflow.get("jobs") or {}).items():
        perms = job.get("permissions")
        if perms in ("write-all", "read-all") or (isinstance(perms, str)):
            problems.append(f"job {name} grants {perms!r}")
    if "pull_request_target" in trigger_names(workflow):
        problems.append("triggers on pull_request_target")
    return problems


def steps(workflow: dict[Any, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, step)
        for name, job in (workflow.get("jobs") or {}).items()
        for step in job.get("steps") or []
    ]


def checkout_problems(workflow: dict[Any, Any]) -> list[str]:
    problems = []
    for name, step in steps(workflow):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            if (step.get("with") or {}).get("persist-credentials") is not False:
                problems.append(f"job {name}: checkout keeps credentials")
    return problems


def expression_in_run(workflow: dict[Any, Any]) -> list[str]:
    return [
        f"job {name}: {step.get('name', step['run'][:40])}"
        for name, step in steps(workflow)
        if "run" in step and EXPRESSION.search(str(step["run"]))
    ]


def test_there_is_a_ci_workflow() -> None:
    assert CI.is_file()
    assert CI in workflow_files()


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_every_action_is_pinned_by_full_sha(path: Path) -> None:
    assert unpinned_uses(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_permissions_are_read_only_at_the_top(path: Path) -> None:
    assert permission_problems(load(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_checkouts_drop_credentials(path: Path) -> None:
    assert checkout_problems(load(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
def test_no_expression_inside_a_run_script(path: Path) -> None:
    assert expression_in_run(load(path.read_text(encoding="utf-8"))) == []


def test_ci_runs_the_one_test_command_on_every_python() -> None:
    workflow = load(CI.read_text(encoding="utf-8"))
    assert {"push", "pull_request"} <= trigger_names(workflow)
    assert triggers(workflow)["push"]["branches"] == ["main"]
    job = workflow["jobs"]["test"]
    assert job["permissions"] == {"contents": "read"}
    assert job["strategy"]["fail-fast"] is False
    assert [str(v) for v in job["strategy"]["matrix"]["python-version"]] == PYTHONS
    runs = [str(step["run"]).strip() for step in job["steps"] if "run" in step]
    assert "uv sync --locked" in runs
    assert "uv run pytest" in runs
    assert runs.index("uv sync --locked") < runs.index("uv run pytest")


def test_ci_installs_a_checksummed_gitleaks() -> None:
    text = CI.read_text(encoding="utf-8")
    assert "gitleaks_8.30.1_linux_x64.tar.gz" in text
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in text
    assert "sha256sum -c" in text
    assert "ffmpeg" in text


# Controls: planted text each rule must refuse.

GOOD_USES = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4\n",
        "      - uses: actions/checkout@3d3c42e # v7.0.1\n",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # main\n",
        "        uses: actions/checkout@main\n",
    ],
    ids=["tag", "short-sha", "no-comment", "branch-comment", "branch"],
)
def test_pin_rule_control(line: str) -> None:
    assert unpinned_uses(line) != []


def test_pin_rule_control_passes_a_pin() -> None:
    assert unpinned_uses(GOOD_USES) == []


@pytest.mark.parametrize(
    "text",
    [
        "on: push\njobs: {}\n",
        "on: push\npermissions: write-all\njobs: {}\n",
        "on: push\npermissions:\n  contents: write\njobs: {}\n",
        "on: push\npermissions: {}\njobs:\n  a:\n    permissions: write-all\n",
        "on: [pull_request_target]\npermissions: {}\njobs: {}\n",
    ],
    ids=["missing", "write-all", "contents-write", "job-write-all", "pr-target"],
)
def test_permission_rule_control(text: str) -> None:
    assert permission_problems(load(text)) != []


def test_permission_rule_control_passes_empty() -> None:
    assert permission_problems(load("on:\n  push:\npermissions: {}\njobs: {}\n")) == []


def test_checkout_rule_control() -> None:
    text = (
        "on: push\npermissions: {}\njobs:\n  a:\n    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
    )
    assert checkout_problems(load(text)) != []


def test_expression_rule_control() -> None:
    text = (
        "on: push\npermissions: {}\njobs:\n  a:\n    steps:\n      - run: echo ${{ github.ref }}\n"
    )
    assert expression_in_run(load(text)) != []


# CI's other jobs, the scheduled audit and Dependabot (05-03)

AUDIT = WORKFLOWS / "audit.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


def ci() -> dict[Any, Any]:
    return load(CI.read_text(encoding="utf-8"))


def runs_of(job: dict[str, Any]) -> list[str]:
    return [" ".join(str(step["run"]).split()) for step in job["steps"] if "run" in step]


def uses_of(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if str(step.get("uses", "")).startswith(f"{action}@")]


def test_ci_has_four_read_only_jobs_with_timeouts() -> None:
    jobs = ci()["jobs"]
    assert set(jobs) == {"test", "lint", "scan", "image"}
    for name, job in jobs.items():
        assert job["permissions"] == {"contents": "read"}, name
        assert isinstance(job["timeout-minutes"], int), name


def test_the_lint_job_runs_every_static_gate() -> None:
    runs = runs_of(ci()["jobs"]["lint"])
    expected = [
        "uv sync --locked",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src tests",
        "uv run lint-imports",
    ]
    assert [r for r in runs if r in expected] == expected


def test_the_scan_job_runs_both_scans_and_zizmor_with_default_rules() -> None:
    job = ci()["jobs"]["scan"]
    checkout = uses_of(job, "actions/checkout")
    assert checkout and checkout[0]["with"]["fetch-depth"] == 0
    runs = runs_of(job)
    assert "scripts/scan-private.sh tree" in runs
    assert "scripts/scan-private.sh history" in runs
    assert any("sha256sum -c" in r for r in runs)
    zizmor = [step for step in job["steps"] if "uvx zizmor==1.32.0" in str(step.get("run", ""))]
    assert len(zizmor) == 1
    assert ".github/workflows" in zizmor[0]["run"]
    assert "GH_TOKEN" in zizmor[0]["env"]
    # D-08: the private deny-list never reaches CI.
    text = CI.read_text(encoding="utf-8")
    assert "PLYRIX_DENY_LIST" not in text
    assert "deny-list" not in text


def test_the_image_job_builds_loads_gates_and_scans_without_pushing() -> None:
    job = ci()["jobs"]["image"]
    build = uses_of(job, "docker/build-push-action")
    assert len(build) == 1
    options = build[0]["with"]
    assert options["platforms"] == "linux/amd64"
    assert options["load"] is True
    assert options["push"] is False
    assert options["tags"] == "plyrix:local"
    assert options["provenance"] is False
    assert options["sbom"] is False
    runs = runs_of(job)
    assert "PLYRIX_DOCKER_GATE=1 uv run pytest tests/gates/test_image.py -m docker_image" in runs
    grype = uses_of(job, "anchore/scan-action")
    assert len(grype) == 1
    assert grype[0]["with"]["image"] == "plyrix:local"
    assert grype[0]["with"]["fail-build"] is True
    assert grype[0]["with"]["severity-cutoff"] == "high"
    assert grype[0]["with"]["only-fixed"] is True


def test_every_grype_ignore_is_one_cve_at_one_package_version() -> None:
    # A broad ignore would hollow out the image gate, so each one is pinned to the exact build.
    config = yaml.safe_load((ROOT / ".grype.yaml").read_text(encoding="utf-8"))
    assert set(config) == {"ignore"}
    for rule in config["ignore"]:
        assert re.fullmatch(r"CVE-\d{4}-\d+", rule["vulnerability"]), rule
        assert set(rule) == {"vulnerability", "package"}, rule
        assert set(rule["package"]) == {"name", "version"}, rule
        assert rule["package"]["name"] and str(rule["package"]["version"]), rule


def test_ci_never_logs_in_or_pushes() -> None:
    workflow = ci()
    for name, step in steps(workflow):
        assert not str(step.get("uses", "")).startswith("docker/login-action@"), name
        assert (step.get("with") or {}).get("push") is not True, name


def test_the_audit_runs_on_a_schedule_with_hashes() -> None:
    workflow = load(AUDIT.read_text(encoding="utf-8"))
    assert trigger_names(workflow) == {"schedule", "workflow_dispatch"}
    assert triggers(workflow)["schedule"][0]["cron"]
    runs = [r for job in workflow["jobs"].values() for r in runs_of(job)]
    assert any("uv export --locked --no-dev --no-emit-project" in r for r in runs)
    audit = [r for r in runs if "uvx pip-audit==2.10.1" in r]
    assert audit and "--require-hashes" in audit[0]


def test_dependabot_covers_uv_docker_and_actions_weekly() -> None:
    data = load(DEPENDABOT.read_text(encoding="utf-8"))
    assert data["version"] == 2
    root = {u["package-ecosystem"]: u for u in data["updates"] if u["directory"] == "/"}
    assert {"uv", "docker", "github-actions"} <= set(root)
    for update in data["updates"]:
        assert update["schedule"]["interval"] == "weekly"


# The project site's workflow (05-05, D-22)

PAGES = WORKFLOWS / "pages.yml"
PAGES_PATHS = ["site/**", "README.md", "assets/brand/**", ".github/workflows/pages.yml"]


def test_pages_builds_on_relevant_pushes_and_prs_and_by_hand() -> None:
    workflow = load(PAGES.read_text(encoding="utf-8"))
    on = triggers(workflow)
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["paths"] == PAGES_PATHS
    assert on["pull_request"]["paths"] == PAGES_PATHS


def test_pages_build_job_builds_checks_and_sends_no_telemetry() -> None:
    job = load(PAGES.read_text(encoding="utf-8"))["jobs"]["build"]
    assert job["permissions"] == {"contents": "read"}
    assert job["env"]["ASTRO_TELEMETRY_DISABLED"] == "1"
    astro = uses_of(job, "withastro/action")
    assert len(astro) == 1
    assert astro[0]["with"]["path"] == "site"
    assert astro[0]["with"]["cache"] == "false"
    runs = runs_of(job)
    assert "node --test 'site/scripts/*.test.mjs'" in runs
    assert "python3 scripts/check_public_text.py --html site/dist" in runs
    assert "python3 scripts/check_public_text.py site/src" in runs


def test_pages_deploys_only_from_main_once_patrick_turns_it_on() -> None:
    job = load(PAGES.read_text(encoding="utf-8"))["jobs"]["deploy"]
    assert job["needs"] in ("build", ["build"])
    condition = " ".join(job["if"].split())
    assert "github.event_name != 'pull_request'" in condition
    assert "github.ref == 'refs/heads/main'" in condition
    assert "vars.PLYRIX_PAGES == 'on'" in condition
    assert job["permissions"] == {"pages": "write", "id-token": "write"}
    assert job["environment"]["name"] == "github-pages"
    assert len(uses_of(job, "actions/deploy-pages")) == 1


def test_dependabot_covers_the_site_npm_packages() -> None:
    data = load(DEPENDABOT.read_text(encoding="utf-8"))
    npm = [u for u in data["updates"] if u["package-ecosystem"] == "npm"]
    assert len(npm) == 1
    assert npm[0]["directory"] == "/site"
    assert npm[0]["schedule"]["interval"] == "weekly"
