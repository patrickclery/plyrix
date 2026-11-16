"""Lidarr's view of the library mapped onto plyrix's: the path map and the album folders."""

import os
from pathlib import Path

import pytest

from conftest import lidarr_env
from plyrix import config, lidarr
from plyrix.config import ConfigError, Settings

MAP = [("/music", "/srv/music")]


def test_map_path_swaps_the_matching_prefix() -> None:
    assert lidarr.map_path("/music/A/t.flac", MAP) == "/srv/music/A/t.flac"
    assert lidarr.map_path("/music", MAP) == "/srv/music"


def test_map_path_matches_whole_folder_names() -> None:
    assert lidarr.map_path("/music2/A", MAP) == "/music2/A"
    assert lidarr.map_path("/musicA", MAP) == "/musicA"


def test_map_path_with_no_map_changes_nothing() -> None:
    assert lidarr.map_path("/music/A/t.flac", []) == "/music/A/t.flac"


def test_the_longest_from_wins() -> None:
    path_map = [("/music", "/x"), ("/music/sub", "/y")]

    assert lidarr.map_path("/music/sub/a", path_map) == "/y/a"
    assert lidarr.map_path("/music/other/a", path_map) == "/x/other/a"


def test_a_root_from_maps_everything() -> None:
    assert lidarr.map_path("/A/t.flac", [("/", "/srv/music")]) == "/srv/music/A/t.flac"


@pytest.mark.parametrize("entry", ["/music=/srv/music", "/music/=/srv/music/"])
def test_parse_path_map_strips_trailing_slashes(entry: str) -> None:
    assert lidarr.parse_path_map([entry]) == [("/music", "/srv/music")]


def test_parse_path_map_splits_at_the_first_equals_slash() -> None:
    assert lidarr.parse_path_map(["/a=b=/c"]) == [("/a=b", "/c")]


def test_parse_path_map_keeps_a_lone_slash() -> None:
    assert lidarr.parse_path_map(["/=/srv/music"]) == [("/", "/srv/music")]


@pytest.mark.parametrize(
    "entries",
    [
        [""],
        ["music=/x"],
        ["/music=x"],
        ["/music"],
        ["/music=/x", "/music/=/y"],
        ["/m\ud800=/x"],
    ],
)
def test_parse_path_map_refuses_a_bad_entry(entries: list[str]) -> None:
    with pytest.raises(ValueError) as caught:
        lidarr.parse_path_map(entries)

    for entry in entries:
        if entry:
            assert entry not in str(caught.value)


def event(**environ: str) -> lidarr.Event:
    parsed = lidarr.parse({"Lidarr_EventType": "AlbumDownload", **environ})
    assert parsed is not None
    return parsed


def is_dir_in(folders: set[str]):  # type: ignore[no-untyped-def]
    return lambda path: path in folders


def test_a_multi_disc_album_gives_every_disc() -> None:
    album = event(Lidarr_AddedTrackPaths="/m/A/CD2/1.flac|/m/A/CD1/1.flac|/m/A/CD1/2.flac")

    folders = lidarr.album_folders(album, (), is_dir_in({"/m/A/CD1", "/m/A/CD2"}))

    assert folders == ["/m/A/CD1", "/m/A/CD2"]


def test_three_tracks_in_one_folder_give_it_once() -> None:
    album = event(Lidarr_AddedTrackPaths="/m/A/1.flac|/m/A/2.flac|/m/A/3.flac")

    assert lidarr.album_folders(album, (), is_dir_in({"/m/A"})) == ["/m/A"]


def test_the_folders_are_mapped() -> None:
    album = event(Lidarr_AddedTrackPaths="/music/A/1.flac", Lidarr_Artist_Path="/music")

    assert lidarr.album_folders(album, MAP, is_dir_in({"/srv/music/A"})) == ["/srv/music/A"]


def test_a_missing_folder_falls_back_to_the_mapped_artist_folder() -> None:
    album = event(Lidarr_AddedTrackPaths="/music/Art/A/1.flac", Lidarr_Artist_Path="/music/Art")

    folders = lidarr.album_folders(album, MAP, is_dir_in({"/srv/music/Art"}))

    assert folders == ["/srv/music/Art"]


def test_no_added_paths_gives_the_artist_folder() -> None:
    album = event(Lidarr_Artist_Path="/m/Art")

    assert lidarr.album_folders(album, (), is_dir_in({"/m/Art"})) == ["/m/Art"]


