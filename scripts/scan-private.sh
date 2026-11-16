#!/usr/bin/env bash
# Scan for secrets and private terms with gitleaks.
# Usage: scan-private.sh staged|tree|history, or scan-private.sh message FILE
#
# gitleaks' own rules see only file contents, so the rest goes through a temp folder as text:
#   staged   staged contents and file names, and the identity that will sign the commit
#   message  a commit message file
#   tree     every file in HEAD and its name
#   history  every commit's changes on every ref, and every file name those commits ever held
# tree and history also cover the messages and identities of every commit on every ref, every ref
# name (branches, tags, notes and anything else under refs/), and each annotated tag's name, tagger
# and message, following a tag that points at another tag.
#
# Not covered: a private term the deny-list doesn't hold, since a list finds only the terms it has,
# as its rules write them, so a misspelling, another case or an encoded form gets through unless a
# rule matches it. Untracked and gitignored files, on purpose. A blob or tree a tag points at
# directly, which no commit holds. Unreachable objects and reflogs, which a push doesn't send.
# Local git config and hooks, which a push doesn't send either. A clean result means these rules
# matched nothing in the places above, and no more than that.
#
# Private terms come from PLYRIX_DENY_LIST, else .planning/private/deny-list.toml beside the
# main checkout's git directory. With neither, the default ruleset runs and a warning is printed.
# A PLYRIX_DENY_LIST naming no file is an error, as falling back would turn the private scan off.
# Never point `gitleaks dir` at the repo root: it reads gitignored private files.
set -euo pipefail

usage() {
  echo "usage: scan-private.sh staged|tree|history, or scan-private.sh message FILE" >&2
  exit 2
}

[ "$#" -ge 1 ] || usage
mode=$1
case "$mode" in
  staged | tree | history) [ "$#" -eq 1 ] || usage ;;
  message) [ "$#" -eq 2 ] && [ -f "$2" ] || usage ;;
  *) usage ;;
esac

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "scan-private: gitleaks is not on PATH" >&2
  exit 2
fi

cd "$(git rev-parse --show-toplevel)"

git_common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
default_config="$(dirname "$git_common_dir")/.planning/private/deny-list.toml"
config_args=()
if [ -n "${PLYRIX_DENY_LIST:-}" ]; then
  if [ ! -f "$PLYRIX_DENY_LIST" ]; then
    echo "scan-private: PLYRIX_DENY_LIST names no file" >&2
    exit 2
  fi
  config_args=(-c "$PLYRIX_DENY_LIST")
elif [ -f "$default_config" ]; then
  config_args=(-c "$default_config")
else
  echo "scan-private: no private deny-list found, using the default ruleset" >&2
fi

scratch=$(mktemp -d)
cleanup() {
  rm -rf "$scratch"
}
trap cleanup EXIT
text_dir="$scratch/text"
mkdir "$text_dir"

status=0
# A leak is reported over any other failure, and any failure over a clean result.
record() {
  if [ "$1" -eq 1 ] || [ "$status" -eq 0 ]; then
    status=$1
  fi
}

save_commit_text() {
  git log --all --format='%an%n%ae%n%cn%n%ce%n%B' >"$text_dir/commits"
  git for-each-ref --format='%(refname)' >"$text_dir/refs"
  # git cat-file prints a tag's name, tagger and message, and what it points at.
  git for-each-ref --format='%(objectname) %(objecttype)' | while read -r object type; do
    while [ "$type" = tag ]; do
      git cat-file tag "$object" >"$scratch/tag"
      cat "$scratch/tag" >>"$text_dir/tags"
      { read -r _ object; read -r _ type; } <"$scratch/tag"
    done
  done
}

scan_text() {
  gitleaks dir "$text_dir" --no-banner --redact -l error ${config_args[@]+"${config_args[@]}"} ||
    record $?
}

# Names are listed with -z, as git otherwise quotes a non-ASCII byte and hides a term from the rules.
case "$mode" in
  message)
    cat -- "$2" >"$text_dir/message"
    scan_text
    ;;
  staged)
    gitleaks git . --pre-commit --staged --no-banner --redact -l error \
      ${config_args[@]+"${config_args[@]}"} || record $?
    # A deletion is left out, so that a name already leaked can still be removed.
    git diff --cached --name-only --diff-filter=d -z | tr '\0' '\n' >"$text_dir/names"
    git var GIT_AUTHOR_IDENT >"$text_dir/identity" 2>/dev/null || true
    git var GIT_COMMITTER_IDENT >>"$text_dir/identity" 2>/dev/null || true
    scan_text
    ;;
  history)
    gitleaks git . --no-banner --redact --log-opts=--all ${config_args[@]+"${config_args[@]}"} ||
      record $?
    if git rev-parse --verify --quiet HEAD >/dev/null; then
      save_commit_text
      git log --all -m --name-only --format= -z | tr '\0' '\n' >"$text_dir/names"
      scan_text
    fi
    ;;
  tree)
    if ! git rev-parse --verify --quiet HEAD >/dev/null; then
      echo "scan-private: no commits yet, nothing to scan in tree mode"
      exit 0
    fi
    export_dir="$scratch/export"
    mkdir "$export_dir"
    git archive HEAD | tar -x -C "$export_dir"
    gitleaks dir "$export_dir" --no-banner --redact ${config_args[@]+"${config_args[@]}"} ||
      record $?
    save_commit_text
    git ls-tree -r -z --name-only HEAD | tr '\0' '\n' >"$text_dir/names"
    scan_text
    ;;
esac

if [ "$status" -eq 1 ]; then
  echo "scan-private: $mode scan found a leak" >&2
fi
exit "$status"
