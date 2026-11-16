"""Hold the tag-driven release to D-17, D-18 and D-19, and prove publish-check's release mode.

The workflow rules read release.yml. The release-mode proofs run on a clone of this repo, so the
full gate runs on real files; `origin` in the clone is a local path, so nothing touches a network.
"""

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no stubs

import plyrix
from test_repo_scan import base_env

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
RELEASING = ROOT / "RELEASING.md"
OVERRIDE = ROOT / "examples" / "compose.release.yaml"
ORDER = ["verify", "dist", "pypi", "image", "github-release"]
# The clone holds this repo's tests, which hold test_repo_scan's planted term, so its deny-list
# plants a term built here, which appears in no file as one word.
CLONE_TERM = "zz" + "releasegate" + "term"
CLONE_DENY_LIST = f"""\
[extend]
useDefault = true

[[rules]]
id = "planted"
description = "planted placeholder term"
regex = "{CLONE_TERM}"
keywords = ["{CLONE_TERM}"]
"""
IMAGE = "ghcr.io/patrickclery/plyrix"
NESTED = "PLYRIX_RELEASE_GATE_PROOF"


def release() -> dict[Any, Any]:
    data = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def jobs() -> dict[str, dict[str, Any]]:
    result = release()["jobs"]
    assert isinstance(result, dict)
    return result


def step_uses(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [s for s in job["steps"] if str(s.get("uses", "")).startswith(f"{action}@")]


def runs(job: dict[str, Any]) -> list[str]:
    return [" ".join(str(s["run"]).split()) for s in job["steps"] if "run" in s]


def pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data["project"]["version"]
    assert isinstance(version, str)
    return version


def test_release_runs_only_on_a_version_tag_push() -> None:
    data = release()
    on = data.get("on", data.get(True))
    assert on == {"push": {"tags": ["v*.*.*"]}}
    assert data["permissions"] == {}
    assert data["concurrency"]["cancel-in-progress"] is False


def test_the_jobs_run_in_order() -> None:
    found = jobs()
    assert list(found) == ORDER
    assert "needs" not in found["verify"]
    for before, after in zip(ORDER, ORDER[1:], strict=False):
        assert found[after]["needs"] in (before, [before]), after


def test_each_job_holds_only_the_permission_it_needs() -> None:
    found = jobs()
    granted = {
        name: {k for k, v in (job.get("permissions") or {}).items() if v == "write"}
        for name, job in found.items()
    }
    assert granted == {
        "verify": set(),
        "dist": set(),
        "pypi": {"id-token"},
        "image": {"packages"},
        "github-release": {"contents"},
    }
    assert found["pypi"]["environment"]["name"] == "pypi"
    for name, job in found.items():
        assert job.get("environment") is None or name == "pypi"


def test_no_release_job_uses_a_cache() -> None:
    for name, job in jobs().items():
        for step in step_uses(job, "astral-sh/setup-uv"):
            assert step["with"]["enable-cache"] is False, name
        for step in step_uses(job, "docker/build-push-action"):
            assert "cache-from" not in step["with"], name


def test_verify_checks_the_tag_and_runs_the_suite_on_3_14() -> None:
    job = jobs()["verify"]
    text = " ".join(runs(job))
    assert "git cat-file -t" in text
    assert "uv version --short" in text
    commands = runs(job)
    for command in ["uv lock --check", "uv sync --locked", "uv run pytest"]:
        assert command in commands
    assert step_uses(job, "astral-sh/setup-uv")[0]["with"]["python-version"] == "3.14"


def test_dist_builds_and_checks_and_pypi_publishes_by_trusted_publishing() -> None:
    found = jobs()
    commands = runs(found["dist"])
    assert "uv build" in commands
    assert "uvx twine==7.0.0 check --strict dist/*" in commands
    assert step_uses(found["dist"], "actions/upload-artifact")
    assert step_uses(found["pypi"], "actions/download-artifact")
    publish = step_uses(found["pypi"], "pypa/gh-action-pypi-publish")
    assert len(publish) == 1
    options = publish[0]["with"]
    assert options["attestations"] is True
    assert not {"password", "user", "repository-url"} & set(options)


def test_the_image_is_multi_arch_attested_and_never_latest() -> None:
    job = jobs()["image"]
    assert step_uses(job, "docker/setup-qemu-action")
    assert step_uses(job, "docker/setup-buildx-action")
    login = step_uses(job, "docker/login-action")
    assert len(login) == 1 and login[0]["with"]["registry"] == "ghcr.io"
    assert "GITHUB_TOKEN" in login[0]["with"]["password"]
    build = step_uses(job, "docker/build-push-action")
    assert len(build) == 1
    options = build[0]["with"]
    assert options["platforms"] == "linux/amd64,linux/arm64"
    assert options["push"] is True
    assert options["provenance"] == "mode=max"
    assert options["sbom"] is True
    tags_step = [s for s in job["steps"] if s.get("id") == "tags"]
    assert len(tags_step) == 1
    assert "GITHUB_REF_NAME" in tags_step[0]["run"]
    assert f"{IMAGE}:$version" in tags_step[0]["run"]
    assert f"{IMAGE}:$minor" in tags_step[0]["run"]
    assert step_uses(job, "anchore/scan-action")


def test_nothing_publishes_latest_lrclib_npm_or_cargo() -> None:
    # The runner label is the only `latest` allowed.
    text = RELEASE.read_text(encoding="utf-8").lower().replace("runs-on: ubuntu-latest", "")
    assert "latest" not in text
    assert "lrclib" not in text
    assert "npm publish" not in text
    assert "cargo publish" not in text
    assert "release-please" not in text


def test_the_github_release_uses_the_tag_message_as_notes() -> None:
    job = jobs()["github-release"]
    commands = runs(job)
    assert 'gh release create "$TAG" dist/* --verify-tag --notes-from-tag' in commands
    step = next(s for s in job["steps"] if "gh release create" in str(s.get("run", "")))
    assert set(step["env"]) >= {"TAG", "GH_TOKEN"}


def test_the_versions_agree() -> None:
    assert plyrix.__version__ == pyproject_version()
    match = re.search(rf"image: {re.escape(IMAGE)}:(\S+)", OVERRIDE.read_text(encoding="utf-8"))
    assert match is not None
    assert match.group(1) == pyproject_version()


def test_releasing_documents_the_hand_procedure() -> None:
    text = " ".join(RELEASING.read_text(encoding="utf-8").split())
    for phrase in [
        "uv version --bump",
        "uv lock",
        "chore(release): vX.Y.Z",
        "scripts/publish-check.sh --release vX.Y.Z",
        "src/plyrix/__init__.py",
        "examples/compose.release.yaml",
        "v0.1.0",
        "release-please",
        "pypi",
    ]:
        assert phrase in text, phrase
    assert "RELEASING.md" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


# publish-check.sh --release, on a clone of this repo


def uv_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}}


