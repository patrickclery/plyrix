"""Read the variables Lidarr's Custom Script connection sets for an event.

Lidarr 3.1.0.4875's CustomScript.cs spells the names in mixed case, such as Lidarr_EventType, but
it collects them in a .NET StringDictionary, which lowercases its keys. So on Linux, where names
are case-sensitive, the script really gets lidarr_eventtype and the rest, as a real Lidarr showed
on 2026-11-15. Both spellings are read, the mixed-case one first, and nothing else counts. Events
are always handed on under the mixed-case names. An upgrade arrives as an AlbumDownload with
Lidarr_DeletedPaths set; Lidarr has no separate event type for it.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

EVENT_TYPE = "Lidarr_EventType"
ADDED_TRACK_PATHS = "Lidarr_AddedTrackPaths"
DELETED_PATHS = "Lidarr_DeletedPaths"
ARTIST_PATH = "Lidarr_Artist_Path"
ALBUM_ID = "Lidarr_Album_Id"
KEYS = (EVENT_TYPE, ADDED_TRACK_PATHS, DELETED_PATHS, ARTIST_PATH, ALBUM_ID)
# What Lidarr really sets on Linux.
LOWERCASE_KEYS = tuple(key.lower() for key in KEYS)
ALBUM_DOWNLOAD = "AlbumDownload"
TEST = "Test"
# Lidarr joins each path list with this.
_SEPARATOR = "|"
# A path map entry is FROM=TO, split here, so TO is always absolute.
_MAP_SPLIT = "=/"
_SLASH = "/"


@dataclass(frozen=True)
class Event:
    """One Lidarr event, with its values as Lidarr gave them."""

    type: str
    added: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    artist_path: str = ""
    album_id: str = ""

    @property
    def is_upgrade(self) -> bool:
        """Whether the import replaced files the album already had."""
        return bool(self.deleted)


def fields(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the event's variables under their mixed-case names, from either spelling.

    A non-empty mixed-case value wins, then a non-empty lowercase one. A name set only empty is
    kept empty, and a name set in neither spelling is left out, as Lidarr leaves out a list it has
    nothing for.
    """
    found: dict[str, str] = {}
    for key, lower in zip(KEYS, LOWERCASE_KEYS, strict=True):
        value = environ.get(key) or environ.get(lower)
        if value:
            found[key] = value
        elif key in environ or lower in environ:
            found[key] = ""
    return found


def parse(environ: Mapping[str, str]) -> Event | None:
    """Return the event in environ, or None when its event type is missing or empty."""
    values = fields(environ)
    event_type = values.get(EVENT_TYPE, "")
    if not event_type:
        return None
    return Event(
        type=event_type,
        added=_split(values.get(ADDED_TRACK_PATHS, "")),
        deleted=_split(values.get(DELETED_PATHS, "")),
        artist_path=values.get(ARTIST_PATH, ""),
        album_id=values.get(ALBUM_ID, ""),
    )


def parse_path_map(entries: Sequence[str]) -> list[tuple[str, str]]:
    """Return each FROM=TO entry as a pair, with trailing slashes stripped.

    Raises ValueError with a reason that holds no entry text, as an entry may be private.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        source, split, target = entry.partition(_MAP_SPLIT)
        if not split or not source:
            raise ValueError("each entry is FROM=TO, both absolute")
        source = _trimmed(source)
        target = _trimmed(_SLASH + target)
        if not source.startswith(_SLASH):
            raise ValueError("each entry is FROM=TO, both absolute")
        if source in seen:
            raise ValueError("a FROM folder is listed twice")
        try:
            os.fsencode(source)
            os.fsencode(target)
        except UnicodeEncodeError:
            raise ValueError("an entry holds text no path can") from None
        seen.add(source)
        pairs.append((source, target))
    return pairs


def map_path(path: str, path_map: Sequence[tuple[str, str]]) -> str:
    """Return path with the longest matching FROM swapped for its TO, matching whole folders."""
    best: tuple[str, str] | None = None
    for source, target in path_map:
        if _under(path, source) and (best is None or len(source) > len(best[0])):
            best = (source, target)
    if best is None:
        return path
    source, target = best
    rest = path[len(source) :] if source != _SLASH else path[1:]
    if not rest:
        return target
    rest = rest.lstrip(_SLASH)
    return target + rest if target == _SLASH else f"{target}{_SLASH}{rest}"


def album_folders(
    event: Event,
    path_map: Sequence[tuple[str, str]] = (),
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> list[str]:
    """Return the folders to run for the event, mapped onto plyrix's view of the library.

    These are the distinct folders holding the added tracks, when each one exists. Otherwise it
    is the artist folder alone, which is cheap to walk, as an unchanged track costs only a stat. A
    name Lidarr couldn't pass on intact won't exist, so it falls back the same way. With nothing
    usable, it is none.
    """
    folders = sorted({map_path(os.path.dirname(path), path_map) for path in event.added})
    if folders and all(is_dir(folder) for folder in folders):
        return folders
    artist = map_path(event.artist_path, path_map) if event.artist_path else ""
    if artist and is_dir(artist):
        return [artist]
    return []


def _under(path: str, folder: str) -> bool:
    if folder == _SLASH:
        return path.startswith(_SLASH)
    return path == folder or path.startswith(folder + _SLASH)


def _trimmed(path: str) -> str:
    stripped = path.rstrip(_SLASH)
    return stripped or (_SLASH if path else "")


def _split(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split(_SEPARATOR) if item)
