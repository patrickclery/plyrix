# Contributing to plyrix

Thanks for helping. plyrix's promise is that it never touches an audio file, so every change is held to that first.

## Setup

Install the locked environment with `uv sync`.

## The one test command

`uv run pytest` is the one test command. The same command runs locally and in hosted CI. The test fixtures are synthetic audio files made by `scripts/make_fixtures.sh` with ffmpeg; they hold no real music and no lyrics.

Some gates need things a hosted runner doesn't have, such as real audio samples, a local LRCLIB, a local Plex server or a built image. They are pytest markers (`real_samples`, `lrclib_contract`, `plex_contract`, `docker_image`, `lidarr_e2e` and `lrclib_update`, listed in `pyproject.toml`), they skip unless their environment variable is set, and they never run in hosted CI.

## Gates

Run these before you open a pull request:

- `uv run pytest`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy src tests`
- `uv run lint-imports`

Run `scripts/install-hooks.sh` once in your checkout. It installs git hooks that scan each commit and push for secrets with gitleaks. A contributor's scan uses gitleaks' default rules; the maintainer also runs a private list of terms that stays local.

## Rules

- Test only on copies of a library, never a live library. plyrix writes files beside your audio, so try it on a copy first.
- No lyrics, tokens, private paths or library listings in issues, pull requests, fixtures or screenshots. Replace a path with a placeholder such as `/srv/music`.
- No tag-writing library may reach runtime. plyrix reads tags with a reader that has no write path, and import-linter contracts and tests hold that.
- New dependencies should be well adopted and permissively licensed.
- Numbers in docs come only from measured evidence, with their source.

Maintainers: the release procedure is in [RELEASING.md](RELEASING.md).