class Clone:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=self.path, env=self.env, capture_output=True, text=True, check=False
        )

    def git(self, *args: str) -> str:
        result = self.run("git", *args)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def release(self, tag: str) -> subprocess.CompletedProcess[str]:
        return self.run("scripts/publish-check.sh", "--release", tag)


def working_files() -> list[str]:
    """Tracked files and new unignored ones, as the working tree holds them now."""
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    names = [os.fsdecode(name) for name in listing.split(b"\0") if name]
    return [name for name in names if (ROOT / name).is_file()]


@pytest.fixture(scope="module")
def clone(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Clone]:
    if shutil.which("gitleaks") is None:
        reason = "gitleaks is not on PATH, so the release gate cannot be proved here"
        if os.environ.get("CI") or os.environ.get("PLYRIX_REQUIRE_GITLEAKS"):
            pytest.fail(reason)
        pytest.skip(reason)
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    # publish-check's full gate runs this file among its fast tests, inside the clone's own gate.
    if os.environ.get(NESTED):
        pytest.skip("already inside a release-gate proof")
    base = tmp_path_factory.mktemp("release")
    deny_list = base / "deny-list.toml"
    deny_list.write_text(CLONE_DENY_LIST, encoding="utf-8")
    path = base / "clone"
    env = uv_env({**base_env(), "PLYRIX_DENY_LIST": str(deny_list), NESTED: "1"})
    subprocess.run(
        ["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(path)], env=env, check=True
    )
    repo = Clone(path, env)
    # The working tree as it is now, so the proofs run on what is about to be committed.
    for name in working_files():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    repo.git("add", "-A")
    if repo.run("git", "diff", "--cached", "--quiet").returncode != 0:
        repo.git("commit", "-q", "--no-verify", "-m", "overlay the release files")
    yield repo


@pytest.fixture
def tags(clone: Clone) -> Iterator[None]:
    yield
    for tag in clone.git("tag", "-l").split():
        clone.git("tag", "-d", tag)


def test_release_mode_refuses_a_lightweight_tag(clone: Clone, tags: None) -> None:
    version = pyproject_version()
    clone.git("tag", f"v{version}")
    result = clone.release(f"v{version}")
    assert result.returncode == 1
    assert "not an annotated tag" in result.stderr


def test_release_mode_refuses_a_tag_not_at_head(clone: Clone, tags: None) -> None:
    version = pyproject_version()
    clone.git("tag", "-a", f"v{version}", "-m", "clean notes", "HEAD")
    clone.git("commit", "--allow-empty", "-q", "-m", "after the tag")
    result = clone.release(f"v{version}")
    assert result.returncode == 1
    assert "does not point at HEAD" in result.stderr


def test_release_mode_refuses_a_version_that_differs(clone: Clone, tags: None) -> None:
    clone.git("tag", "-a", "v9.9.9", "-m", "clean notes")
    result = clone.release("v9.9.9")
    assert result.returncode == 1
    assert "pyproject.toml" in result.stderr


def test_release_mode_refuses_notes_that_break_the_public_text_rules(
    clone: Clone, tags: None
) -> None:
    version = pyproject_version()
    clone.git("tag", "-a", f"v{version}", "-m", "Matches 42.4% more tracks.")
    result = clone.release(f"v{version}")
    assert result.returncode == 1
    assert "release notes" in result.stderr
    assert "42.4%" in result.stdout


def test_release_mode_refuses_a_malformed_tag(clone: Clone, tags: None) -> None:
    clone.git("tag", "-a", "release-1", "-m", "clean notes")
    result = clone.release("release-1")
    assert result.returncode == 1
    assert "vX.Y.Z" in result.stderr


def test_release_mode_refuses_a_tag_already_on_origin(
    clone: Clone, tags: None, tmp_path: Path
) -> None:
    version = pyproject_version()
    clone.git("tag", "-a", f"v{version}", "-m", "clean notes")
    bare = tmp_path / "origin.git"
    clone.git("init", "-q", "--bare", str(bare))
    clone.git(
        "-C",
        str(bare),
        "fetch",
        "-q",
        str(clone.path),
        f"refs/tags/v{version}:refs/tags/v{version}",
    )
    original = clone.git("remote", "get-url", "origin")
    clone.git("remote", "set-url", "origin", str(bare))
    try:
        result = clone.release(f"v{version}")
    finally:
        clone.git("remote", "set-url", "origin", original)
    assert result.returncode == 1
    assert "already on origin" in result.stderr


def test_release_mode_refuses_without_an_origin(clone: Clone, tags: None) -> None:
    version = pyproject_version()
    clone.git("tag", "-a", f"v{version}", "-m", "clean notes")
    original = clone.git("remote", "get-url", "origin")
    clone.git("remote", "remove", "origin")
    try:
        result = clone.release(f"v{version}")
    finally:
        clone.git("remote", "add", "origin", original)
    assert result.returncode == 1
    assert "no origin remote" in result.stderr


def test_release_mode_passes_a_correct_annotated_tag(clone: Clone, tags: None) -> None:
    version = pyproject_version()
    clone.git("tag", "-a", f"v{version}", "-m", "clean notes")
    result = clone.release(f"v{version}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "publish-check: release tag ok" in result.stdout
    assert "publish-check: versions agree ok" in result.stdout
    assert "publish-check: lock ok" in result.stdout
    assert f"publish-check: {clone.git('rev-parse', 'HEAD')} ok" in result.stdout


def test_the_clone_is_left_without_a_venv_in_the_tree(clone: Clone) -> None:
    assert os.path.isdir(clone.path / ".git")
    assert clone.git("status", "--porcelain") == ""


def test_the_release_override_runs_the_ghcr_image(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH")
    env = {
        **os.environ,
        "LRCLIB_DATA": str(tmp_path / "lrclib"),
        "MUSIC_ROOT": str(tmp_path / "music"),
        "STATE_DIR": str(tmp_path / "state"),
    }
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(OVERRIDE),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    services = yaml.safe_load(proc.stdout)["services"]
    assert services["plyrix"]["image"] == f"{IMAGE}:{pyproject_version()}"
    assert "build" not in services["plyrix"]
    assert services["plyrix"]["pull_policy"] == "missing"
    # D-19: LRCLIB is still built from the pinned upstream commit.
    lrclib = services["lrclib"]
    assert lrclib["build"]["context"].startswith("https://github.com/tranxuanthang/lrclib.git#")
    assert lrclib["pull_policy"] == "build"
