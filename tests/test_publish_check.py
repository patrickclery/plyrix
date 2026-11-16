"""Prove publish-check.sh and its pre-push hook refuse each failure, on throwaway repos (D-07).

Each repo holds copies of scan-private.sh, publish-check.sh and install-hooks.sh, committed, so the
tree is clean, and a planted deny-list through PLYRIX_DENY_LIST. No real private term is used.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_repo_scan import DENY_LIST, TERM, base_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("scan-private.sh", "publish-check.sh", "install-hooks.sh", "check_public_text.py")
ZEROS = "0" * 40


@pytest.fixture(autouse=True)
def gitleaks_on_path() -> None:
    """Skip without gitleaks, unless the run says these proofs have to happen, as test_repo_scan."""
    if shutil.which("gitleaks") is not None:
        return
    reason = "gitleaks is not on PATH, so the gate and the hook cannot be proved here"
    if os.environ.get("CI") or os.environ.get("PLYRIX_REQUIRE_GITLEAKS"):
        pytest.fail(reason)
    pytest.skip(reason)


class GateRepo:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env

    def run(
        self, *args: str, stdin: str = "", env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=self.path,
            env=env if env is not None else self.env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def git(self, *args: str) -> str:
        result = self.run("git", *args)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def commit_file(self, name: str, contents: str) -> str:
        """Commit one file, past the pre-commit hook, so a planted term can reach history."""
        path = self.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        self.git("add", "--", name)
        self.git("commit", "-q", "--no-verify", "-m", f"add {name}")
        return self.git("rev-parse", "HEAD")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def hook(
        self, lines: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        stdin = "".join(f"{line}\n" for line in lines)
        return self.run("scripts/publish-check.sh", "--hook", stdin=stdin, env=env)

    def push_line(self, ref: str = "refs/heads/main", sha: str | None = None) -> str:
        return f"{ref} {sha or self.head()} {ref} {ZEROS}"


@pytest.fixture
def repo(tmp_path: Path) -> GateRepo:
    deny_list = tmp_path / "deny-list.toml"
    deny_list.write_text(DENY_LIST, encoding="utf-8")
    path = tmp_path / "repo"
    (path / "scripts").mkdir(parents=True)
    for script in SCRIPTS:
        shutil.copy2(ROOT / "scripts" / script, path / "scripts" / script)
    gate = GateRepo(path, {**base_env(), "PLYRIX_DENY_LIST": str(deny_list)})
    gate.git("init", "-q", "-b", "main")
    gate.git("add", "scripts")
    gate.git("commit", "-qm", "add the scripts")
    gate.commit_file("README.md", "a clean file\n")
    return gate


def test_a_clean_push_of_head_passes(repo: GateRepo) -> None:
    result = repo.hook([repo.push_line()])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"publish-check: {repo.head()} ok" in result.stdout
    assert "publish-check: scan-private history ok" in result.stdout


def test_a_private_path_anywhere_in_history_fails(repo: GateRepo) -> None:
    repo.commit_file(".planning/notes.md", "clean words\n")
    repo.git("rm", "-q", "-r", ".planning")
    repo.git("commit", "-qm", "remove it again")
    result = repo.hook([repo.push_line()])
    assert result.returncode == 1
    assert "touch a private path" in result.stderr


def test_a_planted_term_on_another_branch_fails(repo: GateRepo) -> None:
    repo.git("switch", "-q", "-c", "other")
    repo.commit_file("later.txt", f"{TERM}\n")
    repo.git("switch", "-q", "main")
    result = repo.hook([repo.push_line()])
    assert result.returncode == 1
    assert "scan-private.sh history failed" in result.stderr or "tree failed" in result.stderr


def test_a_dirty_tree_fails(repo: GateRepo) -> None:
    (repo.path / "README.md").write_text("changed\n", encoding="utf-8")
    result = repo.hook([repo.push_line()])
    assert result.returncode == 1
    assert "working tree is not clean" in result.stderr


def test_an_untracked_file_fails(repo: GateRepo) -> None:
    (repo.path / "new.txt").write_text("new\n", encoding="utf-8")
    result = repo.hook([repo.push_line()])
    assert result.returncode == 1
    assert "working tree is not clean" in result.stderr


def test_a_missing_deny_list_fails(repo: GateRepo) -> None:
    env = {key: value for key, value in repo.env.items() if key != "PLYRIX_DENY_LIST"}
    result = repo.hook([repo.push_line()], env=env)
    assert result.returncode == 1
    assert "no private deny-list" in result.stderr


def test_a_deny_list_variable_naming_no_file_fails(repo: GateRepo, tmp_path: Path) -> None:
    env = {**repo.env, "PLYRIX_DENY_LIST": str(tmp_path / "missing.toml")}
    result = repo.hook([repo.push_line()], env=env)
    assert result.returncode == 1
    assert "PLYRIX_DENY_LIST names no file" in result.stderr


def test_a_push_of_a_commit_that_is_not_head_is_refused(repo: GateRepo) -> None:
    older = repo.git("rev-parse", "HEAD~1")
    result = repo.hook([repo.push_line(sha=older)])
    assert result.returncode == 1
    assert "check out what you push" in result.stderr


def test_one_bad_line_among_good_ones_is_refused(repo: GateRepo) -> None:
    older = repo.git("rev-parse", "HEAD~1")
    result = repo.hook([repo.push_line(), repo.push_line("refs/heads/old", older)])
    assert result.returncode == 1
    assert "check out what you push" in result.stderr


def test_a_delete_push_is_refused(repo: GateRepo) -> None:
    result = repo.hook([f"(delete) {ZEROS} refs/heads/gone {repo.head()}"])
    assert result.returncode == 1
    assert "refusing to delete" in result.stderr


def test_an_annotated_tag_at_head_is_accepted(repo: GateRepo) -> None:
    repo.git("tag", "-a", "v9.9.9", "-m", "a clean release note")
    tag_object = repo.git("rev-parse", "v9.9.9")
    assert tag_object != repo.head()
    result = repo.hook([repo.push_line("refs/tags/v9.9.9", tag_object)])
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_annotated_tag_not_at_head_is_refused(repo: GateRepo) -> None:
    repo.git("tag", "-a", "v9.9.8", "-m", "a clean release note", "HEAD~1")
    result = repo.hook([repo.push_line("refs/tags/v9.9.8", repo.git("rev-parse", "v9.9.8"))])
    assert result.returncode == 1
    assert "check out what you push" in result.stderr


def test_the_full_gate_refuses_a_ref_that_is_not_head(repo: GateRepo) -> None:
    result = repo.run("scripts/publish-check.sh", "HEAD~1")
    assert result.returncode == 1
    assert "is not HEAD" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--hook", "x"),
        ("--bogus",),
        ("a", "b"),
        ("--release",),
        ("--release", "a", "b"),
        ("--external",),
        ("--external", "dir"),
        ("--external", "dir", "base", "extra"),
    ],
)
def test_a_bad_command_line_exits_2(repo: GateRepo, args: tuple[str, ...]) -> None:
    assert repo.run("scripts/publish-check.sh", *args).returncode == 2


@pytest.fixture
def remote(repo: GateRepo, tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], env=repo.env, check=True)
    result = repo.run("scripts/install-hooks.sh")
    assert result.returncode == 0, result.stderr
    hook = Path(repo.git("rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"))
    assert hook == repo.path / ".git" / "hooks" / "pre-push"
    assert os.access(hook, os.X_OK)
    return bare


def remote_refs(repo: GateRepo, bare: Path) -> str:
    return repo.git("ls-remote", str(bare))


def test_the_installed_hook_lets_a_clean_push_through(repo: GateRepo, remote: Path) -> None:
    result = repo.run("git", "push", str(remote), "main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert remote_refs(repo, remote) == f"{repo.head()}\trefs/heads/main"


def test_the_installed_hook_blocks_a_planted_term(repo: GateRepo, remote: Path) -> None:
    repo.commit_file("planted.txt", f"{TERM}\n")
    result = repo.run("git", "push", str(remote), "main")
    assert result.returncode != 0
    assert remote_refs(repo, remote) == ""


def test_the_installed_hook_blocks_a_push_of_another_branch(repo: GateRepo, remote: Path) -> None:
    repo.git("branch", "old", "HEAD~1")
    result = repo.run("git", "push", str(remote), "old")
    assert result.returncode != 0
    assert "check out what you push" in result.stderr
    assert remote_refs(repo, remote) == ""


# --external DIR BASE: the gate for a push from another repo, such as the portfolio or profile.


@pytest.fixture
def external(repo: GateRepo, tmp_path: Path) -> GateRepo:
    """Another repo whose `base` branch stands for its public origin, with one clean new commit."""
    path = tmp_path / "external"
    path.mkdir()
    ext = GateRepo(path, repo.env)
    ext.git("init", "-q", "-b", "master")
    # Public history before BASE may hold anything; the gate must not scan it.
    ext.commit_file("old.txt", f"{TERM} and 99% in old public history\n")
    ext.git("rm", "-q", "old.txt")
    ext.git("commit", "-qm", "remove old")
    ext.commit_file("README.md", "a profile\n")
    ext.git("branch", "base")
    ext.git("switch", "-q", "-c", "card")
    ext.commit_file("README.md", "a profile\n- a clean new line about plyrix\n")
    return ext


def run_external(
    repo: GateRepo, ext: GateRepo, base: str = "base", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return repo.run("scripts/publish-check.sh", "--external", str(ext.path), base, env=env)


def test_external_passes_a_clean_branch(repo: GateRepo, external: GateRepo) -> None:
    result = run_external(repo, external)
    assert result.returncode == 0, result.stdout + result.stderr
    steps = ("clean tree", "deny-list", "tree", "commits", "identities and messages", "public text")
    for step in steps:
        assert f"publish-check: external {step} ok" in result.stdout
    assert f"publish-check: external {external.head()} ok" in result.stdout


def test_external_fails_a_planted_term_in_a_new_commit(repo: GateRepo, external: GateRepo) -> None:
    external.commit_file("planted.txt", f"{TERM}\n")
    external.git("rm", "-q", "planted.txt")
    external.git("commit", "-qm", "remove it again")
    result = run_external(repo, external)
    assert result.returncode == 1
    assert "external commits" in result.stderr
    assert TERM not in result.stdout + result.stderr


def test_external_fails_a_planted_term_in_the_head_tree(repo: GateRepo, external: GateRepo) -> None:
    external.commit_file("planted.txt", f"{TERM}\n")
    result = run_external(repo, external)
    assert result.returncode == 1
    assert "external tree" in result.stderr


def test_external_fails_a_planted_term_in_a_message(repo: GateRepo, external: GateRepo) -> None:
    external.git("commit", "-q", "--allow-empty", "--no-verify", "-m", f"about {TERM}")
    result = run_external(repo, external)
    assert result.returncode == 1
    assert "external identities and messages" in result.stderr


def test_external_fails_a_planted_term_in_an_identity(repo: GateRepo, external: GateRepo) -> None:
    env = {**external.env, "GIT_AUTHOR_NAME": TERM, "GIT_AUTHOR_EMAIL": "a@example.invalid"}
    result = external.run("git", "commit", "-q", "--allow-empty", "-m", "clean", env=env)
    assert result.returncode == 0, result.stderr
    result = run_external(repo, external)
    assert result.returncode == 1
    assert "external identities and messages" in result.stderr


def test_external_fails_an_unlisted_percentage_on_an_added_line(
    repo: GateRepo, external: GateRepo
) -> None:
    external.commit_file("src/card.tsx", 'description: "right on 97.5% of a sample",\n')
    result = run_external(repo, external)
    assert result.returncode == 1
    assert "number" in result.stdout
    assert "public text" in result.stderr


def test_external_checks_added_lines_only(repo: GateRepo, external: GateRepo) -> None:
    external.git("switch", "-q", "base")
    external.commit_file("old-stats.md", "an old 42% line already public\n")
    external.git("switch", "-q", "-c", "card2")
    external.commit_file("old-stats.md", "an old 42% line already public\nand a clean one\n")
    result = run_external(repo, external)
    assert result.returncode == 0, result.stdout + result.stderr


def test_external_skips_binary_files_for_public_text(repo: GateRepo, external: GateRepo) -> None:
    (external.path / "images").mkdir()
    (external.path / "images" / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n\0 50% \0")
    external.git("add", "images/p.png")
    external.git("commit", "-qm", "add a preview")
    result = run_external(repo, external)
    assert result.returncode == 0, result.stdout + result.stderr


def test_external_fails_a_dirty_tree(repo: GateRepo, external: GateRepo) -> None:
    (external.path / "README.md").write_text("changed\n", encoding="utf-8")
    result = run_external(repo, external)
    assert result.returncode == 1
    assert "is not clean" in result.stderr


def test_external_fails_without_a_deny_list(repo: GateRepo, external: GateRepo) -> None:
    env = {key: value for key, value in repo.env.items() if key != "PLYRIX_DENY_LIST"}
    result = run_external(repo, external, env=env)
    assert result.returncode == 1
    assert "no private deny-list" in result.stderr


def test_external_refuses_an_empty_range(repo: GateRepo, external: GateRepo) -> None:
    result = run_external(repo, external, base="card")
    assert result.returncode == 1
    assert "no commits" in result.stderr


def test_external_refuses_an_unknown_base(repo: GateRepo, external: GateRepo) -> None:
    result = run_external(repo, external, base="nope")
    assert result.returncode == 1
    assert "names no commit" in result.stderr


def test_external_refuses_a_folder_that_is_not_a_repo(repo: GateRepo, tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    result = repo.run("scripts/publish-check.sh", "--external", str(tmp_path / "plain"), "base")
    assert result.returncode == 1
    assert "not a git repo" in result.stderr
