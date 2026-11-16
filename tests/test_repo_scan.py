"""Prove the pre-commit hook and all three scan modes against a planted placeholder term.

The temporary repos deliberately keep the caller's global git config. The global core.hooksPath
on a machine that sets one must lose to the repo-local override, or the gate is silently off.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# A placeholder, never a real private term.
TERM = "zzprivateband"
# git quotes a non-ASCII byte in a name unless it is asked not to, which would hide this one.
ACCENTED_TERM = "zzprivat\u00e9band"
DENY_LIST = f"""\
[extend]
useDefault = true

[[rules]]
id = "planted"
description = "planted placeholder term"
regex = "{TERM}"
keywords = ["{TERM}"]

[[rules]]
id = "planted-accented"
description = "planted placeholder term that is not ASCII"
regex = "{ACCENTED_TERM}"
keywords = ["{ACCENTED_TERM}"]
"""


@pytest.fixture(autouse=True)
def gitleaks_on_path() -> None:
    """Skip these proofs without gitleaks, unless the run says they have to happen (D-19)."""
    if shutil.which("gitleaks") is not None:
        return
    reason = "gitleaks is not on PATH, so the scan and the hook cannot be proved here"
    if os.environ.get("CI") or os.environ.get("PLYRIX_REQUIRE_GITLEAKS"):
        pytest.fail(reason)
    pytest.skip(reason)


def leak_marker(mode: str) -> str:
    return f"scan-private: {mode} scan found a leak"


def base_env() -> dict[str, str]:
    """Return the caller's environment without settings that would point git elsewhere."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "PLYRIX_DENY_LIST"
    }
    env.update(
        GIT_AUTHOR_NAME="Fixture Author",
        GIT_AUTHOR_EMAIL="author@example.invalid",
        GIT_COMMITTER_NAME="Fixture Committer",
        GIT_COMMITTER_EMAIL="committer@example.invalid",
    )
    return env


class ScanRepo:
    """A temporary git repo holding copies of the scan and hook scripts."""

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

    def scan(self, mode: str) -> subprocess.CompletedProcess[str]:
        return self.run("scripts/scan-private.sh", mode)

    def stage(self, name: str, contents: str) -> None:
        (self.path / name).write_text(contents, encoding="utf-8")
        self.git("add", "--", name)

    def commit_file(self, name: str, contents: str) -> None:
        self.stage(name, contents)
        self.git("commit", "-qm", f"add {name}")

    def install_hooks(self) -> None:
        result = self.run("scripts/install-hooks.sh")
        assert result.returncode == 0, result.stderr
        # Resolved by git itself, so this fails if a global core.hooksPath still wins.
        hook = Path(
            self.git("rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-commit")
        )
        assert hook == self.path / ".git" / "hooks" / "pre-commit"
        assert (hook.parent / "commit-msg").is_file()


@pytest.fixture
def repo(tmp_path: Path) -> ScanRepo:
    deny_list = tmp_path / "deny-list.toml"
    deny_list.write_text(DENY_LIST, encoding="utf-8")

    repo_path = tmp_path / "repo"
    (repo_path / "scripts").mkdir(parents=True)
    # The hook finds the scan through the repo's top level. Without these copies its exec fails
    # with 127, and a blocked-commit assertion would pass with no scan run.
    for script in ("scan-private.sh", "install-hooks.sh"):
        shutil.copy2(ROOT / "scripts" / script, repo_path / "scripts" / script)

    scan_repo = ScanRepo(repo_path, {**base_env(), "PLYRIX_DENY_LIST": str(deny_list)})
    scan_repo.git("init", "-q")
    return scan_repo


def test_staged_scan_exits_1_on_planted_term(repo: ScanRepo) -> None:
    repo.stage("planted.txt", f"{TERM}\n")
    result = repo.scan("staged")
    assert result.returncode == 1
    assert leak_marker("staged") in result.stderr
    assert TERM not in result.stdout + result.stderr


def test_staged_scan_exits_0_on_clean_content(repo: ScanRepo) -> None:
    repo.stage("clean.txt", "nothing to see\n")
    result = repo.scan("staged")
    assert result.returncode == 0, result.stdout + result.stderr
    assert leak_marker("staged") not in result.stderr


def test_staged_scan_redacts_when_findings_are_printed(repo: ScanRepo, tmp_path: Path) -> None:
    # The scan prints no finding by default, which would make a redaction check pass vacuously.
    # This wrapper turns printing on, and the redaction has to come from the script's own flag.
    gitleaks = shutil.which("gitleaks")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "gitleaks"
    wrapper.write_text(f'#!/bin/sh\nexec "{gitleaks}" "$@" --verbose\n', encoding="utf-8")
    wrapper.chmod(0o755)
    repo.env["PATH"] = f"{bin_dir}{os.pathsep}{repo.env['PATH']}"

    repo.stage("planted.txt", f"{TERM}\n")
    result = repo.scan("staged")

    assert result.returncode == 1
    assert "REDACTED" in result.stdout + result.stderr
    assert TERM not in result.stdout + result.stderr


