"""Typed settings. The music root and the LRCLIB URL have no default, by design.

Sources, highest first: CLI flags, PLYRIX_* variables, secret files, then a TOML file. Secret
files are read from Docker's secrets directory and systemd's credentials directory. TOML text
is Unicode, so it can't hold a non-UTF-8 path: such a root has to come from a variable or the run
path.
"""

import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from platformdirs import user_config_path, user_state_path
from pydantic import AnyHttpUrl, Field, PrivateAttr, SecretStr, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
    SettingsError,
    TomlConfigSettingsSource,
)

from plyrix import lidarr

_ENV_PREFIX = "PLYRIX_"
# These configure the repo's scripts and test gates, and are not settings.
_TOOLING_ENV = frozenset(
    {
        "PLYRIX_DENY_LIST",
        "PLYRIX_REAL_SAMPLES",
        "PLYRIX_GATE_RESULT",
        "PLYRIX_REQUIRE_GITLEAKS",
        "PLYRIX_LRCLIB_CONTRACT_URL",
        "PLYRIX_PLEX_CONTRACT_URL",
        "PLYRIX_PLEX_CONTRACT_SECTION",
        "PLYRIX_PLEX_CONTRACT_TOKEN",
        "PLYRIX_DOCKER_GATE",
        "PLYRIX_LIDARR_E2E",
        "PLYRIX_LIDARR_E2E_ALBUM",
        "PLYRIX_LRCLIB_UPDATE_GATE",
    }
)
_SOURCE_ERROR_FIELD = re.compile(r'field "(\w+)"')
_PUBLIC_LRCLIB_HOST = "lrclib.net"
# A Plex token sent here has broken the server record, so plyrix never talks to it.
_PLEX_ACCOUNT_HOST = "plex.tv"
# Where Docker mounts a compose secret. systemd's LoadCredential names its own in a variable.
_DOCKER_SECRETS_DIR = "/run/secrets"
_CREDENTIALS_VARIABLE = "CREDENTIALS_DIRECTORY"
_DUMP_ID_MAX_LENGTH = 255
# A lone surrogate is how an undecodable byte arrives from argv or a variable, and SQLite can't
# store one, so it would otherwise stop the run with a traceback.
_NOT_IN_A_DUMP_ID = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
# One track in about 12 days. Far below this, the wait is too long for time.sleep to take.
_SLOWEST_MAX_RATE = 1e-6


class ConfigError(Exception):
    """A config mistake, with one line for the user that names settings and never their values."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _SecretFileError(Exception):
    """A secret file that exists but can't be read, named by its setting, not the TOML's."""

    def __init__(self, field: str, why: str) -> None:
        super().__init__(field)
        self.message = f"plyrix: the secret file for {_setting(field)} {why}"


class _SecretDirError(_SecretFileError):
    """A secrets directory that can't be listed, which is no one setting's fault."""

    def __init__(self) -> None:
        Exception.__init__(self)
        self.message = "plyrix: a secrets directory can't be listed by this user"


class _SecretsSource(SecretsSettingsSource):
    """Secret files, where one that can't be read stops the load naming its setting."""

    @classmethod
    def find_case_path(cls, dir_path: Path, file_name: str, case_sensitive: bool) -> Path | None:
        # pydantic-settings lists the directory for every field, so an error here would
        # otherwise be blamed on whichever setting it looked up first.
        try:
            return super().find_case_path(dir_path, file_name, case_sensitive)
        except OSError:
            raise _SecretDirError from None

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        try:
            return super().get_field_value(field, field_name)
        except OSError:
            raise _SecretFileError(field_name, "can't be read by this user") from None
        except UnicodeDecodeError:
            raise _SecretFileError(field_name, "isn't UTF-8 text") from None


