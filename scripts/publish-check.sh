#!/usr/bin/env bash
# The scrub gate for this repo (D-07). Run it immediately before every push of this repo, on the
# exact ref being pushed; the pre-push hook runs its scan subset again during the push.
#
# Usage:
#   publish-check.sh REF   full gate: REF is HEAD, the tree is clean, no commit on any ref touches
#                          a private path, a private deny-list is in use, scan-private tree and
#                          history pass, zizmor passes on .github/workflows, `uv build` works and
#                          the fast public-text tests pass
#   publish-check.sh --hook
#                          the pre-push hook: reads git's pre-push lines on stdin, refuses a delete
#                          or any pushed commit that isn't HEAD, then runs the scan subset only
#   publish-check.sh --release vX.Y.Z
#                          before a release tag push: the tag is annotated and at HEAD, its version
#                          equals pyproject.toml's, plyrix.__version__ and the image tag in
#                          examples/compose.release.yaml, its message (the release notes) passes
#                          scripts/check_public_text.py, `uv lock --check` passes, and the tag
#                          isn't on origin yet; then the full gate on HEAD
#   publish-check.sh --external DIR BASE
#                          before a push from another repo, such as the portfolio or profile: run
#                          from this repo, with this repo's private deny-list, against the repo at
#                          DIR. DIR's tree is clean, BASE..HEAD holds commits, gitleaks finds
#                          nothing in DIR's HEAD tree and file names, in the BASE..HEAD changes, or
#                          in those commits' messages and identities and the branch name, and the
#                          lines BASE..HEAD adds to text files pass scripts/check_public_text.py.
#                          DIR's history before BASE is already public and is not scanned.
#
# Each step prints `publish-check: <step> ok`. The first failure exits 1; a usage error or a
# missing tool exits 2. Unlike scan-private.sh, a missing deny-list is a failure here.
set -euo pipefail

# Private paths that must never appear in any commit on any ref.
PRIVATE_PATHS=(.planning PROJECT-BRIEF.md CLAUDE.local.md reference .claude secrets config)

# Fast tests run by the full gate. Later plans append to this list.
FAST_TESTS=(
  tests/test_workflows.py
  tests/test_lyric_leak.py::test_the_listing_covers_the_repo_and_leaves_out_private_files
  tests/test_lyric_leak.py::test_no_tracked_lyric_files
  tests/test_lyric_leak.py::test_timestamped_lines_are_placeholders
  tests/test_public_claims.py
  tests/test_governance.py
  tests/test_release.py
  tests/test_packaging.py::test_runtime_licences_fit_mit
)

ZIZMOR=zizmor==1.32.0

usage() {
  echo "usage: publish-check.sh REF, publish-check.sh --release vX.Y.Z," >&2
  echo "       publish-check.sh --external DIR BASE, or" >&2
  echo "       publish-check.sh --hook (pre-push lines on stdin)" >&2
  exit 2
}

fail() {
  echo "publish-check: $*" >&2
  exit 1
}

ok() {
  echo "publish-check: $1 ok"
}

need() {
  local tool
  for tool in "$@"; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      echo "publish-check: $tool is not on PATH" >&2
      exit 2
    fi
  done
}

head_sha() {
  git rev-parse --verify HEAD^{commit}
}

check_clean_tree() {
  if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    git status --short >&2
    fail "the working tree is not clean; commit or remove these first"
  fi
  ok "clean tree"
}

check_private_paths() {
  local hits
  hits=$(git log --all --format=%H -- "${PRIVATE_PATHS[@]}")
  if [ -n "$hits" ]; then
    echo "$hits" | head -5 >&2
    fail "commits on some ref touch a private path (${PRIVATE_PATHS[*]})"
  fi
  ok "private paths"
}

# Sets deny_list to the absolute path of the private deny-list in use, or fails.
check_deny_list() {
  local common default
  common=$(git rev-parse --path-format=absolute --git-common-dir)
  default="$(dirname "$common")/.planning/private/deny-list.toml"
  if [ -n "${PLYRIX_DENY_LIST:-}" ]; then
    [ -f "$PLYRIX_DENY_LIST" ] || fail "PLYRIX_DENY_LIST names no file"
    deny_list=$(realpath -- "$PLYRIX_DENY_LIST")
  elif [ -f "$default" ]; then
    deny_list=$default
  else
    fail "no private deny-list: set PLYRIX_DENY_LIST or create $default"
  fi
  ok "${1:-}deny-list"
}

