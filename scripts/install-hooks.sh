#!/usr/bin/env bash
# Install the pre-commit and commit-msg scans, and the pre-push gate. A global core.hooksPath would make git ignore .git/hooks, so this
# sets a repo-local core.hooksPath to this repo's own hooks directory.
set -euo pipefail

git_common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
hooks_dir="$git_common_dir/hooks"

mkdir -p "$hooks_dir"
git config --local core.hooksPath "$hooks_dir"

cat >"$hooks_dir/pre-commit" <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/scan-private.sh" staged
HOOK
chmod +x "$hooks_dir/pre-commit"

cat >"$hooks_dir/commit-msg" <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/scan-private.sh" message "$1"
HOOK
chmod +x "$hooks_dir/commit-msg"

# git gives pre-push its ref lines on stdin; publish-check refuses any pushed commit that isn't
# HEAD, then runs the private scans on it.
cat >"$hooks_dir/pre-push" <<'HOOK'
#!/usr/bin/env bash
exec "$(git rev-parse --show-toplevel)/scripts/publish-check.sh" --hook
HOOK
chmod +x "$hooks_dir/pre-push"

echo "pre-commit, commit-msg and pre-push hooks installed in $hooks_dir"