class Settings(BaseSettings):
    """Settings from init values, then the environment, then secret files, then TOML."""

    # An empty variable counts as unset. As a path it would mean the working directory.
    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX, extra="forbid", env_ignore_empty=True)
    _toml_path: ClassVar[Path | None] = None
    # Set by load when dump_id came from a flag, which is then an override of the live record.
    _dump_id_from_flag: bool = PrivateAttr(default=False)

    music_root: Path
    lrclib_url: AnyHttpUrl
    excludes: list[str] = Field(default_factory=list)
    state_db: Path = Field(default_factory=lambda: user_state_path("plyrix") / "state.db")
    verbose: bool = False
    dump_id: str = ""
    plain: Literal["write", "skip"] = "write"
    dry_run: bool = False
    # Turns the targeted retry off: after a dump update, every earlier miss and .txt is looked up.
    full: bool = False
    max_rate: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    sidecar_method: Literal["auto", "plain"] = "auto"
    report_file: Path | None = None
    # The refresh step is optional, so none of these is required, and the URL has no default.
    plex_url: AnyHttpUrl | None = None
    plex_section: int | None = Field(default=None, gt=0)
    plex_token: SecretStr | None = None
    # The music root as Plex sees it, which load sets to music_root when it isn't given (D-06).
    plex_music_root: Path | None = None
    plex_pace: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    # Lidarr's view of the library mapped onto this one, as FROM=TO entries (D-08).
    lidarr_path_map: list[str] = Field(default_factory=list)
    # Where the hook queues an album it couldn't finish, which load puts beside the state database
    # when it isn't given (D-05).
    lidarr_spool: Path | None = None
    # How long one Lidarr import may wait on the hook, checked between tracks (D-04).
    lidarr_budget: float = Field(default=60.0, gt=0, allow_inf_nan=False)

    @field_validator(
        "music_root", "state_db", "report_file", "plex_music_root", "lidarr_spool", mode="before"
    )
    @classmethod
    def _names_a_path(cls, value: object) -> object:
        if isinstance(value, str) and (not value or "\0" in value):
            raise ValueError("not a usable path")
        return value

    @field_validator("lrclib_url", "plex_url")
    @classmethod
    def _can_take_a_path(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        # The request path is appended to this, and it would land inside a query or a fragment.
        if url is not None and ("?" in str(url) or "#" in str(url)):
            raise ValueError("a base URL can't have a query or a fragment")
        return url

    @field_validator("lrclib_url")
    @classmethod
    def _is_self_hosted(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        # Only by name: a DNS alias or an IP address for the public server can't be seen from here.
        host = (url.host or "").rstrip(".").lower()
        if host == _PUBLIC_LRCLIB_HOST or host.endswith(f".{_PUBLIC_LRCLIB_HOST}"):
            raise ValueError("plyrix only uses a self-hosted LRCLIB")
        return url

    @field_validator("plex_url")
    @classmethod
    def _is_your_own_plex(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        # By name, like lrclib_url, and url.host leaves out any user@. A *.plex.direct name is
        # allowed, as it resolves to the address it encodes, which is the user's own server (D-17).
        if url is None:
            return url
        host = (url.host or "").rstrip(".").lower()
        if host == _PLEX_ACCOUNT_HOST or host.endswith(f".{_PLEX_ACCOUNT_HOST}"):
            raise ValueError("plyrix only talks to your own Plex Media Server")
        return url

    @field_validator("plex_token")
    @classmethod
    def _empty_is_unset(cls, token: SecretStr | None) -> SecretStr | None:
        # As with a variable, an empty secret file counts as unset.
        return token if token is not None and token.get_secret_value() else None

    @field_validator("excludes")
    @classmethod
    def _encode_as_paths(cls, excludes: list[str]) -> list[str]:
        # JSON or TOML can hold text that no path can, such as a lone surrogate. The walker
        # compares bytes, so it would otherwise fail on this mid-run.
        for exclude in excludes:
            try:
                os.fsencode(exclude)
            except UnicodeEncodeError:
                raise ValueError("not a usable path pattern") from None
            # The walker compares each pattern with a normalised path from the music root, so
            # any of these segments could never match, and the folder would be written into.
            if {"", ".", ".."} & set(exclude.rstrip("/").split("/")):
                raise ValueError("not a pattern relative to the music root")
        return excludes

    @field_validator("lidarr_path_map")
    @classmethod
    def _maps_absolute_folders(cls, entries: list[str]) -> list[str]:
        lidarr.parse_path_map(entries)
        return entries

    @field_validator("dump_id")
    @classmethod
    def _names_a_dump(cls, dump_id: str) -> str:
        if len(dump_id) > _DUMP_ID_MAX_LENGTH or _NOT_IN_A_DUMP_ID.search(dump_id):
            raise ValueError("not a usable dump id")
        return dump_id

    @field_validator("max_rate", "plex_pace", "plex_section", "lidarr_budget", mode="before")
    @classmethod
    def _is_not_a_switch(cls, value: object) -> object:
        # Pydantic would read a TOML true as 1, which would silently pace the run or pick a section.
        if isinstance(value, bool):
            raise ValueError("not a number")
        return value

    @field_validator("max_rate")
    @classmethod
    def _can_be_waited_for(cls, max_rate: float | None) -> float | None:
        if max_rate is not None and max_rate < _SLOWEST_MAX_RATE:
            raise ValueError("too slow to wait for")
        return max_rate

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_file = cls._toml_path or user_config_path("plyrix") / "config.toml"
        # A secret directory given at construction wins, and otherwise only the standard ones
        # that exist are read, as one that doesn't gives a warning on every load.
        secret_dirs = getattr(file_secret_settings, "secrets_dir", None) or _secret_dirs()
        if secret_dirs:
            file_secret_settings = _SecretsSource(settings_cls, secrets_dir=secret_dirs)
        return (
            init_settings,
            env_settings,
            file_secret_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=toml_file),
        )

    @classmethod
    def load(
        cls,
        path: str | Path | None,
        overrides: Mapping[str, object],
        config_file: Path | None = None,
    ) -> tuple["Settings", bytes]:
        """Return the settings and the absolute folder to walk, applying the run path rule (D-23).

        With no music root configured, path becomes the root. With one configured, path has to be
        inside it and only narrows the walk, so the guard and the ledger keep the root. Both come
        back fully resolved, because the guard trips on any relative write open. A path from a file
        or a variable has to be absolute after expanding ~, as its meaning must not depend on the
        working directory. Only the PATH argument and flags may be relative, since the user's shell
        is what they mean then, and a flag's leading ~ still means the home folder. Raises
        ConfigError for every config mistake.
        """
        if path == "":
            raise ConfigError("plyrix: the library path to run on is empty")
        # A file the user named has to exist, or the settings in it would silently not apply.
        if config_file is not None and not os.path.isfile(config_file):
            raise ConfigError("plyrix: the file given to --config does not exist")
        cls._toml_path = config_file
        try:
            settings, root_came_from_path = cls._from_sources(path, overrides)
        except ValidationError as error:
            raise ConfigError(_describe_validation(error)) from None
        except SettingsError as error:
            # pydantic-settings wraps what a source raises, which would read as a parse error.
            if isinstance(error.__cause__, _SecretFileError):
                raise ConfigError(error.__cause__.message) from None
            raise ConfigError(_describe_source_error(error)) from None
        except _SecretFileError as error:
            raise ConfigError(error.message) from None
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            raise ConfigError("plyrix: the config file is not valid TOML") from None
        except OSError:
            raise ConfigError("plyrix: the config file can't be read") from None
        finally:
            cls._toml_path = None

        music_root, state_db = settings.music_root, settings.state_db
        if not root_came_from_path and "music_root" not in overrides:
            music_root = _anchored(music_root, "music_root")
        # A shell leaves ~ alone after an equals sign, so --state-db=~/x.db arrives as typed.
        state_db = _expanded(state_db, "state_db")
        if "state_db" not in overrides:
            state_db = _anchored(state_db, "state_db")

        root = os.path.realpath(os.fsencode(music_root))
        start = root if path is None else os.path.realpath(os.fsencode(path))
        if not _inside(start, root):
            raise ConfigError(
                f"plyrix: the library path to run on must be inside music_root "
                f"({_ENV_PREFIX}MUSIC_ROOT)"
            )
        for _source, target in lidarr.parse_path_map(settings.lidarr_path_map):
            if not _inside(os.path.realpath(os.fsencode(target)), root):
                raise ConfigError(
                    f"plyrix: each {_setting('lidarr_path_map')} entry must map to a folder "
                    f"inside music_root ({_ENV_PREFIX}MUSIC_ROOT)"
                )
        if _inside(os.path.realpath(os.fsencode(state_db)), root):
            raise ConfigError(
                f"plyrix: state_db ({_ENV_PREFIX}STATE_DB) must be outside the library"
            )
        resolved: dict[str, Path] = {"music_root": Path(os.fsdecode(root)), "state_db": state_db}
        # Plex's view of the library, which may be another machine's path, so it is taken as given.
        if settings.plex_music_root is None:
            resolved["plex_music_root"] = resolved["music_root"]
        elif not settings.plex_music_root.is_absolute():
            raise ConfigError(f"plyrix: {_setting('plex_music_root')} must be an absolute path")
        resolved["lidarr_spool"] = _resolved_spool(
            settings.lidarr_spool, "lidarr_spool" in overrides, root, state_db
        )
        if settings.report_file is not None:
            resolved["report_file"] = _resolved_report(
                settings.report_file, "report_file" in overrides, root, state_db
            )
        loaded = settings.model_copy(update=resolved)
        loaded._dump_id_from_flag = bool(overrides.get("dump_id"))
        return loaded, start

    @property
    def dump_id_from_flag(self) -> bool:
        """Whether dump_id came from --dump-id, so it overrides the dump recorded live (D-07)."""
        return self._dump_id_from_flag

    @classmethod
    def _from_sources(
        cls, path: str | Path | None, overrides: Mapping[str, object]
    ) -> tuple["Settings", bool]:
        """Return the settings, and whether the run path had to stand in as the music root."""
        try:
            return cls(**overrides), False  # type: ignore[arg-type]
        except ValidationError as error:
            root_missing = any(
                problem["loc"] == ("music_root",) and problem["type"] == "missing"
                for problem in error.errors(include_input=False)
            )
            if path is None or not root_missing:
                raise
        return cls(**overrides, music_root=Path(path)), True  # type: ignore[arg-type]


def contain(settings: Settings, paths: Sequence[str | bytes]) -> tuple[list[bytes], list[bytes]]:
    """Split paths into those inside music_root and those refused, raising nothing.

    It is the test load applies to the run path: realpath first, so `..` and symlinks resolve
    before the check. Inside paths come back resolved and once each, in first-seen order. Refused
    ones come back as given, encoded. An empty path, or one realpath can't take, is refused.
    """
    root = os.path.realpath(os.fsencode(settings.music_root))
    inside: list[bytes] = []
    refused: list[bytes] = []
    for path in paths:
        given = _encoded(path)
        try:
            real = os.path.realpath(given) if given else b""
        except ValueError:
            real = b""
        if real and _inside(real, root):
            if real not in inside:
                inside.append(real)
        else:
            refused.append(given)
    return inside, refused


def _encoded(path: str | bytes) -> bytes:
    """Return a path as bytes, keeping text that the filesystem encoding can't take."""
    if isinstance(path, bytes):
        return path
    try:
        return os.fsencode(path)
    except UnicodeEncodeError:
        return path.encode("utf-8", "surrogatepass")


def warn_unknown_env(environ: Mapping[str, str]) -> None:
    """Print a line for each PLYRIX_* name that is not a setting, so a typo can't go unnoticed.

    pydantic-settings silently ignores these, and reads variable names in any case. Names only,
    since a value may be private.
    """
    for name in sorted(environ):
        upper = name.upper()
        if not upper.startswith(_ENV_PREFIX) or upper in _TOOLING_ENV:
            continue
        if upper.removeprefix(_ENV_PREFIX).lower() not in Settings.model_fields:
            print(f"plyrix: ignoring unknown variable {_printable(name)}", file=sys.stderr)


def _secret_dirs() -> list[str]:
    """Return the secret directories this user can list: Docker's, then systemd's credentials.

    One that can't be listed, such as a host's /run/secrets at mode 0711, holds nothing
    pydantic-settings can find, and reading it would stop every command.
    """
    candidates = (_DOCKER_SECRETS_DIR, os.environ.get(_CREDENTIALS_VARIABLE))
    return [
        path
        for path in candidates
        if path and os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)
    ]


def _expanded(path: Path, field: str) -> Path:
    """Return a path with a leading ~ expanded, refusing one whose home folder can't be found."""
    try:
        return path.expanduser()
    except RuntimeError:
        raise ConfigError(
            f"plyrix: {_setting(field)} starts with a ~ whose home folder can't be found"
        ) from None


def _anchored(path: Path, field: str) -> Path:
    """Return a path from a file or a variable with ~ expanded, refusing one that is relative."""
    expanded = _expanded(path, field)
    if not expanded.is_absolute():
        raise ConfigError(f"plyrix: {_setting(field)} must be an absolute path")
    return expanded


def _resolved_report(report: Path, from_flag: bool, root: bytes, state_db: Path) -> Path:
    """Return an absolute report path, as the guard trips on any relative write open.

    It follows state_db's rules, and it can't take the place of the ledger.
    """
    report = _expanded(report, "report_file")
    if not from_flag:
        report = _anchored(report, "report_file")
    real = os.path.realpath(os.fsencode(report))
    if _inside(real, root):
        raise ConfigError(f"plyrix: {_setting('report_file')} must be outside the library")
    if os.path.realpath(os.fsencode(state_db)) == real:
        raise ConfigError(f"plyrix: {_setting('report_file')} can't be the state database")
    return Path(os.path.abspath(report))


def _resolved_spool(spool: Path | None, from_flag: bool, root: bytes, state_db: Path) -> Path:
    """Return an absolute spool folder, beside the state database unless one is given.

    It follows state_db's rules: outside the library, and absolute when it comes from a file or a
    variable. Lidarr can write the spool, so it can't be the state database's folder, or any
    folder above it, as that would hand Lidarr the ledger.
    """
    if spool is None:
        spool = state_db.parent / "lidarr-spool"
    else:
        spool = _expanded(spool, "lidarr_spool")
        if not from_flag:
            spool = _anchored(spool, "lidarr_spool")
    real = os.path.realpath(os.fsencode(spool))
    if _inside(real, root):
        raise ConfigError(f"plyrix: {_setting('lidarr_spool')} must be outside the library")
    if _inside(os.path.realpath(os.fsencode(state_db)), real):
        raise ConfigError(
            f"plyrix: {_setting('lidarr_spool')} can't be the state database or a folder holding it"
        )
    return Path(os.path.abspath(spool))


def _inside(path: bytes, root: bytes) -> bool:
    return path == root or path.startswith(root.rstrip(b"/") + b"/")


def _printable(name: str) -> str:
    """Return a name from outside plyrix on one line, with no raw control or undecodable byte."""
    return ascii(name)[1:-1]


def _setting(field: str) -> str:
    return f"{field} ({_ENV_PREFIX}{field.upper()})"


def _describe_validation(error: ValidationError) -> str:
    missing: list[str] = []
    invalid: list[str] = []
    unknown: list[str] = []
    for problem in error.errors(include_input=False):
        field = str(problem["loc"][0])
        if problem["type"] == "missing":
            missing.append(_setting(field))
        elif problem["type"] == "extra_forbidden":
            unknown.append(_printable(field))
        elif _setting(field) not in invalid:
            invalid.append(_setting(field))
    parts = []
    if missing:
        parts.append("missing required settings: " + ", ".join(missing))
    if invalid:
        parts.append("invalid settings: " + ", ".join(invalid))
    if _setting("excludes") in invalid:
        parts.append("each exclude is a pattern relative to the music root")
    if _setting("lidarr_path_map") in invalid:
        parts.append(
            "each lidarr_path_map entry is FROM=TO, both absolute, with TO inside music_root"
        )
    if _setting("lrclib_url") in invalid:
        parts.append(
            f"lrclib_url must be the URL of a self-hosted LRCLIB, not {_PUBLIC_LRCLIB_HOST}"
        )
    if _setting("plex_url") in invalid:
        parts.append(f"plex_url must be your own Plex Media Server, not {_PLEX_ACCOUNT_HOST}")
    if unknown:
        parts.append("unknown settings in the config file: " + ", ".join(unknown))
    return "plyrix: " + "; ".join(parts)


def _describe_source_error(error: SettingsError) -> str:
    """Name the field from the source's message, which is otherwise not printed."""
    match = _SOURCE_ERROR_FIELD.search(str(error))
    name = _setting(match.group(1)) if match else "a setting"
    return f"plyrix: invalid settings: {name} could not be parsed; a list must be a JSON list"