check_scans() {
  "$top/scripts/scan-private.sh" tree || fail "scan-private.sh tree failed"
  ok "scan-private tree"
  "$top/scripts/scan-private.sh" history || fail "scan-private.sh history failed"
  ok "scan-private history"
}

scan_subset() {
  check_clean_tree
  check_private_paths
  check_deny_list
  check_scans
}

check_ref_is_head() {
  local ref=$1 sha
  sha=$(git rev-parse --verify --quiet "$ref^{commit}") || fail "$ref names no commit"
  [ "$sha" = "$(head_sha)" ] || fail "$ref is not HEAD; check out what you push and run this again"
  ok "ref is HEAD"
}

check_zizmor() {
  uvx "$ZIZMOR" --offline .github/workflows || fail "zizmor found problems in .github/workflows"
  ok "zizmor"
}

check_build() {
  local out
  out=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$out'" EXIT
  uv build --quiet --out-dir "$out" || fail "uv build failed"
  ok "build"
}

check_fast_tests() {
  local test
  for test in "${FAST_TESTS[@]}"; do
    [ -f "${test%%::*}" ] || fail "listed test file ${test%%::*} is missing"
  done
  uv run pytest -q "${FAST_TESTS[@]}" || fail "the fast public-text tests failed"
  ok "fast tests"
}

full_mode() {
  local ref=$1
  need git gitleaks uv uvx
  check_ref_is_head "$ref"
  scan_subset
  check_zizmor
  check_build
  check_fast_tests
  echo "publish-check: $(head_sha) ok"
}

