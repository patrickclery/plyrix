"""Lidarr gate: a real Lidarr import reaches plyrix through the spool shim, end to end.

Skipped unless PLYRIX_LIDARR_E2E names a scratch folder, and never run in hosted CI. It runs a
throwaway Lidarr from the local linuxserver/lidarr image, never pulled, with a container name and
port of its own and mounts only from a fresh run folder inside the scratch folder. A copy of the
album in PLYRIX_LIDARR_E2E_ALBUM is imported for real, through a Custom Script connection to
examples/lidarr/lidarr-spool.sh, and host plyrix drains the queue against the local LRCLIB at
PLYRIX_LRCLIB_CONTRACT_URL. The only internet use is Lidarr's own metadata server.

It checks that Lidarr's Test passes and fails as the spool allows, that the import queues one
event, that the drain writes sidecars and leaves every audio file byte-identical, and that with
the state database held the hook queues the album and a later drain runs it. Teardown removes the
container and the run folder, and the containers and images present before must be unchanged.

Its output and its result file hold counts, booleans, the Lidarr version and the date, and no
artist, album, track name or path.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from plyrix import lidarr, spool, tags
from plyrix.domain import AUDIO_SUFFIXES
from plyrix.state import db

IMAGE = "linuxserver/lidarr:latest"
NAME = "plyrix-phase7-lidarr"
PORT = 18686
BASE = f"http://127.0.0.1:{PORT}/api/v1"
SHIM = Path(__file__).resolve().parents[2] / "examples" / "lidarr" / "lidarr-spool.sh"
PLYRIX = Path(sys.executable).parent / "plyrix"
# Docker's own names for a container started without --name: adjective_surname.
GENERATED_NAME = re.compile(r"^[a-z]+_[a-z]+$")
START_TIMEOUT = 180
ARTIST_TIMEOUT = 300
EVENT_TIMEOUT = 60
COMMAND_TIMEOUT = 600

pytestmark = [
    pytest.mark.lidarr_e2e,
    pytest.mark.skipif(
        not os.environ.get("PLYRIX_LIDARR_E2E"),
        reason="set PLYRIX_LIDARR_E2E to a scratch folder for the throwaway Lidarr",
    ),
]


def docker(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        pytest.fail("PLYRIX_LIDARR_E2E is set but docker is not on PATH")
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def containers() -> dict[str, tuple[str, str]]:
    """Return each container's name, with its ID and state."""
    proc = docker("ps", "-a", "--no-trunc", "--format", "{{.Names}}\t{{.ID}}\t{{.State}}")
    assert proc.returncode == 0, "docker ps failed"
    listed = {}
    for line in proc.stdout.splitlines():
        name, ident, state = line.split("\t")
        listed[name] = (ident, state)
    return listed


def images() -> list[str]:
    proc = docker("images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}")
    assert proc.returncode == 0, "docker images failed"
    return sorted(proc.stdout.splitlines())