def test_hook_allows_clean_commit(repo: ScanRepo) -> None:
    repo.install_hooks()
    repo.stage("clean.txt", "nothing to see\n")

    result = repo.run("git", "commit", "-qm", "clean")

    assert result.returncode == 0, result.stderr
    assert repo.git("rev-list", "--count", "HEAD") == "1"


def test_hook_blocks_planted_term(repo: ScanRepo) -> None:
    repo.install_hooks()
    repo.commit_file("clean.txt", "nothing to see\n")
    head = repo.git("rev-parse", "HEAD")
    repo.stage("planted.txt", f"{TERM}\n")

    result = repo.run("git", "commit", "-qm", "planted")

    assert result.returncode != 0
    assert repo.git("rev-parse", "HEAD") == head
    assert leak_marker("staged") in result.stderr
    assert TERM not in result.stdout + result.stderr


def test_hook_missing_script_has_no_marker(repo: ScanRepo) -> None:
    repo.install_hooks()
    (repo.path / "scripts" / "scan-private.sh").unlink()
    repo.stage("clean.txt", "nothing to see\n")

    result = repo.run("git", "commit", "-qm", "clean")

    assert result.returncode != 0
    assert leak_marker("staged") not in result.stderr


def test_history_scan_finds_term_added_then_deleted(repo: ScanRepo) -> None:
    repo.commit_file("clean.txt", "nothing to see\n")
    assert repo.scan("history").returncode == 0

    repo.commit_file("planted.txt", f"{TERM}\n")
    repo.git("rm", "-q", "--", "planted.txt")
    repo.git("commit", "-qm", "remove planted.txt")

    assert not (repo.path / "planted.txt").exists()
    result = repo.scan("history")
    assert result.returncode == 1
    assert leak_marker("history") in result.stderr
    assert TERM not in result.stdout + result.stderr


def test_tree_scan_ignores_gitignored_file(repo: ScanRepo) -> None:
    repo.commit_file(".gitignore", "ignored.txt\n")
    (repo.path / "ignored.txt").write_text(f"{TERM}\n", encoding="utf-8")

    result = repo.scan("tree")

    assert result.returncode == 0, result.stdout + result.stderr


def test_tree_scan_reports_tracked_file(repo: ScanRepo) -> None:
    repo.commit_file("planted.txt", f"{TERM}\n")

    result = repo.scan("tree")

    assert result.returncode == 1
    assert leak_marker("tree") in result.stderr
    assert TERM not in result.stdout + result.stderr


def test_scan_warns_without_deny_list_and_passes_clean_content(repo: ScanRepo) -> None:
    del repo.env["PLYRIX_DENY_LIST"]
    repo.stage("clean.txt", "nothing to see\n")

    result = repo.scan("staged")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr.count("no private deny-list found") == 1


# File names and commit metadata, which gitleaks' rules never look at


def assert_found_quietly(result: subprocess.CompletedProcess[str], mode: str, term: str) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert leak_marker(mode) in result.stderr
    assert term not in result.stdout + result.stderr


@pytest.mark.parametrize("term", [TERM, ACCENTED_TERM], ids=["ascii", "accented"])
def test_staged_scan_finds_a_term_only_in_a_file_name(repo: ScanRepo, term: str) -> None:
    repo.stage(f"{term}.txt", "nothing to see\n")
    assert_found_quietly(repo.scan("staged"), "staged", term)


def test_staged_scan_allows_deleting_a_file_with_a_term_in_its_name(repo: ScanRepo) -> None:
    repo.commit_file(f"{TERM}.txt", "nothing to see\n")
    repo.git("rm", "-q", "--", f"{TERM}.txt")

    result = repo.scan("staged")

    assert result.returncode == 0, result.stdout + result.stderr


def test_staged_scan_finds_a_term_in_the_author_name(repo: ScanRepo) -> None:
    repo.stage("clean.txt", "nothing to see\n")
    repo.env["GIT_AUTHOR_NAME"] = TERM
    assert_found_quietly(repo.scan("staged"), "staged", TERM)


@pytest.mark.parametrize("mode", ["tree", "history"])
@pytest.mark.parametrize("term", [TERM, ACCENTED_TERM], ids=["ascii", "accented"])
def test_a_term_only_in_a_tracked_file_name_is_found(repo: ScanRepo, mode: str, term: str) -> None:
    repo.stage(f"{term}.txt", "nothing to see\n")
    repo.git("commit", "-qm", "clean message")
    assert_found_quietly(repo.scan(mode), mode, term)