release_mode() {
  local tag=$1 version pyproject module image notes
  need git gitleaks uv uvx python3
  [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "$tag is not a vX.Y.Z tag"
  [ "$(git cat-file -t "refs/tags/$tag" 2>/dev/null)" = tag ] ||
    fail "$tag is not an annotated tag; create it with git tag -a"
  [ "$(git rev-parse --verify --quiet "refs/tags/$tag^{commit}")" = "$(head_sha)" ] ||
    fail "$tag does not point at HEAD"
  ok "release tag"

  # The tag's message is the release notes, so it is held to the public-text rules.
  notes=$(mktemp)
  git for-each-ref --format='%(contents)' "refs/tags/$tag" >"$notes"
  if ! python3 "$top/scripts/check_public_text.py" "$notes"; then
    rm -f "$notes"
    fail "the release notes in $tag break the public-text rules"
  fi
  rm -f "$notes"
  ok "release notes"

  version=${tag#v}
  pyproject=$(uv version --short)
  [ "$pyproject" = "$version" ] || fail "$tag is $version, but pyproject.toml says $pyproject"
  module=$(uv run --quiet python -c 'import plyrix; print(plyrix.__version__)')
  [ "$module" = "$version" ] || fail "$tag is $version, but plyrix.__version__ is $module"
  image=$(sed -n 's|^ *image: ghcr.io/patrickclery/plyrix:\([^ ]*\) *$|\1|p' \
    examples/compose.release.yaml)
  [ "$image" = "$version" ] ||
    fail "$tag is $version, but examples/compose.release.yaml runs ${image:-no plyrix image}"
  ok "versions agree"

  uv lock --check --quiet || fail "uv.lock is out of date; run uv lock and commit"
  ok "lock"

  git remote get-url origin >/dev/null 2>&1 || fail "no origin remote; a release needs one"
  [ -z "$(git ls-remote --tags origin "refs/tags/$tag")" ] || fail "$tag is already on origin"
  ok "tag not on origin"

  full_mode HEAD
}

hook_mode() {
  local head local_ref local_sha remote_ref peeled zeros
  need git gitleaks
  head=$(head_sha)
  zeros=0000000000000000000000000000000000000000
  while read -r local_ref local_sha remote_ref _; do
    [ -n "${local_ref:-}" ] || continue
    if [ "$local_sha" = "$zeros" ]; then
      fail "refusing to delete $remote_ref; deletes are done by hand, not through this hook"
    fi
    # An annotated tag's line carries the tag object's SHA, so peel it to its commit.
    peeled=$(git rev-parse --verify --quiet "$local_sha^{commit}") ||
      fail "$local_ref does not point at a commit"
    if [ "$peeled" != "$head" ]; then
      fail "$local_ref is not HEAD; check out what you push, so the scans run on it"
    fi
  done
  ok "pushed refs are HEAD"
  scan_subset
  echo "publish-check: $head ok"
}

# The gate for a push from another repo. Runs from this repo's top level; works inside DIR.
external_mode() {
  local dir=$1 base=$2 base_sha head scratch status=0 name added
  need git gitleaks python3
  check_deny_list "external "
  git -C "$dir" rev-parse --git-dir >/dev/null 2>&1 || fail "$dir is not a git repo"
  cd "$dir"
  base_sha=$(git rev-parse --verify --quiet "$base^{commit}") || fail "$base names no commit in $dir"
  head=$(head_sha)
  [ -n "$(git rev-list "$base_sha..$head")" ] || fail "$base..HEAD holds no commits; nothing to push"

  if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    git status --short >&2
    fail "the working tree in $dir is not clean; commit or remove these first"
  fi
  ok "external clean tree"

  scratch=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$scratch'" EXIT
  mkdir "$scratch/export" "$scratch/names" "$scratch/text" "$scratch/added"

  # HEAD's tree, as it will be published, and its file names. Never DIR itself, which may hold
  # untracked or ignored files that are not pushed.
  git archive HEAD | tar -x -C "$scratch/export"
  git ls-tree -r -z --name-only HEAD | tr '\0' '\n' >"$scratch/names/tree"
  gitleaks dir "$scratch/export" --no-banner --redact -l error -c "$deny_list" &&
    gitleaks dir "$scratch/names" --no-banner --redact -l error -c "$deny_list" ||
    fail "external tree: gitleaks found a leak in $dir's HEAD tree or file names"
  ok "external tree"

  # The new commits' changes, and the names they held, but not the history before BASE.
  git log -m --name-only --format= -z "$base_sha..$head" | tr '\0' '\n' >"$scratch/names/new"
  gitleaks git . --no-banner --redact -l error -c "$deny_list" --log-opts="$base_sha..$head" &&
    gitleaks dir "$scratch/names" --no-banner --redact -l error -c "$deny_list" ||
    fail "external commits: gitleaks found a leak in $base..HEAD"
  ok "external commits"

  git log --format='%an%n%ae%n%cn%n%ce%n%B' "$base_sha..$head" >"$scratch/text/commits"
  git symbolic-ref --quiet --short HEAD >"$scratch/text/branch" || true
  gitleaks dir "$scratch/text" --no-banner --redact -l error -c "$deny_list" ||
    fail "external identities and messages: gitleaks found a leak in $base..HEAD"
  ok "external identities and messages"

  # The lines BASE..HEAD adds to text files; numstat marks a binary file with "-".
  while IFS=$'\t' read -r -d '' added _ name; do
    [ "$added" != "-" ] || continue
    [ -n "$name" ] || continue
    mkdir -p "$scratch/added/$(dirname -- "$name")"
    git diff --no-color --no-ext-diff -U0 "$base_sha..$head" -- "$name" |
      sed -n '/^+++ /d; s/^+//p' >"$scratch/added/$name"
  done < <(git diff --numstat -z --no-renames --diff-filter=d "$base_sha..$head")
  if [ -n "$(find "$scratch/added" -type f -print -quit)" ]; then
    mapfile -d '' added_files < <(find "$scratch/added" -type f -print0 | sort -z)
    python3 "$top/scripts/check_public_text.py" "${added_files[@]}" || status=1
  fi
  [ "$status" -eq 0 ] || fail "external public text: lines added in $base..HEAD break the rules"
  ok "external public text"
  echo "publish-check: external $head ok"
}

[ "$#" -ge 1 ] || usage
need git
top=$(git rev-parse --show-toplevel)
cd "$top"

case "$1" in
  --hook)
    [ "$#" -eq 1 ] || usage
    hook_mode
    ;;
  --release)
    [ "$#" -eq 2 ] || usage
    release_mode "$2"
    ;;
  --external)
    [ "$#" -eq 3 ] || usage
    external_mode "$2" "$3"
    ;;
  -*) usage ;;
  *)
    [ "$#" -eq 1 ] || usage
    full_mode "$1"
    ;;
esac
