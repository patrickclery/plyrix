# plyrix

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/lockup-dark.svg">
  <img src="assets/brand/lockup.svg" alt="plyrix">
</picture>

plyrix: time-synced lyrics for your Plex music library, from your own LRCLIB database, without touching an audio file.

It walks a library and writes a `.lrc` beside each track that has synced lyrics and a `.txt` for plain-only ones, and a `.txt` it didn't write is never replaced. plyrix ships no lyrics and no data: you download LRCLIB's database dump yourself, from https://lrclib.net/db-dumps, and every lookup stays on your machine. Tested on ZFS on Linux only.

Try it on a copy of your library first, for example a copy at `/srv/music`.

## Audio safety

The code that reads audio cannot write, and the only writer creates `.lrc` and `.txt` files.

A `.lrc` plyrix didn't write, or that it can't show is unchanged, is never replaced.

## Install

### Disk space

plyrix reads lyrics from a local LRCLIB server loaded with LRCLIB's database dump, and the dump is large. Check the space before any step:

| What | Size |
|---|---|
| The dump download | 49.3 GB, compressed |
| A first load, at its peak | 319.8 GB apparent, as the download is kept until LRCLIB is ready and LRCLIB builds its search index on first start. On ZFS with lz4 compression and `recordsize=128K` that peak took 129.6 GB on disk. |
| Loaded, without the kept download | About 80 GB on disk, on the same ZFS pool |
| Docker build space, for building both images | Measured 2026-11-11: the plyrix image is 246 MB and the LRCLIB image 44 MB as `docker images` reports them, or 89.1 MB and 12.5 MB as Docker 29's content sizes. Building LRCLIB's Rust image also fills Docker's build cache. All of it is in Docker's own storage. |

Measured 2026-11-11 on one clean install from this README, with the LRCLIB dump of 2026-10-30, tested on ZFS on Linux only. LRCLIB's `/api/search` answers only once its search index is ready, while `/api/get` answers at once.

### LRCLIB