def settings() -> tuple[Path, Path, str]:
    """Return the scratch folder, the album to copy and LRCLIB's URL, failing with a reason."""
    scratch = Path(os.environ["PLYRIX_LIDARR_E2E"]).resolve()
    album_value = os.environ.get("PLYRIX_LIDARR_E2E_ALBUM")
    url = os.environ.get("PLYRIX_LRCLIB_CONTRACT_URL")
    if not scratch.is_dir():
        pytest.fail("PLYRIX_LIDARR_E2E is not a folder")
    if not album_value or not Path(album_value).is_dir():
        pytest.fail("set PLYRIX_LIDARR_E2E_ALBUM to the album folder to copy")
    album = Path(album_value).resolve()
    if album == scratch or scratch in album.parents:
        pytest.fail("PLYRIX_LIDARR_E2E_ALBUM must be outside PLYRIX_LIDARR_E2E")
    if not url:
        pytest.fail("set PLYRIX_LRCLIB_CONTRACT_URL to the local LRCLIB")
    host = (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    if host == "lrclib.net" or host.endswith(".lrclib.net"):
        pytest.fail("PLYRIX_LRCLIB_CONTRACT_URL must be a self-hosted LRCLIB")
    return scratch, album, url


_AUDIO = {suffix.decode() for suffix in AUDIO_SUFFIXES}


def audio_hashes(folder: Path) -> dict[str, str]:
    """Return each audio file's sha256, by its path relative to folder."""
    hashes = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in _AUDIO:
            hashes[str(path.relative_to(folder))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def sidecars(folder: Path) -> int:
    return sum(1 for path in folder.rglob("*") if path.suffix in (".lrc", ".txt"))


class Lidarr:
    """The throwaway Lidarr's v1 API, with its key read from its own config."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.key = ""

    def wait(self) -> str:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            match = None
            config_xml = self.config / "config.xml"
            if config_xml.exists():
                match = re.search(r"<ApiKey>([^<]+)</ApiKey>", config_xml.read_text())
            if match:
                self.key = match.group(1)
                try:
                    status, body = self.api("GET", "/system/status")
                except OSError:
                    status, body = 0, None
                if status == 200 and isinstance(body, dict):
                    return str(body["version"])
            time.sleep(2)
        pytest.fail("the throwaway Lidarr didn't start in time")

    def api(
        self, method: str, path: str, body: object = None, params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-Api-Key", self.key)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, None

    def command(self, body: dict[str, object]) -> str:
        status, started = self.api("POST", "/command", body)
        assert status in (200, 201), f"Lidarr refused a command: {status}"
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while time.monotonic() < deadline:
            _, current = self.api("GET", f"/command/{started['id']}")
            state = str(current.get("status"))
            if state in ("completed", "failed", "aborted", "cancelled"):
                return state
            time.sleep(2)
        pytest.fail("a Lidarr command didn't finish in time")


def first_artist(album: Path) -> str:
    for path in sorted(album.rglob("*")):
        if path.is_file() and path.suffix.lower() in _AUDIO:
            read = tags.read(os.fsencode(path))
            # An ID3v1 field comes padded with spaces.
            name = (read.albumartist or read.artist or "").strip()
            if name:
                return name
    pytest.fail("no audio file in the album has an artist tag")


def profiles(api: Lidarr) -> tuple[list[Any], list[Any]]:
    """Return the quality and metadata profiles, which a first start fills in after it answers."""
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        _, quality = api.api("GET", "/qualityprofile")
        _, metadata = api.api("GET", "/metadataprofile")
        if quality and metadata:
            return quality, metadata
        time.sleep(2)
    pytest.fail("the throwaway Lidarr has no quality or metadata profile")


def add_artist(api: Lidarr, artist: str) -> None:
    quality, metadata = profiles(api)
    status, _ = api.api(
        "POST",
        "/rootfolder",
        {
            "path": "/music",
            "name": "scratch",
            "defaultQualityProfileId": quality[0]["id"],
            "defaultMetadataProfileId": metadata[0]["id"],
            "defaultMonitorOption": "none",
            "defaultNewItemMonitorOption": "none",
            "defaultTags": [],
        },
    )
    assert status in (200, 201), f"Lidarr refused the root folder: {status}"
    status, found = api.api("GET", "/artist/lookup", params={"term": artist})
    if status != 200 or not isinstance(found, list):
        pytest.fail(f"Lidarr's metadata server didn't answer the artist lookup: {status}")
    matches = [item for item in found if item.get("artistName", "").casefold() == artist.casefold()]
    assert matches, "Lidarr's metadata server knows no artist by the album's tag"
    chosen = matches[0]
    chosen.update(
        monitored=False,
        qualityProfileId=quality[0]["id"],
        metadataProfileId=metadata[0]["id"],
        rootFolderPath="/music",
        addOptions={"monitor": "none", "searchForMissingAlbums": False},
        monitorNewItems="none",
    )
    status, added = api.api("POST", "/artist", chosen)
    assert status in (200, 201), f"Lidarr refused the artist: {status}"
    deadline = time.monotonic() + ARTIST_TIMEOUT
    while time.monotonic() < deadline:
        _, albums = api.api("GET", "/album", params={"artistId": str(added["id"])})
        if albums:
            return
        time.sleep(3)
    pytest.fail("Lidarr didn't load the artist's albums in time")


def connection(api: Lidarr) -> dict[str, Any]:
    status, schema = api.api("GET", "/notification/schema")
    assert status == 200, f"Lidarr gave no notification schema: {status}"
    (custom,) = [item for item in schema if item.get("implementation") == "CustomScript"]
    for field in custom["fields"]:
        if field["name"] == "path":
            field["value"] = "/scripts/lidarr-spool.sh"
    custom.update(name="plyrix", onReleaseImport=True, onUpgrade=True)
    return dict(custom)


def events(folder: Path) -> list[Path]:
    return spool.entries(folder)


def wait_for_event(folder: Path) -> bool:
    deadline = time.monotonic() + EVENT_TIMEOUT
    while time.monotonic() < deadline:
        if events(folder):
            return True
        time.sleep(2)
    return False


def manual_import(api: Lidarr, folder: str) -> None:
    status, items = api.api(
        "GET",
        "/manualimport",
        params={
            "folder": folder,
            "filterExistingFiles": "true",
            "replaceExistingFiles": "true",
        },
    )
    assert status == 200 and items, f"Lidarr's manual import found nothing: {status}"
    files = [
        {
            "path": item["path"],
            "artistId": item["artist"]["id"],
            "albumId": item["album"]["id"],
            "albumReleaseId": item["albumReleaseId"],
            "trackIds": [track["id"] for track in item["tracks"]],
            "quality": item["quality"],
            "indexerFlags": 0,
            "disableReleaseSwitching": False,
        }
        for item in items
        if item.get("album") and item.get("tracks")
    ]
    assert files, "Lidarr identified no file of the album"
    state = api.command(
        {
            "name": "ManualImport",
            "files": files,
            "importMode": "copy",
            "replaceExistingFiles": True,
        }
    )
    assert state == "completed", f"Lidarr's manual import ended {state}"


def plyrix(run: Path, lrclib_url: str, *args: str, env: dict[str, str] | None = None) -> Any:
    inherited = {
        k: v for k, v in os.environ.items() if not k.lower().startswith(("plyrix_", "lidarr_"))
    }
    return subprocess.run(
        [str(PLYRIX), *args],
        env={
            **inherited,
            "XDG_STATE_HOME": str(run / "xdg-state"),
            "XDG_CONFIG_HOME": str(run / "xdg-config"),
            "PLYRIX_MUSIC_ROOT": str(run / "music"),
            "PLYRIX_LIDARR_PATH_MAP": json.dumps([f"/music={run / 'music'}"]),
            "PLYRIX_LIDARR_SPOOL": str(run / "spool"),
            "PLYRIX_STATE_DB": str(run / "state" / "state.db"),
            "PLYRIX_LRCLIB_URL": lrclib_url,
            **(env or {}),
        },
        capture_output=True,
        check=False,
        timeout=1800,
    )


def counts(stdout: bytes) -> dict[str, int]:
    found = re.findall(rb"^([a-z_]+)=(\d+)$", stdout, re.MULTILINE)
    return {key.decode(): int(value) for key, value in found}


@pytest.fixture
def run_folder() -> Iterator[tuple[Path, dict[str, object]]]:
    """A fresh run folder and the result, with teardown that always runs and is checked."""
    scratch, _, _ = settings()
    image = docker("image", "inspect", "--format", "{{.Id}}", IMAGE)
    if image.returncode != 0:
        pytest.fail("the local linuxserver/lidarr image is missing, and the gate never pulls")
    before_containers = containers()
    before_images = images()
    if NAME in before_containers:
        pytest.fail("a container named plyrix-phase7-lidarr already exists")
    run = Path(tempfile.mkdtemp(prefix="run-", dir=scratch))
    result: dict[str, object] = {
        "date": datetime.date.today().isoformat(),
        "image_id": image.stdout.strip(),
    }
    try:
        yield run, result
    finally:
        docker("rm", "-f", NAME)
        try:
            shutil.rmtree(run)
        except OSError:
            # Something the container's user owns, removed with the same image, never pulled.
            docker(
                "run", "--rm", "--pull", "never", "--entrypoint", "sh",
                "-v", f"{run}:/run-folder", IMAGE,
                "-c", "rm -rf /run-folder/* /run-folder/.[!.]*",
            )  # fmt: skip
            shutil.rmtree(run, ignore_errors=True)
        after_containers = containers()
        named = {
            name: entry
            for name, entry in before_containers.items()
            if not GENERATED_NAME.fullmatch(name)
        }
        result["named_containers_unchanged"] = all(
            after_containers.get(name) == entry for name, entry in named.items()
        )
        result["gate_container_gone"] = NAME not in after_containers
        result["generated_names_changed"] = len(
            {n for n in before_containers if GENERATED_NAME.fullmatch(n)}
            ^ {n for n in after_containers if GENERATED_NAME.fullmatch(n)}
        )
        result["images_unchanged"] = images() == before_images
        result["run_folder_removed"] = not run.exists()
        destination = os.environ.get("PLYRIX_GATE_RESULT")
        if destination:
            Path(destination).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "image_id"}))


def test_a_real_lidarr_import_reaches_plyrix(
    run_folder: tuple[Path, dict[str, object]],
) -> None:
    run, result = run_folder
    _, album, lrclib_url = settings()
    for name in ("config", "music", "downloads", "spool", "scripts", "state"):
        (run / name).mkdir(mode=0o755)
    shutil.copyfile(SHIM, run / "scripts" / "lidarr-spool.sh")
    (run / "scripts" / "lidarr-spool.sh").chmod(0o755)
    started = docker(
        "run", "-d", "--pull", "never", "--name", NAME,
        "-p", f"127.0.0.1:{PORT}:8686",
        "-e", f"PUID={os.getuid()}", "-e", f"PGID={os.getgid()}", "-e", "TZ=Etc/UTC",
        "-v", f"{run / 'config'}:/config",
        "-v", f"{run / 'music'}:/music",
        "-v", f"{run / 'downloads'}:/downloads",
        "-v", f"{run / 'spool'}:/plyrix-spool",
        "-v", f"{run / 'scripts'}:/scripts:ro",
        IMAGE,
    )  # fmt: skip
    assert started.returncode == 0, "the throwaway Lidarr didn't start"
    api = Lidarr(run / "config")
    result["lidarr_version"] = api.wait()

    # The connection, and Lidarr's Test button (HOOK-01, D-14).
    add_artist(api, first_artist(album))
    custom = connection(api)
    status, _ = api.api("POST", "/notification/test", custom)
    result["test_passes"] = 200 <= status < 300
    result["test_wrote_nothing"] = list((run / "spool").iterdir()) == []
    (run / "spool").chmod(0o555)
    try:
        status, _ = api.api("POST", "/notification/test", custom)
    finally:
        (run / "spool").chmod(0o755)
    result["test_fails_on_an_unwritable_spool"] = 400 <= status < 500
    status, _ = api.api("POST", "/notification", custom)
    assert status in (200, 201), f"Lidarr refused the connection: {status}"
    assert result["test_passes"] and result["test_wrote_nothing"]
    assert result["test_fails_on_an_unwritable_spool"]

    # A real import (D-12, D-21).
    source_before = audio_hashes(album)
    download = run / "downloads" / "album"
    shutil.copytree(album, download)
    state = api.command(
        {"name": "DownloadedAlbumsScan", "path": "/downloads/album", "importMode": "Copy"}
    )
    result["import_path"] = "DownloadedAlbumsScan"
    if not wait_for_event(run / "spool"):
        manual_import(api, "/downloads/album")
        result["import_path"] = "ManualImport"
        assert wait_for_event(run / "spool"), f"no event after either import ({state})"
    queued = events(run / "spool")
    result["events_after_import"] = len(queued)
    assert len(queued) == 1
    event = lidarr.parse(spool.read(queued[0]))
    assert event is not None and event.type == lidarr.ALBUM_DOWNLOAD
    result["event_paths_under_music"] = bool(event.added) and all(
        path.startswith("/music/") for path in event.added
    )
    assert result["event_paths_under_music"]
    fields = spool.read(queued[0])

    # Host plyrix drains it (HOOK-03).
    baseline = audio_hashes(run / "music")
    result["library_audio_files"] = len(baseline)
    assert baseline, "Lidarr imported no audio file"
    drained = plyrix(run, lrclib_url, "drain")
    drain_counts = counts(drained.stdout)
    result["drain_exit"] = drained.returncode
    result["drained"] = drain_counts.get("drained", 0)
    written = sidecars(run / "music")
    result["sidecars_written"] = written
    result["spool_empty_after_drain"] = events(run / "spool") == []
    result["audio_unchanged_after_drain"] = audio_hashes(run / "music") == baseline
    downloaded = audio_hashes(download)
    result["lidarr_kept_the_download_bytes"] = (
        "same" if sorted(downloaded.values()) == sorted(baseline.values()) else "retagged"
    )
    assert drained.returncode == 0, f"drain exited {drained.returncode}"
    assert result["drained"] == 1
    assert written >= 1
    assert result["spool_empty_after_drain"]
    assert result["audio_unchanged_after_drain"]

    # With the state database held, the hook queues the album, and a drain runs it (HOOK-04).
    holder = db.connect(run / "state" / "state.db")
    try:
        # Under the lowercase names, as Lidarr itself sets them on Linux.
        lowercase = {name.lower(): value for name, value in fields.items()}
        replay = plyrix(run, lrclib_url, "lidarr-hook", env=lowercase)
    finally:
        holder.close()
    result["locked_hook_exit"] = replay.returncode
    result["locked_hook_queued"] = len(events(run / "spool"))
    assert replay.returncode == 0
    assert result["locked_hook_queued"] == 1
    again = plyrix(run, lrclib_url, "drain")
    result["second_drain_exit"] = again.returncode
    result["second_drain_drained"] = counts(again.stdout).get("drained", 0)
    result["spool_empty_after_second_drain"] = events(run / "spool") == []
    result["audio_unchanged_at_the_end"] = audio_hashes(run / "music") == baseline
    result["source_unchanged"] = audio_hashes(album) == source_before
    assert again.returncode == 0
    assert result["second_drain_drained"] == 1
    assert result["spool_empty_after_second_drain"]
    assert result["audio_unchanged_at_the_end"]
    assert result["source_unchanged"]


def test_the_teardown_left_the_host_as_it_was() -> None:
    """Reads the result the first test's teardown wrote, when one was asked for."""
    destination = os.environ.get("PLYRIX_GATE_RESULT")
    if not destination or not Path(destination).exists():
        pytest.skip("set PLYRIX_GATE_RESULT to check the teardown")
    result = json.loads(Path(destination).read_text(encoding="utf-8"))
    for key in (
        "named_containers_unchanged",
        "gate_container_gone",
        "images_unchanged",
        "run_folder_removed",
    ):
        assert result.get(key) is True, key
