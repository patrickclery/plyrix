"""A run's report for a scheduler: counts and run facts, and no path, tag or lyric text.

It writes outside the library only. Config refuses a report path inside it, and the guard stops
anything that reaches it some other way.
"""

import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path


def now() -> str:
    """Return the time as a report states it."""
    return datetime.now(UTC).isoformat()


def write(path: Path, payload: Mapping[str, object]) -> None:
    """Put the payload at an absolute path as JSON, so a reader sees all of it or none of it.

    A folder there is refused first, as the rename would fail with the temp already made. The temp
    has a random name and is created exclusively, so it can't follow a symlink or reuse a file left
    at that name, and the folder may be shared. Any failure removes the temp. Raises OSError.
    """
    if path.is_dir():
        raise IsADirectoryError(path)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    # This raises before the temp is ours, so nothing above the try needs cleaning up.
    out = temp.open("x", encoding="utf-8")
    try:
        with out:
            out.write(json.dumps(payload, indent=2) + "\n")
        temp.replace(path)
    except BaseException:
        _remove_quietly(temp)
        raise


def _remove_quietly(temp: Path) -> None:
    """Remove a temp, without letting a failure replace the error that got here."""
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