def test_a_name_lidarr_garbled_gives_the_artist_folder() -> None:
    album = event(Lidarr_AddedTrackPaths="/m/Art/A�/1.flac", Lidarr_Artist_Path="/m/Art")

    assert lidarr.album_folders(album, (), is_dir_in({"/m/Art"})) == ["/m/Art"]


def test_nothing_usable_gives_no_folder() -> None:
    album = event(Lidarr_AddedTrackPaths="/m/A/1.flac", Lidarr_Artist_Path="/m/Art")

    assert lidarr.album_folders(album, (), is_dir_in(set())) == []
    assert lidarr.album_folders(event(), (), is_dir_in(set())) == []


def test_the_default_checks_the_real_filesystem(tmp_path: Path) -> None:
    (tmp_path / "A").mkdir()
    album = event(Lidarr_AddedTrackPaths=f"{tmp_path}/A/1.flac")

    assert lidarr.album_folders(album) == [f"{tmp_path}/A"]


def load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **environ: str) -> Settings:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", str(lib))
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", "http://127.0.0.1:1/api")
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    settings, _ = Settings.load(None, {})
    return settings


def test_the_path_map_loads_from_a_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load(tmp_path, monkeypatch, PLYRIX_LIDARR_PATH_MAP=f'["/music={tmp_path / "lib"}"]')

    assert settings.lidarr_path_map == [f"/music={tmp_path / 'lib'}"]


def test_the_path_map_loads_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml = tmp_path / "config" / "plyrix" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(f'lidarr_path_map = ["/music={tmp_path / "lib" / "A"}"]\n')
    (tmp_path / "lib" / "A").mkdir(parents=True)

    settings = load(tmp_path, monkeypatch)

    assert settings.lidarr_path_map == [f"/music={tmp_path / 'lib' / 'A'}"]


def test_the_path_map_defaults_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(tmp_path, monkeypatch).lidarr_path_map == []


SETTING = "lidarr_path_map (PLYRIX_LIDARR_PATH_MAP)"


def test_a_to_outside_music_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = f'["/music={tmp_path / "elsewhere"}"]'

    with pytest.raises(ConfigError) as caught:
        load(tmp_path, monkeypatch, PLYRIX_LIDARR_PATH_MAP=value)

    assert SETTING in caught.value.message
    assert "music_root (PLYRIX_MUSIC_ROOT)" in caught.value.message
    assert "elsewhere" not in caught.value.message
    assert "/music" not in caught.value.message


def test_a_to_reached_through_dotdot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = f'["/music={tmp_path / "lib"}/../elsewhere"]'

    with pytest.raises(ConfigError) as caught:
        load(tmp_path, monkeypatch, PLYRIX_LIDARR_PATH_MAP=value)

    assert SETTING in caught.value.message


def test_a_malformed_entry_is_refused_without_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, monkeypatch, PLYRIX_LIDARR_PATH_MAP='["secretname=relative"]')

    assert SETTING in caught.value.message
    assert "FROM=TO" in caught.value.message
    assert "secretname" not in caught.value.message
    assert "relative" not in caught.value.message


def test_contain_still_checks_a_mapped_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load(tmp_path, monkeypatch)
    inside, refused = config.contain(settings, [lidarr.map_path("/music/x", [("/music", "/")])])

    assert inside == []
    assert refused == [os.fsencode("/x")]


def test_lidarr_env_spells_the_names_exactly() -> None:
    env = lidarr_env(added=("/m/A/1.flac", "/m/A/2.flac"), artist="/m", album_id="7")

    assert env == {
        "Lidarr_EventType": "AlbumDownload",
        "Lidarr_AddedTrackPaths": "/m/A/1.flac|/m/A/2.flac",
        "Lidarr_Artist_Path": "/m",
        "Lidarr_Album_Id": "7",
    }


def test_lidarr_env_sets_deleted_paths_only_on_an_upgrade() -> None:
    plain = lidarr_env()
    upgrade = lidarr_env(deleted=("/m/A/old.mp3",))

    assert "Lidarr_AddedTrackPaths" not in plain
    assert "Lidarr_DeletedPaths" not in plain
    assert upgrade["Lidarr_DeletedPaths"] == "/m/A/old.mp3"