def test_history_scan_finds_a_file_name_since_removed(repo: ScanRepo) -> None:
    repo.stage(f"{TERM}.txt", "nothing to see\n")
    repo.git("commit", "-qm", "clean message")
    repo.git("rm", "-q", "--", f"{TERM}.txt")
    repo.git("commit", "-qm", "another clean message")
    assert_found_quietly(repo.scan("history"), "history", TERM)


@pytest.mark.parametrize("mode", ["tree", "history"])
def test_a_term_only_in_a_commit_message_is_found(repo: ScanRepo, mode: str) -> None:
    repo.stage("clean.txt", "nothing to see\n")
    repo.git("commit", "-qm", f"mention {TERM}")
    assert_found_quietly(repo.scan(mode), mode, TERM)


@pytest.mark.parametrize("mode", ["tree", "history"])
@pytest.mark.parametrize("field", ["GIT_AUTHOR_NAME", "GIT_COMMITTER_EMAIL"])
def test_a_term_only_in_commit_identity_is_found(repo: ScanRepo, mode: str, field: str) -> None:
    repo.stage("clean.txt", "nothing to see\n")
    repo.env[field] = TERM
    repo.git("commit", "-qm", "clean message")
    del repo.env[field]
    assert_found_quietly(repo.scan(mode), mode, TERM)


# Ref names and annotated tags, which a push publishes and git log never prints


def clean_commit(repo: ScanRepo) -> None:
    repo.stage("clean.txt", "nothing to see\n")
    repo.git("commit", "-qm", "clean message")


@pytest.mark.parametrize("mode", ["tree", "history"])
@pytest.mark.parametrize("term", [TERM, ACCENTED_TERM], ids=["ascii", "accented"])
def test_a_term_only_in_an_annotated_tag_message_is_found(
    repo: ScanRepo, mode: str, term: str
) -> None:
    clean_commit(repo)
    repo.git("tag", "-a", "v1", "-m", f"mention {term}")
    assert_found_quietly(repo.scan(mode), mode, term)


@pytest.mark.parametrize("mode", ["tree", "history"])
@pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
def test_a_term_only_in_a_tag_name_is_found(repo: ScanRepo, mode: str, annotated: bool) -> None:
    clean_commit(repo)
    repo.git("tag", *(["-a", "-m", "clean message"] if annotated else []), TERM)
    assert_found_quietly(repo.scan(mode), mode, TERM)


@pytest.mark.parametrize("mode", ["tree", "history"])
@pytest.mark.parametrize("term", [TERM, ACCENTED_TERM], ids=["ascii", "accented"])
def test_a_term_only_in_a_branch_name_is_found(repo: ScanRepo, mode: str, term: str) -> None:
    clean_commit(repo)
    repo.git("branch", term)
    assert_found_quietly(repo.scan(mode), mode, term)


@pytest.mark.parametrize("mode", ["tree", "history"])
@pytest.mark.parametrize("field", ["GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"])
def test_a_term_only_in_a_tagger_field_is_found(repo: ScanRepo, mode: str, field: str) -> None:
    clean_commit(repo)
    repo.env[field] = TERM
    repo.git("tag", "-a", "v1", "-m", "clean message")
    del repo.env[field]
    assert_found_quietly(repo.scan(mode), mode, TERM)


@pytest.mark.parametrize("mode", ["tree", "history"])
def test_a_term_in_a_tag_that_another_tag_points_at_is_found(repo: ScanRepo, mode: str) -> None:
    clean_commit(repo)
    repo.git("tag", "-a", "inner", "-m", f"mention {TERM}")
    repo.git("tag", "-a", "outer", "-m", "clean message", "inner")
    repo.git("tag", "-d", "inner")
    assert_found_quietly(repo.scan(mode), mode, TERM)


@pytest.mark.parametrize("mode", ["tree", "history"])
def test_clean_tags_and_branches_pass(repo: ScanRepo, mode: str) -> None:
    clean_commit(repo)
    repo.git("branch", "topic")
    repo.git("tag", "v0")
    repo.git("tag", "-a", "v1", "-m", "clean message")

    result = repo.scan(mode)

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_scan_says_what_it_does_not_cover() -> None:
    """A deny-list can't be complete, so the script has to say where its coverage stops."""
    script = (ROOT / "scripts" / "scan-private.sh").read_text(encoding="utf-8")
    header = " ".join(script.split("set -euo")[0].replace("#", " ").split())
    for gap in ("Not covered", "only the terms", "gitignored", "points at directly"):
        assert gap in header, gap


@pytest.mark.parametrize("mode", ["tree", "history"])
def test_clean_names_and_metadata_pass(repo: ScanRepo, mode: str) -> None:
    repo.commit_file("clean.txt", "nothing to see\n")
    result = repo.scan(mode)
    assert result.returncode == 0, result.stdout + result.stderr


