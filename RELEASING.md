# Releasing plyrix

Releases are cut by hand and published by `.github/workflows/release.yml` when an annotated `vX.Y.Z` tag is pushed. It publishes the sdist and wheel to PyPI by trusted publishing, with attestations, and the `linux/amd64` and `linux/arm64` image to `ghcr.io/patrickclery/plyrix`, tagged `X.Y.Z` and `X.Y` and never `latest`. Then it creates the GitHub Release. LRCLIB's image is never published.

release-please isn't used yet, because it doesn't update `uv.lock`; that waits on its uv.lock issue.

## Steps

1. Start on `main`, up to date, with CI green.
2. For a later release, bump the version with `uv version --bump patch` or `uv version --bump minor`, then set the same version in `src/plyrix/__init__.py` and in the image tag in `examples/compose.release.yaml`. The first release is v0.1.0, which is already the version, so it needs no bump.
3. Run `uv lock`, then commit with the message `chore(release): vX.Y.Z`.
4. Create an annotated tag whose message is the release notes: `git tag -a vX.Y.Z`. The notes hold no lyrics, and only numbers from the measured allow-list in `scripts/check_public_text.py`, each with its source.
5. Run `scripts/publish-check.sh --release vX.Y.Z`. It runs the full gate, checks the tag is annotated and at HEAD, that its version matches `pyproject.toml`, `plyrix.__version__` and `examples/compose.release.yaml`, that `uv lock --check` passes and that the tag isn't on `origin` yet.
6. Push the tag only: `git push origin vX.Y.Z`.
7. Approve the `pypi` deployment in the Actions run. The `pypi` environment needs the maintainer's review, so nothing is published before it.
8. Check that PyPI has the sdist and wheel, that GHCR has both architectures, and that the GitHub Release exists with the notes.