plyrix needs a local [LRCLIB](https://github.com/tranxuanthang/lrclib) loaded with the official dump, and never bundles or mirrors the dump. The lyrics come from LRCLIB and its contributors.

The steps below run from a checkout of this repo: `git clone https://github.com/patrickclery/plyrix.git`, then `cd plyrix`.

1. Pick a folder for LRCLIB's data on a disk with room, and make it as the user that will own it, for example `mkdir /srv/lrclib`.
2. Build LRCLIB at the pinned upstream commit `e1d863afc430392c8b2f97bea72139292d498432`. This builds from source and pulls no LRCLIB image.
3. Open https://lrclib.net/db-dumps in a browser and copy the newest file name. The page needs JavaScript, so the script can't look it up. Without a browser, the JSON listing that page reads holds the same names; `scripts/lrclib-update.sh` names it as its default `LRCLIB_DUMP_LISTING`. Its host may change, so the page stays the reference.
4. Run `scripts/lrclib-setup.sh` with that name. It checks the folder and its free space with `plyrix doctor --lrclib-data` first, then downloads, checks and loads the dump, starts LRCLIB and waits until its search index is ready, which can take hours.
5. When plyrix is configured, setup records the loaded dump in plyrix's state, and runs use it with no setting. Otherwise run `plyrix lrclib-record NAME --status live` once plyrix is configured, and until then set `dump_id` to that name, with `PLYRIX_DUMP_ID` or in the config file. A dump recorded live wins over a `dump_id` set with `PLYRIX_DUMP_ID` or in the config file, so an update's new dump is used even with `dump_id` still set, and the run warns once that it ignored it. Only `--dump-id` on the command line overrides the live record, for one run. To keep the dump current from then on, see Keeping LRCLIB current.

```sh
export LRCLIB_DATA=/srv/lrclib
MUSIC_ROOT=/nonexistent STATE_DIR=/nonexistent docker compose build lrclib
PLYRIX="uv run plyrix" scripts/lrclib-setup.sh lrclib-db-dump-20260101T000000Z.sqlite3.gz
```

Run these from the checkout. Compose checks every service's variables for any command, and only plyrix's own container uses `MUSIC_ROOT` and `STATE_DIR`, so placeholders do for building LRCLIB. The script supplies the same placeholders when it builds, stops and starts LRCLIB, and checks that compose can read the file before it downloads anything. It runs `docker compose build lrclib` before the download, which is quick once the image exists, and starts LRCLIB after the swap with `--no-build`, so that start can't fail on reaching GitHub with the new database in place. If LRCLIB doesn't start, the script says so and prints the command to start it. LRCLIB then answers at `http://localhost:3300/api` on this host only. Only the first start builds the search index; after a stop and restart LRCLIB reuses it, and search answered again within a minute on the clean install. A download that breaks part way, on a dropped connection, a stream error, a timeout or a server error, is retried with a growing wait and resumes from the bytes already on disk; by default the script gives up after 10 attempts in a row that add nothing (`LRCLIB_DOWNLOAD_RETRIES`, with `LRCLIB_RETRY_SECONDS` for the first wait). A partial download is never deleted: one that can't be finished is kept, and running the script again resumes it. If the server answers a fresh range request with the whole file, it can't resume, so the script stops, keeps the partial and prints the command to delete it and start from zero. A finished download must match the server's size and pass `gzip -t`; one that is full size but corrupt is deleted, and nothing that fails a check ever replaces a working database. The script keeps the download and prints how to delete it.

| Variable | Meaning | Default |
|---|---|---|
| `LRCLIB_DATA` | LRCLIB's data folder, mounted at `/data` | required |
| `LRCLIB_USER` | `uid:gid` LRCLIB runs as, which should own `LRCLIB_DATA` | `1000:1000` |
| `LRCLIB_PORT` | The loopback port LRCLIB is published on | `3300` |
| `PLYRIX` | The command the script runs doctor with | `plyrix` |
| `LRCLIB_READY_TIMEOUT` | Seconds the script waits for the search index | `43200` |
| `LRCLIB_POLL_SECONDS` | Seconds between its checks | `30` |
| `LRCLIB_DUMP_BASE` | Where the dump is fetched from, for tests | `https://db-dumps.lrclib.net` |

### Package

Install the released package from PyPI:

```sh
uv tool install plyrix
```

`pipx install plyrix` works as well. Neither installs uv or pipx; see uv's install docs at https://docs.astral.sh/uv/getting-started/installation/. The clean-machine test covered the container path only, not this one. To build the package from a checkout instead, and install the file:

```sh
uv build
uv tool install dist/plyrix-0.1.0-py3-none-any.whl
```

`pipx install dist/plyrix-0.1.0-py3-none-any.whl` works as well. LRCLIB still runs separately, as above.

To run it on a schedule, copy the two example units from `examples/systemd/` to `~/.config/systemd/user/`, and enable the timer:

```sh
cp examples/systemd/plyrix.service examples/systemd/plyrix.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now plyrix.timer
```

The service runs `plyrix run` at idle IO priority, then `plyrix plex-refresh` after every run, successful or not. The timer runs it weekly, Mondays at 03:00, and catches up at the next boot after a missed run. Settings come from plyrix's config file or `PLYRIX_` variables. For the Plex token, a system unit can pass a file with `LoadCredential=plyrix_plex_token:PATH`, which plyrix reads from systemd's credentials directory. Otherwise the token comes from `PLYRIX_PLEX_TOKEN` or the config file, kept private with `chmod 600`.

### Container

Install Docker with Docker's own steps, https://docs.docker.com/engine/install/, and run these commands as a user in the `docker` group, or with rootless Docker.

The released plyrix image is `ghcr.io/patrickclery/plyrix:0.1.0`, for `linux/amd64` and `linux/arm64`. To run it instead of building one, add the release override to each compose command below: `docker compose -f compose.yaml -f examples/compose.release.yaml`. Without it, compose builds the plyrix image locally from this checkout. LRCLIB's image is always built locally from the pinned commit and published nowhere. Building needs room in Docker's own storage, see Disk space. `compose.yaml` runs plyrix beside the pinned LRCLIB, and reads these variables:

| Variable | Meaning | Default |
|---|---|---|
| `MUSIC_ROOT` | The library's real absolute path, with no symlink in it. plyrix records real paths, so the container sees the library at the same path as the host. | required |
| `STATE_DIR` | plyrix's state folder. Make it first, owned by the container's user. | required |
| `LRCLIB_DATA` | LRCLIB's data folder, as above. plyrix sees it read-only, so `doctor` can check its disk. | required |
| `CONTAINER_USER` | `uid:gid` plyrix runs as. Use a `STATE_DIR` that uid owns. | `1000:1000` |
| `CRON_SCHEDULE` | When cron mode runs, as a cron expression | `@weekly` |
| `PLEX_TOKEN_FILE` | The file holding the Plex token | `./secrets/plyrix_plex_token` |

The library must already exist at `MUSIC_ROOT`, writable by uid 1000, or by the uid in `CONTAINER_USER`, as plyrix writes sidecars beside the tracks; the commands below make the state, config and secrets folders, not the library. Put plyrix's settings, such as the Plex URL and section, in `config/plyrix/config.toml` in the checkout. Put the Plex token in `secrets/plyrix_plex_token`, or point `PLEX_TOKEN_FILE` at another file. Leave the file empty when not using Plex, as an empty token counts as unset. Compose mounts that file as it is on the host, so it must be readable by uid 1000, or by the uid in `CONTAINER_USER`, for example owned by that uid with mode `0400`. The library is mounted writable, because sidecars sit beside the tracks. The code that reads audio cannot write, and the only writer creates `.lrc` and `.txt` files; the write tripwire guards the mount.

```sh
export MUSIC_ROOT=/srv/music STATE_DIR=/srv/plyrix-state LRCLIB_DATA=/srv/lrclib
mkdir -p "$STATE_DIR" config/plyrix secrets
touch secrets/plyrix_plex_token && chmod 0400 secrets/plyrix_plex_token
docker compose build
PLYRIX="docker compose -f $PWD/compose.yaml run --rm --no-deps plyrix" \
  scripts/lrclib-setup.sh lrclib-db-dump-20260101T000000Z.sqlite3.gz
docker compose run --rm plyrix doctor
docker compose run --rm plyrix run
docker compose up -d
```

The setup script runs doctor through compose with `--no-deps`, so doctor neither starts LRCLIB nor waits for it to be healthy, which can take hours while LRCLIB builds its search index. Started on an empty folder, LRCLIB makes an empty database and reports healthy, and doctor then reports that database as empty and exits 1. `$PWD/compose.yaml` stands for this checkout's absolute path, so the command works from any folder. `docker compose run --rm plyrix run` runs once. Because both services set `pull_policy: build`, every `up` and `run` rebuilds the images, and LRCLIB's build fetches its pinned source from GitHub; `docker compose up -d --no-build` starts the images already built without that. The rebuild's progress prints before plyrix's own lines on `docker compose run`. `docker compose up -d` starts cron mode, which runs `plyrix run` and then `plyrix plex-refresh` on `CRON_SCHEDULE`, at idle IO priority where the filesystem honours it. In cron mode, the scheduler's "job succeeded" or "job failed" line reflects only `plex-refresh`, the last command, so read the run's counts lines in the container's log, or its `report_file`, for the run's own result. Cron mode logs "process reaping disabled, not pid 1" at start, which is harmless: tini is pid 1 and reaps. Through compose, doctor's `--lrclib-data` check says the folder can't be written from here, because plyrix sees LRCLIB's data read-only; that line is expected. The LRCLIB update script runs on the host, not in this cron, as it stops and starts LRCLIB; see Keeping LRCLIB current.

## Run

plyrix needs a local copy of [LRCLIB](https://github.com/tranxuanthang/lrclib) that it can reach over HTTP. From a checkout, with your copied library at `/srv/music`:

```sh
uv sync
export PLYRIX_LRCLIB_URL=http://localhost:3300/api
uv run plyrix run /srv/music
```

LRCLIB has to hold a fully loaded dump. Before each run, plyrix checks that LRCLIB answers, that its search index is ready and that it holds tracks, and if not it stops with nothing recorded. It can't tell a partly loaded database from a complete one, and every track missing from a partial one would count as a miss.

The run prints counts only. Add `--verbose` to print each track's outcome and path as well, with anything outside printable ASCII escaped, so `Björk` prints as `Bj\xf6rk`. Paths are relative to the music root.

Add `--dry-run` to see what a run would do first. A dry run reads tags and asks LRCLIB as a real run does, changes nothing in the library or the state database, and prints what a real run would count. It stops with exit code 75 if another run is using the state database, and with exit code 2 if a killed run left that database mid-write, in which case do a normal run first.

### Settings

| Setting | Flag or argument | Variable | In the config file | Default |
|---|---|---|---|---|
| LRCLIB URL | `--lrclib-url` | `PLYRIX_LRCLIB_URL` | `lrclib_url` | none, required |
| Music root | `PATH` argument | `PLYRIX_MUSIC_ROOT` | `music_root` | none, required |
| Excludes | `--exclude`, repeatable | `PLYRIX_EXCLUDES`, as a JSON list | `excludes` | none |
| State database | `--state-db` | `PLYRIX_STATE_DB` | `state_db` | `$XDG_STATE_HOME/plyrix/state.db` |
| Loaded dump's filename: the flag overrides the dump recorded live, the variable and file only stand in when none is | `--dump-id` | `PLYRIX_DUMP_ID` | `dump_id` | none |
| Plain-only lyrics | `--plain` | `PLYRIX_PLAIN` | `plain` | `write` |
| Print paths | `--verbose` | `PLYRIX_VERBOSE` | `verbose` | off |
| Dry run | `--dry-run` | `PLYRIX_DRY_RUN` | `dry_run` | off |
| Retry every earlier miss after a dump update | `--full` | `PLYRIX_FULL` | `full` | off |
| Load limit, in tracks per second | `--max-rate` | `PLYRIX_MAX_RATE` | `max_rate` | no limit |
| How sidecars are created | `--sidecar-method` | `PLYRIX_SIDECAR_METHOD` | `sidecar_method` | `auto` |
| JSON report | `--report` | `PLYRIX_REPORT_FILE` | `report_file` | none |
| Plex URL | `--plex-url` | `PLYRIX_PLEX_URL` | `plex_url` | none |
| Plex library section | `--plex-section` | `PLYRIX_PLEX_SECTION` | `plex_section` | none |
| Plex token | none, on purpose | `PLYRIX_PLEX_TOKEN`, or a secret file `plyrix_plex_token` | `plex_token` | none |
| Music root as Plex sees it | `--plex-music-root` | `PLYRIX_PLEX_MUSIC_ROOT` | `plex_music_root` | the music root |
| Pause between album refreshes, in seconds | `--plex-pace` | `PLYRIX_PLEX_PACE` | `plex_pace` | `1` |
| Lidarr path map | none | `PLYRIX_LIDARR_PATH_MAP`, as a JSON list | `lidarr_path_map` | none |
| Lidarr spool folder | none | `PLYRIX_LIDARR_SPOOL` | `lidarr_spool` | `lidarr-spool` beside the state database |
| Lidarr hook time budget, in seconds | none | `PLYRIX_LIDARR_BUDGET` | `lidarr_budget` | `60` |

A flag beats a variable, a variable beats a secret file, and a secret file beats the config file. An unknown `PLYRIX_` variable gets a warning, and a mistake in a setting stops the run with exit code 2.

- **LRCLIB URL:** it has to be your own server. plyrix refuses `lrclib.net` and its subdomains, and never falls back to an online source. This is a check on the name, so it can't see a DNS alias or an IP address for the public server.
- **Run path:** with no music root configured, `PATH` becomes the root. With one configured, `PATH` has to be inside it and limits the run to that folder, so `PLYRIX_MUSIC_ROOT=/srv/music uv run plyrix run /srv/music/Album` runs one album. A `PATH` outside the root is refused. With a music root configured, `--path DIR`, repeatable, limits the run to those folders instead, for example `uv run plyrix run --path /srv/music/A --path /srv/music/B`. It can't be combined with `PATH`, and a folder outside the root is refused.
- **Lidarr path map:** only `plyrix lidarr-hook` uses it. Each entry is `FROM=TO`: a folder as Lidarr sees it, then the same folder as plyrix sees it, for example `PLYRIX_LIDARR_PATH_MAP='["/music=/srv/music"]'`. The longest matching `FROM` wins, matched on whole folder names, so `/music` doesn't match `/music2`. Every `TO` has to be inside the music root.
- **Lidarr spool folder:** where `plyrix lidarr-hook` queues an album it couldn't finish, one file per album, for `plyrix drain`. It has to be outside the library, like the state database, and can't be the state database's folder or a folder above it, as Lidarr can write it.
- **Lidarr hook time budget:** it bounds how long one Lidarr import waits on plyrix. Lidarr runs the hook inside its import and waits for it, so when the budget runs out the rest of the album is queued. It has to be a number above zero.
- **Excludes:** patterns are relative to the music root and case-sensitive, for example `PLYRIX_EXCLUDES='["Podcasts/*"]'`. A pattern that matches a folder leaves out everything in it, and `*` also matches `/`. A pattern that could never match is refused: an empty one, or one with an absolute path or a `.`, `..` or empty segment.
- **State database:** it has to be outside the library, and a path inside it is refused. It belongs on a local fast disk, since a commit there cost about 96 ms on the tested ZFS pool.
- **Dump id:** name the LRCLIB dump you loaded, ideally by its filename, up to 255 characters on one line. A track with no match, plain lyrics only or marked instrumental is then asked about again only when the dump, its tags or plyrix's matching changes. With no id, plyrix can't tell whether the dump changed, so it warns and asks about those tracks on every run.
- **Plain-only lyrics:** `write` puts them in a `.txt`, and `skip` writes nothing and records the track, which comes due again when the setting goes back to `write`. A `.lrc` with no timestamps shows nothing in Plex, so plain text is never written as one.
- **Load limit:** it caps the tracks per second that read tags or ask LRCLIB, for a run that shares a disk or a server, and `0.5` means one track every two seconds. A track with nothing to do costs no wait, so a re-run over a settled library stays fast. The limit covers tracks, not requests, as one track can make several requests to LRCLIB. It has to be above zero, and anything below `0.000001` is refused.
- **How sidecars are created:** `auto` gives the finished file its name with a hard link, so the name never holds anything less than complete contents. `plain` makes no hard link: it holds the name with an empty file, then renames the finished file over it. Neither ever replaces a file already at the name. The known limit of `plain` is that a kill between those two steps leaves an empty `.lrc` or `.txt` until the next run, which removes it and writes the sidecar. A mount with no hard links takes this path anyway under `auto`, so choose `plain` only for a filesystem that misbehaves with them.
- **A `.txt` already there:** one plyrix can't prove it wrote stays as it is, and a synced match still writes the `.lrc` beside it. A plain match counts as `blocked_foreign` instead. A `.txt` plyrix did write is replaced by a `.lrc` once a synced version exists, as long as the file still matches what it wrote. One it only adopted, because its text equals LRCLIB's, stays beside the `.lrc`.
- **Plex URL:** it has to be your own Plex Media Server, for example `http://localhost:32400`, and only `plyrix plex-refresh` uses it. plyrix refuses `plex.tv` and its subdomains by name, and never follows a redirect, so the token can't be carried to another host. A `*.plex.direct` address works, as it names your own server, but routers with DNS-rebinding protection often block `plex.direct` names, so use the server's LAN address then.
- **Plex library section:** the key of your music library in Plex, for example `1`.
- **Plex token:** it has no flag, because command-line arguments are visible in the process list. It comes from `PLYRIX_PLEX_TOKEN`, from a file named `plyrix_plex_token` in `/run/secrets` or in systemd's credentials directory, or from the config file. An empty value counts as unset.
- **Music root as Plex sees it:** set it when Plex sees the library under another path, such as inside a container. It has to be absolute.
- **JSON report:** see [Reading the counts](#reading-the-counts). It has to be outside the library, and a path inside it is refused.
- **Relative paths:** a path from a variable or the config file can start with `~` but must otherwise be absolute, so the same setting means the same place whichever folder plyrix runs from. Only `PATH`, `--state-db` and `--report` can be relative.
- **Config file:** TOML, read from `$XDG_CONFIG_HOME/plyrix/config.toml`, or from the file you name with `--config`. TOML can't hold a path that isn't valid UTF-8, so give such a path through a variable or as `PATH`.

```toml
music_root = "/srv/music"
lrclib_url = "http://localhost:3300/api"
excludes = ["Podcasts/*"]
```

### Reading the counts

A run prints one `name=value` line per count. Neither the counts nor the report ever hold lyric text.

| Group | Counts | What they say |
|---|---|---|
| Coverage | `synced`, `plain`, `plain_skipped`, `instrumental`, `no_match`, `blocked_foreign`, `untagged` | What each track has. A re-run still reports these. `plain_skipped` is plain lyrics left out by `--plain skip`, and `blocked_foreign` is a file plyrix can't prove it wrote holding the name it needs. |
| Problems | `unavailable`, `unreadable`, `read_failed`, `folder_unreadable`, `fs_no_atomic_publish`, `stem_collision`, `symlink`, `too_long`, `write_failed` | What stopped a track or a folder being looked at. Each walked item adds one to exactly one coverage or problem count. |
| Actions | `written_lrc`, `written_txt`, `upgraded`, `adopted`, `lrc_stale`, `skipped_unchanged`, `looked_up`, `lrc_few_lines`, `lrc_low_coverage` | What this run did. `upgraded` is a `.txt` replaced by a `.lrc`, `adopted` is an existing sidecar plyrix now tracks and never replaces, and `lrc_stale` is a `.lrc` that no longer fits its track. The last two flag a `.lrc` that was written but has few timed lines, or whose timing stops well before the track ends. |
| Reasons | `no_match_not_found`, `no_match_rejected`, `no_match_doubtful_artist`, `no_match_doubtful_title`, `no_match_doubtful_duration`, `no_match_doubtful_version`, `no_match_timing_rejected` | Why a track has no match. These add up to `no_match`. A `doubtful` reason means LRCLIB answered, but that field didn't agree with the track's tags. |

`--report FILE` writes the same counts to a JSON file, for a scheduler to read:

```json
{
  "report_version": 1,
  "plyrix_version": "0.1.0",
  "dry_run": false,
  "dump_id": "",
  "started_at": "2026-11-05T03:00:00+00:00",
  "finished_at": "2026-11-05T04:00:00+00:00",
  "end_reason": "finished",
  "counts": {"synced": 0}
}
```

It holds no paths or tags. `counts` holds every count above, and `end_reason` is `finished` or `lrclib_unavailable`. It is written when a run finishes or stops on LRCLIB, with zero counts if the check before the run stopped it, and a dry run writes one too. A run that couldn't start, such as one with a wrong setting or an empty library, writes none. The file is replaced whole, so a reader never sees part of one.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | The run finished, no write, file read or LRCLIB lookup failed, and every folder could be listed. Tracks with no lyrics, a corrupt file or a sidecar that isn't plyrix's are still counted. |
| 1 | The check before the run found LRCLIB down, still rebuilding its search index or empty, so nothing was recorded. Or the run stopped early, after 5 failed LRCLIB lookups with no successful search between them. |
| 2 | A setting or the state database is wrong, or the library is missing, empty or holds no audio files. |
| 3 | The run finished, but `write_failed`, `fs_no_atomic_publish`, `unavailable`, `read_failed` or `folder_unreadable` is above zero, or the report file couldn't be written. `fs_no_atomic_publish` means the filesystem refused to publish a sidecar atomically, and `read_failed` means permissions or I/O stopped plyrix reading a file. The next run tries those again. |
| 70 | The write tripwire stopped the run. |
| 75 | Another plyrix run is using the state database. |

### Doctor

`plyrix doctor` checks a setup before a run, with the same settings and flags, and changes nothing. It prints one line per check, starting with `ok`, `warning` or `problem`:

- the config loads
- `music_root` exists and this user can write in it
- the state database is on a local disk, not a network or FUSE filesystem, where SQLite's locking can fail, tmpfs, which a reboot empties, or a container's overlay layer, which is lost with the container
- LRCLIB answers, its search index is ready and it holds tracks
- when `plex_url` is set, the server answers, `plex_section` is a music library, and one of its folders matches `plex_music_root`

```sh
uv run plyrix doctor
```

Plex answers many requests without checking the token, so a token can only be proved by a refresh. doctor reports the server as answering, with its token not checked, and the first `plex-refresh` names `plex_token` if Plex refuses it. A server that doesn't answer is only a warning, as Plex is optional for a run. Lines name settings, never their values, so doctor's output never holds `plex_url` or the token.

`plyrix doctor --lrclib-data DIR` checks only LRCLIB's data folder, and needs no settings: that it exists, whether it can be written from here, its filesystem, and whether a first dump load fits. A first load needs about 320 GB, the 319.8 GB peak measured 2026-11-11 on one install, rounded up, with the download kept until LRCLIB is ready; ZFS with lz4 held that peak in 129.6 GB on disk. What is already downloaded in `DIR/dl` of the dump named by `--dump NAME` comes off that need, counting for no more than about 49 GB. Without `--dump`, nothing downloaded counts. The LRCLIB setup script runs this with its dump's name before it downloads anything.

### Doctor exit codes

| Code | Meaning |
|---|---|
| 0 | Every check passed or only warned. |
| 1 | LRCLIB is down, still building its search index or empty, or `--lrclib-data` found too little free space. |
| 2 | A setting is wrong, `music_root` is missing or can't be written, `plex_section` isn't a music library, or the `--lrclib-data` folder is missing. This wins over 1. |

## Keeping LRCLIB current

LRCLIB publishes whole dumps at irregular times, each about 49 GB (the dump of 2026-10-30, measured 2026-11-11, was 49.3 GB), and no diff between them. `scripts/lrclib-update.sh` keeps a local LRCLIB on the newest one. Each run checks the listing LRCLIB's own db-dumps page reads, which is one small request. Only when that names a dump newer than the one LRCLIB serves does it read the first 64 KB of that dump, to check its SQLite version and its exact decompressed size. Only then does it:

1. check the room with `plyrix doctor`;
2. download the dump with resume, as the setup script does;
3. decompress it beside the live database, which LRCLIB keeps serving;
4. verify it read-only with `plyrix lrclib-verify`;
5. record what changed with `plyrix lrclib-diff`, as ids and hashes;
6. swap it in, journaled, wait for LRCLIB, and record the dump live with `plyrix lrclib-record`.

A day with no newer dump costs that one request and downloads nothing.

### Scheduling the update

The schedule belongs to the host, because the script stops and starts LRCLIB with the host user's own compose rights and renames LRCLIB's files. plyrix itself starts and stops nothing, and no container gets Docker's socket. Copy `examples/systemd/plyrix-lrclib-update.service` and `examples/systemd/plyrix-lrclib-update.timer` to `~/.config/systemd/user/`, set the service's Environment lines, and run `systemctl --user enable --now plyrix-lrclib-update.timer`. The timer runs daily. A cron line does the same:

```sh
@daily cd /srv/src/plyrix && LRCLIB_DATA=/srv/lrclib LRCLIB_FOLLOW_UP=1 scripts/lrclib-update.sh
```

With the container, set `PLYRIX="docker compose -f /srv/src/plyrix/compose.yaml run --rm --no-deps plyrix"`, with `MUSIC_ROOT` and `STATE_DIR` set as for compose. `--no-deps` matters, as LRCLIB is down for part of a swap.

"Never touches the music library" holds for the update steps: none of them opens a file in the library. The follow-up below, when it is on, is plyrix's normal scan, under the same write tripwire as any run.

### When something goes wrong

| What | What happens |
|---|---|
| Not enough room for the download, the decompressed database and its search index | doctor stops the update before anything is fetched |
| A dump too new for the pinned LRCLIB build, whose `user_version` is outside 1 to 8 | Refused before the download. Move the LRCLIB pin in `compose.yaml` to load it. |
| A staged database that fails its checks: tables, columns, the index the diff reads, or SQLite's quick_check | Refused, deleted, and LRCLIB is never stopped |
| LRCLIB doesn't stop | Nothing changes |
| LRCLIB doesn't start, or doesn't answer `/api/get` or search in time | Rolled back: the old database, its WAL and its search index are put back, and LRCLIB is started on them |
| The script is killed, or the host reboots, part way | The next run finishes or undoes the swap first, from its journal. A staged database left from before the swap began is removed first, so it doesn't hold the space the next update needs |

A refused or rolled back dump isn't tried again by itself; `LRCLIB_UPDATE_RETRY=1` tries it once more. Its download is kept, so a retry needs no second download. Two updates never overlap, and the setup script takes the same lock.

### Update variables

These are the script's own, read from its environment. The setup script reads the same ones where they apply.

| Variable | Meaning | Default |
|---|---|---|
| `LRCLIB_DATA` | LRCLIB's data folder | required |
| `PLYRIX` | The plyrix command, configured as for a run | `plyrix` |
| `LRCLIB_DUMP_LISTING` | The dump listing | the one LRCLIB's db-dumps page reads |
| `LRCLIB_DUMP_BASE` | Where dumps are fetched from | `https://db-dumps.lrclib.net` |
| `LRCLIB_STOP` | The command that stops LRCLIB | `docker compose stop lrclib` |
| `LRCLIB_START` | The command that starts LRCLIB | `docker compose up -d --no-build lrclib` |
| `LRCLIB_URL` | LRCLIB's API, for the readiness waits | `http://127.0.0.1:3300/api` |
| `LRCLIB_PORT` | The loopback port LRCLIB is published on | `3300` |
| `LRCLIB_USER` | `uid:gid` LRCLIB runs as, passed to compose | `1000:1000` |
| `LRCLIB_DOWNLOAD_RETRIES` | Download attempts in a row that add no bytes before giving up | `10` |
| `LRCLIB_RETRY_SECONDS` | The first wait between download attempts | `5` |
| `LRCLIB_READY_TIMEOUT` | Seconds to wait for the search index after a swap | `43200` |
| `LRCLIB_GET_TIMEOUT` | Seconds to wait for `/api/get` after a swap | `600` |
| `LRCLIB_POLL_SECONDS` | Seconds between readiness polls | `30` |
| `LRCLIB_QUICK_CHECK` | `1` runs SQLite's quick_check on the staged database, which reads all of it; `0` skips it | `1` |
| `LRCLIB_UPDATE_RETRY` | `1` tries a refused or rolled back dump again | `0` |
| `LRCLIB_KEEP_PREVIOUS_DAYS` | Days to keep the previous database after a swap | `0`, deleted once the new dump is live |
| `LRCLIB_KEEP_DOWNLOAD` | `1` keeps the download once the new dump is live | `0` |
| `LRCLIB_STATE_WAIT` | Seconds to wait while another plyrix run holds the state database | `43200` |
| `LRCLIB_FOLLOW_UP` | `1` runs the scan and a capped Plex refresh after a new dump goes live | `0` |
| `LRCLIB_PLEX_LIMIT` | The most album refreshes that follow-up sends | `100` |

### Update exit codes

| Code | Meaning |
|---|---|
| 0 | No newer dump, or the new dump is live. |
| 1 | A check, download, verify, diff or swap failed, or the dump was refused or rolled back. |
| 2 | A setting or argument is wrong, or doctor's exit 2. |
| 3 | The new dump is live, but the follow-up run or Plex refresh didn't finish; the next scheduled run does it. |
| 75 | Another update or plyrix run was busy, so nothing was done, and the next run tries again. |

### After an update

The next `plyrix run` looks up an earlier miss or a `.txt` again only when the diff holds a record by its artist or with its title, when the track was released in or after the old dump's year, or when it was added after the old dump's date. A `.txt` that now finds synced lyrics becomes a `.lrc`. Everything else keeps its result and moves to the new dump without a lookup, since the diff shows its record didn't change, so a row's dump means the dump it is current against, not the one that first answered it. `plyrix run --full` looks up every earlier miss and `.txt` again, whatever the diff says.

The first update after upgrading plyrix looks up every earlier miss, as older ledger rows hold no keys yet. That is expected, and costs what an update cost before.

LRCLIB rebuilds its search index after a swap, which took about 10 minutes as measured on one host in 2026-11. A run in that window stops with nothing recorded, and the next one does the work.

With `LRCLIB_FOLLOW_UP=1`, as the example unit and the cron line above set, the update runs that scan itself right after a new dump goes live. It then runs `plyrix plex-refresh --limit` with `LRCLIB_PLEX_LIMIT`, even when the scan didn't finish every track, paced by `plex_pace`, and the albums over the cap wait for the next refresh. With it off, the next scheduled run and refresh do the same work; add `--limit N` to that refresh to cap a large burst.

### Integrity

The dump has no published checksum or signature. The gzip check, SQLite's quick_check, the schema and index checks and the version guard are what the script relies on instead, over TLS.

On 2026-11-15 the update, the rollback and the refusal were run end to end against the pinned LRCLIB build, with an invented database, by the opt-in gate in `tests/gates/test_lrclib_update_gate.py`.

## Plex

A Plex library scan doesn't attach new sidecars, but an album metadata refresh does. `plyrix plex-refresh` asks your local Plex Media Server to refresh each album that gained or changed a sidecar, one album per request, waiting `plex_pace` seconds after each.

```sh
export PLYRIX_PLEX_URL=http://localhost:32400 PLYRIX_PLEX_SECTION=1
export PLYRIX_PLEX_TOKEN=...   # or a secret file, see Settings
uv run plyrix plex-refresh --dry-run
uv run plyrix plex-refresh --limit 50
uv run plyrix plex-refresh
```

Tested on PMS 1.43.4.10903, 2026-11-10 and 2026-11-11:

- Synced lyrics from a local `.lrc` scrolled in Plexamp, with the first line shown.
- A library scan does not attach a new sidecar; an album metadata refresh does, within about 30 s.
- plyrix's hidden temp files are ignored.
- An empty `.lrc` attaches with no timing; replaced in place, it keeps its stream and serves the timed text after a refresh.
- An `.lrc` without timestamps attaches but isn't timed, which is why plain lyrics go to `.txt`.
- With both `.lrc` and `.txt`, Plex lists both, the `.lrc` timed. Deleting a sidecar and refreshing removes its stream.

`plyrix run` never contacts Plex. The refresh is a separate step, which a scheduler runs after each run. While one of them holds the state database, the other waits its turn by stopping with exit code 75. With no `plex_url` set, `plex-refresh` says so and exits 0, so the step can stay in a schedule.

Every sidecar a run writes or adopts is queued for a refresh, and stays queued until Plex accepts the refresh of its own track's album, or of every album in its folder if Plex lists no track of that name there. A run or a refresh that is interrupted loses nothing: what is still queued is refreshed next time. A folder Plex doesn't list yet, perhaps because Plex hasn't scanned it, stays queued and counts as `not_in_plex`. A large `not_in_plex` usually means `plex_music_root` doesn't match the path Plex sees for the library. plyrix compares its paths with Plex's as POSIX paths, byte for byte.

Before a first refresh of a large backlog, run it with `--dry-run`, which sends nothing and changes nothing, and prints how many albums it would refresh. Then use `--limit N` to send at most N album refreshes per drain. A sidecar whose albums don't fit in what is left of the limit stays queued for the next drain, and the drain moves on to the next one, so each drain makes progress.

| Count | What it says |
|---|---|
| `queued_sidecars`, `queued_folders` | Sidecars waiting for a refresh, and the folders they are in. |
| `albums_to_refresh` | The albums this drain sends, or a dry run would send, within `--limit`. |
| `refreshed`, `refresh_failed` | Album refreshes Plex accepted, and ones it refused or failed. |
| `cleared_folders`, `cleared_sidecars` | Folders with no sidecar still queued, and sidecars no longer queued, because Plex accepted the albums they need. |
| `not_in_plex` | Queued folders Plex doesn't list. They stay queued. |
| `outside_root` | Queued folders outside the music root, which plyrix can't map to Plex's path. They stay queued. |
| `left_by_limit` | Queued folders with a sidecar left for the next drain by `--limit`, or by the Lidarr hook's time budget. |

`--report FILE` writes these counts to a JSON file, with `command` set to `plex-refresh` and `end_reason` set to `finished` or `plex_unavailable`. Only the flag writes one, so the `report_file` setting stays the run's. Like the run's report, it holds no paths and no titles, and it never holds `plex_url` or the token.

### Plex refresh exit codes

| Code | Meaning |
|---|---|
| 0 | Every album sent was refreshed, or nothing was queued. Folders Plex doesn't list don't change this. |
| 1 | Plex didn't answer, or refused the token, before anything was sent, so nothing was refreshed. |
| 2 | A setting is wrong, or `plex_section` isn't a music library on that server. |
| 3 | Plex refused or failed at least one album refresh. Those folders stay queued, and a refused one names `plex_token`. The drain stops at the first refusal, or after three failures in a row, and leaves the rest queued. |
| 75 | Another plyrix run is using the state database. |

### Checking that lyrics attached

Plex accepting a refresh doesn't prove it attached the sidecar. `plyrix plex-check` asks Plex what it attached for a sample of sidecars, 10 by default, picked at random from those whose album refresh Plex already accepted. `--sample N` checks N instead. A PATH checks one track, one sidecar, or every published sidecar in an album folder, whether or not its refresh is still queued.

```sh
uv run plyrix plex-check
uv run plyrix plex-check --sample 50 --verbose
uv run plyrix plex-check "$PLYRIX_MUSIC_ROOT/Artist/Album"
```

Each sidecar gets one verdict:

| Verdict | What Plex shows | Passes for |
|---|---|---|
| `local_timed` | A lyric stream from the sidecar, which Plex says is timed. | `.lrc` |
| `local_untimed` | A lyric stream from the sidecar, which Plex says isn't timed, or doesn't say. | `.txt` |
| `provider` | Lyrics from an online provider only, not from the sidecar. | neither |
| `none` | No lyrics, or no track for the sidecar. | neither |

A `.lrc` passes only when Plex lists a lyric stream from the sidecar itself and says that stream is timed. Lyrics from an online provider don't count, even timed ones. A `.txt` passes as `local_untimed`.

Plex refreshes an album in the background after it accepts the request, so a check straight after a drain can fail and pass a few minutes later. plyrix never queues a sidecar again after a failed check, and a check changes nothing in the state database. To learn whether a stream is timed, the check reads Plex's lyric stream, which holds the lyrics, and keeps nothing from it but that one flag. No lyrics or titles appear in its output or its report.

| Count | What it says |
|---|---|
| `checked`, `passed`, `failed` | Sidecars checked, and whether each got the verdict its kind should. |
| `local_timed`, `local_untimed`, `provider`, `none` | Sidecars with each verdict. |
| `not_in_plex` | Sidecars counted as `none` because Plex doesn't list their track. |
| `unreadable` | Sidecars counted as `local_untimed` because Plex's stream body didn't say whether it is timed, or the stream was gone. A stream request Plex fails or refuses stops the check with exit code 1 instead. |

`--verbose` also prints each sidecar's path, relative to the music root, and its verdict. `--report FILE` writes the counts as `plex-refresh` does, with `command` set to `plex-check`. Unlike `plex-refresh`, `plex-check` stops with exit code 2 when `plex_url` isn't set, as the check was asked for.

### Plex check exit codes

| Code | Meaning |
|---|---|
| 0 | Every sidecar checked passed, or none has had its album refreshed yet. |
| 1 | Plex didn't answer, or refused the token, so the check is incomplete. |
| 2 | A setting is wrong, `plex_section` isn't a music library, or no sidecar is published at the PATH given. |
| 3 | At least one sidecar didn't get the verdict its kind should. Nothing is queued again. |
| 75 | Another plyrix run is using the state database. |

## Lidarr

plyrix can run on each album Lidarr imports, so new music gets lyrics without waiting for the scheduled run. `plyrix lidarr-hook` is made for Lidarr's Custom Script connection: it runs plyrix on just the folders of the album Lidarr imported, and then refreshes that album in Plex when Plex is configured.

### Why after import

Run plyrix from Lidarr after it imports an album, not from a download client such as qBittorrent when a download completes. Lidarr renames and moves files on import, so a sidecar written in the download folder would be left behind or misnamed. Lidarr runs the connection after the files are in their final library folders, and passes their paths to the script.

### Connecting Lidarr

1. In Lidarr, open Settings, Connect, and add a Custom Script.
2. Tick both On Release Import and On Upgrade. On Upgrade alone does nothing, as Lidarr only reaches it from inside Release Import.
3. Set Path to the shim for your route, below. Lidarr passes the script no arguments.
4. Press Test. A Test event does no work: it checks that the settings load, that `music_root` is a folder and that the spool can be written, and Lidarr shows an error when one of them fails.

Lidarr describes the album in `Lidarr_EventType`, `Lidarr_AddedTrackPaths`, `Lidarr_DeletedPaths`, `Lidarr_Artist_Path` and `Lidarr_Album_Id`, as its source spells them. Lidarr 3.1.0.4875 on Linux sets them in lowercase, such as `lidarr_eventtype`, as a real Lidarr showed on 2026-11-15, so plyrix and the shims read either spelling. An upgrade is an import with `Lidarr_DeletedPaths` set. Every other event, Rename included for now, is ignored and left to the next scheduled run. plyrix runs the folders that hold the added tracks, every disc of a multi-disc album in one run, or the artist folder when those folders can't be found. A folder outside `music_root` is refused. When none of them exists where plyrix sees the library, which is how a wrong `lidarr_path_map` looks, the hook says so on stderr and queues the album. A drain that runs before the map is fixed can't find the album either, and moves its file to `failed/` in the spool, so fix the map, then move the file back as shown under [Never holding up Lidarr](#never-holding-up-lidarr).

Lidarr runs the script inside its import and waits for it, so the hook keeps to a time budget, described below.

### Routes

| Route | Shim | Notes |
|---|---|---|
| Lidarr and plyrix in separate containers (recommended) | `examples/lidarr/lidarr-spool.sh` | `examples/lidarr/compose.lidarr.yaml`, a spool folder both containers mount, `DRAIN_SCHEDULE`, and `PLYRIX_LIDARR_PATH_MAP` for Lidarr's `/music` |
| Lidarr on the host | `examples/lidarr/plyrix-hook.sh`, or `examples/lidarr/plyrix-hook-compose.sh` | The first runs plyrix installed on the host, the second plyrix's compose service |
| plyrix inside Lidarr's container | `examples/lidarr/plyrix-hook.sh` | Needs Python 3.12 or later in the Lidarr image. Not tested end to end |

Don't mount the Docker socket into Lidarr's container to reach plyrix from there: that hands Lidarr root on the host.

**Separate containers.** The linuxserver Lidarr image has a shell, but no Python and no docker CLI. So the shim writes each import as one file in a folder both containers mount, and plyrix's container runs `plyrix drain` every few minutes.

```sh
mkdir -p /srv/plyrix-spool
export LIDARR_SPOOL=/srv/plyrix-spool LIDARR_MUSIC=/music
docker compose -f compose.yaml -f examples/lidarr/compose.lidarr.yaml up -d
```

Mount the same folder into Lidarr's container at `/plyrix-spool`, copy `lidarr-spool.sh` into Lidarr's config folder, and set the connection's Path to it, for example `/config/lidarr-spool.sh`. Lidarr's user, its `PUID`, writes the folder, and plyrix's `CONTAINER_USER` reads and removes files in it, so run both as one uid. Don't share plyrix's `STATE_DIR` with Lidarr. The override maps `LIDARR_MUSIC` onto `MUSIC_ROOT` in `PLYRIX_LIDARR_PATH_MAP`, and `DRAIN_SCHEDULE` defaults to every 5 minutes, so an import waits at most that long.

**Lidarr on the host.** With plyrix installed on the host, for example with `uv tool install plyrix`, set Path to `plyrix-hook.sh`, which runs `plyrix lidarr-hook` with the settings in Lidarr's environment or plyrix's config file. To use plyrix's compose service instead, set Path to `plyrix-hook-compose.sh`, and set `COMPOSE_FILE` to plyrix's `compose.yaml` in Lidarr's environment. It runs `docker compose run --pull never --rm --no-deps -T`, passing the five Lidarr names through:

- `--pull never` stops compose rebuilding `plyrix:local` on every import, as the service builds its own image.
- `--no-deps` doesn't wait for LRCLIB's long health check start period.
- `-T`, as Lidarr gives the script no terminal.

If plyrix can't run at all, because it isn't on Lidarr's `PATH`, Docker is stopped, `COMPOSE_FILE` is unset or the image isn't built, either shim queues the import in `PLYRIX_LIDARR_SPOOL`, as `lidarr-spool.sh` does, and still exits 0. So set `PLYRIX_LIDARR_SPOOL` in Lidarr's environment to plyrix's spool folder, as the host sees it, which is `lidarr-spool` in `STATE_DIR` for the compose service, and let Lidarr's user write it. The compose shim doesn't pass it into the container. Without it, the shim says so on stderr, and the album waits for the next scheduled run. A Test still exits 1, so Lidarr shows the broken setup.

Where Lidarr sees the library at another path than plyrix does, map it, for example `PLYRIX_LIDARR_PATH_MAP='["/music=/srv/music"]'`. Schedule `plyrix drain` as well, so an album the hook queued doesn't wait for the weekly run. With plyrix on the host, add it to cron:

```sh
*/5 * * * * plyrix drain
```

With plyrix's compose service, there is no `plyrix` on the host, so set `DRAIN_SCHEDULE` where `docker compose up -d` runs, for example `DRAIN_SCHEDULE='*/5 * * * *'`, and the cron-mode container then drains the queue the hook fills in `STATE_DIR`.

**plyrix inside Lidarr's container.** The linuxserver Lidarr image has no Python, so this needs Python 3.12 or later added to it. LRCLIB has to be reachable from Lidarr's network too: the shipped compose file publishes LRCLIB on the host's loopback only, so put Lidarr on plyrix's compose network and set `PLYRIX_LRCLIB_URL=http://lrclib:3300/api`. This route was not tested end to end.

### Never holding up Lidarr

The hook exits 0 for every event but a Test with a broken setup, and reports problems in Lidarr's log, which records a script's stderr as errors and its stdout at debug level. It works on the album, then on albums queued earlier, for up to `lidarr_budget` seconds, 60 by default. It checks the budget between tracks, between queued albums, and before each Plex listing and refresh, but never stops a step part way, so the step under way when the budget runs out adds its own time: one track's LRCLIB lookups, LRCLIB's readiness check, or a Plex listing of the whole music section, page by page. Each request waits at most 30 seconds, so a server that stops answering can hold the import about that long past the budget, and a slow Plex listing of a large section longer.

When another plyrix run holds the state database, the state database can't be used, LRCLIB is unavailable, the budget runs out, or the hook stops on an unexpected error before the album has run, the album is queued as one file in `lidarr_spool`, and Lidarr waits on nothing more. A queued file holds Lidarr's own paths, so a later drain applies the path map as it is then. Queued albums are run by:

- the next hook, after its own album, within what is left of its budget;
- `plyrix drain`, which removes a file only once its album's run has finished;
- a whole-library `plyrix run` that finishes, which removes the files queued before it started, as it covered them.

A file that can't be run, because it is corrupt or unreadable, isn't an import, or names nothing inside `music_root`, is moved to `failed/` in the spool, so it can't block the queue. Fix the cause, such as `lidarr_path_map`, then move the files back into the spool as plyrix's user, and the next drain runs them:

```sh
mv /srv/plyrix-state/lidarr-spool/failed/*.event /srv/plyrix-state/lidarr-spool/
```

The folder is `lidarr_spool`, which is `lidarr-spool` beside the state database by default, so `lidarr-spool` in `STATE_DIR` for the compose service. A file the drain still can't run goes back to `failed/`. A drain that finds `music_root` missing or empty, as it is while the library is unmounted, runs nothing and moves nothing, so the queue waits for the remount.

| Count | What it says |
|---|---|
| `queued` | Albums in the spool when the drain started. |
| `drained` | Albums run and removed from the spool. |
| `left` | Albums still queued for the next drain. |
| `quarantined` | Files moved to `failed/`. |
| `failed_tracks` | Tracks in drained albums whose write, file read or LRCLIB lookup failed. The next run tries them again. |

### Lidarr hook exit codes

| Code | Meaning |
|---|---|
| 0 | A Test whose setup works, and every other event whatever happened: an album that was run, queued or refused, a wrong setting, or a tripwire stop. Problems go to stderr, which Lidarr logs as errors. |
| 1 | A Test event found that the settings don't load, `music_root` isn't a folder, or `lidarr_spool` can't be written. |
| 2 | `Lidarr_EventType` isn't set, so Lidarr didn't run it. |

### Drain exit codes

| Code | Meaning |
|---|---|
| 0 | Every queued album was run, nothing was queued, or another plyrix run holds the state database, so the albums wait for the next drain. |
| 1 | LRCLIB is unavailable. The albums stay queued. |
| 2 | A setting or the state database is wrong, a spool file can't be removed or moved, `failed/` in the spool is a symlink, the spool can't be listed, or `music_root` is missing or empty, as an unmounted library is. The albums stay queued. |
| 3 | A file was moved to `failed/`, or tracks in a drained album failed. |
| 4 | The drain stopped on an unexpected error. The album it was running and those after it stay queued, so the same error can stop the next drain too; the stderr line names the error. |
| 70 | The write tripwire stopped the drain. |

### Plex and the hook

With Plex configured, the hook and `plyrix drain` refresh only the albums in the folders they just ran, paced by `plex_pace` and within the budget, and print the refresh counts. An album Plex hasn't scanned yet counts as `not_in_plex` and stays queued for the next `plyrix plex-refresh`, as does one the budget left. Without Plex settings, the new sidecars wait in the refresh queue as every sidecar does.

### What Lidarr does with plyrix's sidecars

Tested on Lidarr 3.1.0.4875, the linuxserver image, in a scratch instance on 2026-11-10, with sidecars written after Lidarr imported the album, which is how plyrix writes them:

- Lidarr tracked none of plyrix's `.lrc` or `.txt` files, under the library's naming or Lidarr's default naming.
- A Lidarr rename left every sidecar behind at its old name, in the old folder. plyrix then writes fresh ones beside the renamed tracks, and leaves the old ones where they are.
- The arr-scripts `LyricExtractor` script (v1.5, commit `c959f71`), part of Lidarr Extended, deletes every `.lrc` in the album folder when it runs, and keeps `.txt` files. Disable it, or accept that plyrix writes the `.lrc` again on the next hook, drain or run.
- Sidecars arriving inside a download were not tested.

Tested end to end on Lidarr 3.1.0.4875 on 2026-11-15, in a throwaway Lidarr with a copied album: Lidarr's Test passed with a writable spool and failed with an unwritable one, a real import fired `lidarr-spool.sh` once, `plyrix drain` then wrote the album's sidecars and left its audio byte-identical, and with the state database held the hook queued the album and a later drain ran it. That import went through Lidarr's manual import, as its automatic folder scan didn't identify the copy within a minute.

Not tested, from Lidarr's source: Lidarr's own Import Extra Files setting handles only lyric files that arrive with a download, so it doesn't pick up plyrix's later sidecars. A `.lrc` that does arrive with a download is one plyrix didn't write, so plyrix leaves it in place, as for any sidecar that isn't its own. An upgrade whose new file has a different name leaves the old name's sidecar behind.

## Related projects

- [LRCLIB](https://github.com/tranxuanthang/lrclib) is the lyrics server and database plyrix reads, and the source of the dump.
- [lrcget](https://github.com/tranxuanthang/lrcget) is a desktop app that fetches synced lyrics from LRCLIB, for anyone who would rather use a GUI.

## Credits

The lyrics come from LRCLIB and its contributors. [LRCLIB](https://github.com/tranxuanthang/lrclib) is MIT licensed; plyrix builds it from source at a pinned commit and doesn't link it. plyrix never bundles or mirrors the dump.

## Lyrics and legal

plyrix ships no lyrics: not in this repo, its packages, its images or its site. It writes sidecars on the user's own machine, from the user's own copy of LRCLIB. plyrix doesn't redistribute LRCLIB's database dump; it links to the official source, https://lrclib.net/db-dumps, and credits LRCLIB. Follow LRCLIB's terms for the dump and what it holds.

Questions and concerns go in a GitHub issue. Report a security problem privately through https://github.com/patrickclery/plyrix/security/advisories/new.

## License

plyrix is MIT licensed; see [LICENSE](LICENSE).

plyrix is an independent project, not affiliated with or endorsed by Plex, Inc.