def test_history_scan_of_a_repo_with_no_commits_passes(repo: ScanRepo) -> None:
    result = repo.scan("history")
    assert result.returncode == 0, result.stdout + result.stderr


def test_message_scan_finds_a_term_and_passes_clean_text(repo: ScanRepo, tmp_path: Path) -> None:
    message = tmp_path / "message"
    message.write_text(f"mention {TERM}\n", encoding="utf-8")
    result = repo.run("scripts/scan-private.sh", "message", str(message))
    assert_found_quietly(result, "message", TERM)

    message.write_text("clean message\n", encoding="utf-8")
    result = repo.run("scripts/scan-private.sh", "message", str(message))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("args", [(), ("message",), ("message", "no-such-file"), ("tree", "x")])
def test_a_bad_command_line_exits_2(repo: ScanRepo, args: tuple[str, ...]) -> None:
    assert repo.run("scripts/scan-private.sh", *args).returncode == 2


def test_hook_blocks_a_term_in_the_commit_message(repo: ScanRepo) -> None:
    repo.install_hooks()
    repo.commit_file("first.txt", "nothing to see\n")
    head = repo.git("rev-parse", "HEAD")
    repo.stage("clean.txt", "nothing to see\n")

    result = repo.run("git", "commit", "-qm", f"mention {TERM}")

    assert result.returncode != 0
    assert repo.git("rev-parse", "HEAD") == head
    assert leak_marker("message") in result.stderr
    assert TERM not in result.stdout + result.stderr


def test_hook_blocks_a_term_in_a_file_name(repo: ScanRepo) -> None:
    repo.install_hooks()
    repo.stage(f"{TERM}.txt", "nothing to see\n")

    result = repo.run("git", "commit", "-qm", "clean message")

    assert result.returncode != 0
    assert leak_marker("staged") in result.stderr


@pytest.mark.parametrize("mode", ["staged", "tree", "history"])
def test_a_deny_list_path_naming_no_file_stops_the_scan(
    repo: ScanRepo, tmp_path: Path, mode: str
) -> None:
    repo.commit_file("planted.txt", f"{TERM}\n")
    repo.stage("planted2.txt", f"{TERM}\n")
    repo.env["PLYRIX_DENY_LIST"] = str(tmp_path / "mistyped.toml")

    result = repo.scan(mode)

    assert result.returncode == 2
    assert "PLYRIX_DENY_LIST names no file" in result.stderr
    assert leak_marker(mode) not in result.stderr


def test_a_deny_list_path_naming_no_file_blocks_the_commit(repo: ScanRepo, tmp_path: Path) -> None:
    repo.install_hooks()
    repo.stage("clean.txt", "nothing to see\n")
    repo.env["PLYRIX_DENY_LIST"] = str(tmp_path / "mistyped.toml")

    result = repo.run("git", "commit", "-qm", "clean")

    assert result.returncode != 0
    assert "PLYRIX_DENY_LIST names no file" in result.stderr


def scan_this_repo_tree() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["scripts/scan-private.sh", "tree"],
        cwd=ROOT,
        env=base_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_this_repo_tree_scan_is_clean_under_whichever_ruleset_is_here() -> None:
    result = scan_this_repo_tree()
    assert result.returncode == 0, result.stdout + result.stderr


def test_this_repo_tree_scan_used_the_private_terms() -> None:
    common_dir = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=ROOT,
        env=base_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not (Path(common_dir).parent / ".planning" / "private" / "deny-list.toml").is_file():
        pytest.skip("no private deny-list in this checkout, so only the default ruleset ran")

    result = scan_this_repo_tree()

    assert "no private deny-list found" not in result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def run_this_file_without_gitleaks(
    tmp_path: Path, **required: str
) -> subprocess.CompletedProcess[str]:
    """Run one proof from this file with a PATH that holds no gitleaks."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CI", "PLYRIX_REQUIRE_GITLEAKS", "PATH")
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{Path(__file__).name}::test_staged_scan_exits_0_on_clean_content",
        ],
        cwd=ROOT / "tests",
        env={**inherited, "PATH": str(empty), **required},
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_missing_gitleaks_skips_the_proofs_by_default(tmp_path: Path) -> None:
    result = run_this_file_without_gitleaks(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "1 skipped" in result.stdout


@pytest.mark.parametrize("variable", ["CI", "PLYRIX_REQUIRE_GITLEAKS"])
def test_a_missing_gitleaks_fails_when_the_proofs_are_required(
    tmp_path: Path, variable: str
) -> None:
    result = run_this_file_without_gitleaks(tmp_path, **{variable: "1"})
    assert result.returncode == 1, result.stdout
    assert "skipped" not in result.stdout
    assert "gitleaks is not on PATH" in result.stdout
