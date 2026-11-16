"""SAFE-01 and SAFE-02: a real run leaves every audio format byte-identical, creating only sidecars.

The fixtures are committed, so nothing here calls ffmpeg or skips when it is missing.
"""

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from tinytag import TinyTag

from conftest import (
    AUDIO_SUFFIXES,
    FIXTURES,
    RunCli,
    StubLrclib,
    parse_counts,
    per_format_lib,
    snap,
)

EMBEDDED_TOKEN = b"PLYRIX-EMBEDDED-TOKEN-7F3A"
EXPECTED_FIXTURES = {
    "t_id3v24.mp3",
    "id3v23_unsync.flac",
    "id3v23_unsync.mp3",
    *(
        f"{stem}{suffix}"
        for stem in ("t", "albumartist_only", "various_artists", "notags", "embedded_token")
        for suffix in AUDIO_SUFFIXES
    ),
}


def listing(lib: Path) -> list[str]:
    return sorted(str(path.relative_to(lib)) for path in lib.rglob("*"))


def test_fixtures_match_sha256sums() -> None:
    lines = (FIXTURES / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    recorded = {name: digest for digest, name in (line.split(maxsplit=1) for line in lines)}

    assert set(recorded) == EXPECTED_FIXTURES
    on_disk = {path.name for path in FIXTURES.iterdir()}
    assert on_disk == EXPECTED_FIXTURES | {"SHA256SUMS"}
    for name, digest in recorded.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name


def test_audio_unchanged_after_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    tracks = per_format_lib(lib, "t")
    before = [snap(track) for track in tracks]

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert [snap(track) for track in tracks] == before
    counts = parse_counts(proc.stdout)
    assert counts["written_lrc"] == 5
    assert counts["synced"] == 5
    assert counts["skipped_unchanged"] == 0
    for track in tracks:
        assert sorted(path.name for path in track.parent.iterdir()) == sorted([track.name, "t.lrc"])
    assert len(stub_lrclib.requests) == 5
    for request in stub_lrclib.requests:
        assert request["artist_name"] == "Fixture Artist"
        assert request["track_name"] == "Fixture Title"


def test_only_sidecars_created(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    per_format_lib(lib, "t")
    before = listing(lib)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    after = listing(lib)
    assert set(before) <= set(after)
    created = set(after) - set(before)
    assert len(created) == 5
    assert all(name.endswith((".lrc", ".txt")) for name in created)


def test_snapshot_detects_touch(tmp_path: Path) -> None:
    copy = Path(shutil.copyfile(FIXTURES / "t.flac", tmp_path / "t.flac"))
    before = snap(copy)
    # The bytes stay identical, so only the metadata fields can catch this.
    os.utime(copy, ns=(before[2] + 1_000_000_000, before[2] + 1_000_000_000))

    assert hashlib.sha256(copy.read_bytes()).hexdigest() == before[0]
    assert snap(copy) != before


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_embedded_token_fixture_has_full_tags_and_the_token(suffix: str) -> None:
    path = FIXTURES / f"embedded_token{suffix}"

    found = TinyTag.get(path)

    assert found.artist == "Fixture Artist"
    assert found.title == "Fixture Title"
    assert EMBEDDED_TOKEN in path.read_bytes()


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_edge_fixtures_have_the_tags_their_names_say(suffix: str) -> None:
    album_artist_only = TinyTag.get(FIXTURES / f"albumartist_only{suffix}")
    various = TinyTag.get(FIXTURES / f"various_artists{suffix}")
    untagged = TinyTag.get(FIXTURES / f"notags{suffix}")

    assert (album_artist_only.artist, album_artist_only.albumartist) == (
        None,
        "Fixture Album Artist",
    )
    assert (various.artist, various.albumartist) == (None, "Various Artists")
    assert (untagged.artist, untagged.albumartist, untagged.title) == (None, None, None)


def test_id3v24_fixture_uses_version_four() -> None:
    assert (FIXTURES / "t_id3v24.mp3").read_bytes()[:4] == b"ID3\x04"
    assert (FIXTURES / "t.mp3").read_bytes()[:4] == b"ID3\x03"


def test_an_unsynchronised_id3v23_tag_is_looked_up_by_its_real_title_and_left_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    tracks = []
    for name in ("id3v23_unsync.flac", "id3v23_unsync.mp3"):
        folder = lib / name.rsplit(".", 1)[1]
        folder.mkdir(parents=True)
        tracks.append(Path(shutil.copy(FIXTURES / name, folder)))
    before = [snap(track) for track in tracks]

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert [snap(track) for track in tracks] == before
    assert parse_counts(proc.stdout)["synced"] == 2
    assert [(r["artist_name"], r["track_name"]) for r in stub_lrclib.requests] == [
        ("Fixture Artist", "Fixture Title")
    ] * 2
