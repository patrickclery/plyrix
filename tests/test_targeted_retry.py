"""Prove the scan after a dump update asks LRCLIB only about what the update could change (D-09).

Each test runs the pipeline in this process over copies of t.flac whose tags are patched per file,
against the stub LRCLIB. The dumps are recorded and diffed with the real `plyrix lrclib-record`
and `plyrix lrclib-diff` on the same state database, over invented LRCLIB-shaped databases. Dump
A is live first; then B, whose new database adds an LRCLIB track by "Artist X", is diffed and
recorded live.
"""

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import FIXTURES, RECORD, RunCli, StubLrclib, settings_for
from lrclib_fixture import add_named_track, make_db, renumber
from plyrix import pipeline, tags
from plyrix.config import Settings
from plyrix.domain import TrackTags
from plyrix.state import db

A = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"
B = "lrclib-db-dump-20261201T000000Z.sqlite3.gz"
C = "lrclib-db-dump-20261215T000000Z.sqlite3.gz"
D = "lrclib-db-dump-20261231T000000Z.sqlite3.gz"
BEFORE_A = "2026-01-01T00:00:00+00:00"
NOT_FOUND = (404, {"name": "TrackNotFound"})


class Retry:
    """A library of patched tracks, a state database, and dumps A and B."""

    def __init__(
        self, tmp_path: Path, stub: StubLrclib, monkeypatch: pytest.MonkeyPatch, run_cli: RunCli
    ) -> None:
        self.tmp = tmp_path
        self.stub = stub
        self.run_cli = run_cli
        self.lib = tmp_path / "lib"
        self.lib.mkdir()
        self.settings = settings_for(tmp_path, stub, monkeypatch)
        self.tags: dict[str, TrackTags] = {}
        self.reads: list[str] = []
        monkeypatch.setattr(tags, "read", self.read)
        self.env = {
            "PLYRIX_MUSIC_ROOT": str(self.lib),
            "PLYRIX_LRCLIB_URL": stub.base_url,
            "PLYRIX_STATE_DB": str(self.settings.state_db),
        }
        self.old_db = make_db(tmp_path / "lrclib-a.sqlite3", 20)

    def read(self, path: bytes) -> TrackTags:
        name = os.fsdecode(os.path.basename(path))
        self.reads.append(name)
        return self.tags[name]

    def add(self, name: str, artist: str, title: str, year: str | None = None) -> Path:
        path = self.lib / f"{name}.flac"
        shutil.copyfile(FIXTURES / "t.flac", path)
        self.tags[path.name] = TrackTags(artist, None, title, "Fixture Album", 3.0, year)
        return path

    def cli(self, *args: str) -> None:
        proc = self.run_cli(list(args), self.env)
        assert proc.returncode == 0, proc.stderr

    def live_a(self) -> None:
        self.cli("lrclib-record", A, "--status", "live")

    def run(self, *starts: Path, flag: bool = False, **changes: object) -> pipeline.Counts:
        settings = self.settings.model_copy(update=changes) if changes else self.settings
        if flag:
            # As Settings.load sets it for a --dump-id flag.
            settings = settings.model_copy()
            settings._dump_id_from_flag = True
        self.stub.requests.clear()
        self.reads.clear()
        return pipeline.run(settings, *(os.fsencode(start) for start in starts or (self.lib,)))

    def answer(self, status: int, body: object) -> None:
        self.stub.status, self.stub.body = status, body

    def found(self, artist: str, title: str, synced: bool = True) -> None:
        record = {**RECORD, "artistName": artist, "trackName": title}
        if not synced:
            record["syncedLyrics"] = None
        self.answer(200, record)

    def update_to_b(self, *artists: str, unstable: bool = False) -> None:
        """Diff B, which adds a track for each artist, against A, and record B live."""
        self.update_to(B, self.old_db, *artists, unstable=unstable)

    def update_to(self, name: str, previous: Path, *artists: str, unstable: bool = False) -> Path:
        """Diff a dump that adds a track for each artist to previous, record it live, return it."""
        new = self.tmp / f"{name}.db"
        shutil.copyfile(previous, new)
        for artist in artists:
            add_named_track(new, artist, f"New Song by {artist}")
        if unstable:
            renumber(new)
        self.cli("lrclib-diff", name, "--staged", str(new), "--previous-db", str(previous))
        self.cli("lrclib-record", name, "--status", "live")
        return new

    def ledger(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        con = sqlite3.connect(self.settings.state_db)
        try:
            found = con.execute(sql, params).fetchall()
            con.commit()
            return found
        finally:
            con.close()

    def seen_before_a(self, *names: str) -> None:
        for name in names:
            self.ledger(
                "UPDATE tracks SET first_seen_at = ? WHERE path = ?",
                (BEFORE_A, os.fsencode(self.lib / f"{name}.flac")),
            )

    def row(self, name: str) -> tuple[object, ...]:
        found = self.ledger(
            "SELECT outcome, dump_id FROM tracks WHERE path = ?",
            (os.fsencode(self.lib / f"{name}.flac"),),
        )
        return found[0]

    def unseen(self, name: str) -> object:
        found = self.ledger(
            "SELECT unseen_dump FROM tracks WHERE path = ?",
            (os.fsencode(self.lib / f"{name}.flac"),),
        )
        return found[0][0]

    def asked(self) -> set[str]:
        return {request["artist_name"] for request in self.stub.requests}


@pytest.fixture
def retry(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch, run_cli: RunCli
) -> Retry:
    return Retry(tmp_path, stub_lrclib, monkeypatch, run_cli)


def misses_under_a(retry: Retry, *tracks: tuple[str, str, str | None]) -> None:
    """Add tracks that miss under A, seen before A's date unless a test changes that."""
    for name, artist, year in tracks:
        retry.add(name, artist, f"Song {name}", year)
    retry.live_a()
    retry.answer(*NOT_FOUND)
    counts = retry.run()
    assert counts.no_match == len(tracks)
    retry.seen_before_a(*(name for name, _, _ in tracks))


def test_a_txt_whose_artist_changed_upgrades_to_lrc(retry: Retry) -> None:
    track = retry.add("x", "Artist X", "Song X", "2001")
    retry.live_a()
    retry.found("Artist X", "Song X", synced=False)
    retry.run()
    assert track.with_suffix(".txt").exists()
    retry.seen_before_a("x")
    retry.update_to_b("Artist X")
    retry.found("Artist X", "Song X")

    counts = retry.run()

    assert retry.asked() == {"Artist X"}
    assert counts.synced == 1
    assert track.with_suffix(".lrc").exists()
    assert not track.with_suffix(".txt").exists()
    assert retry.row("x") == ("synced", B)


def test_an_untouched_miss_is_not_asked_and_moves_to_the_new_dump(retry: Retry) -> None:
    misses_under_a(retry, ("y", "Artist Y", "2001"), ("x", "Artist X", "2001"))
    retry.update_to_b("Artist X")

    counts = retry.run()

    assert retry.asked() == {"Artist X"}
    assert counts.skipped_unchanged == 1
    # Nothing in the update could change y's answer, so it is B's answer too.
    assert retry.row("y") == ("no_match", B)
    assert retry.row("x") == ("no_match", B)


def test_an_untouched_miss_lets_old_change_sets_be_pruned(retry: Retry) -> None:
    misses_under_a(retry, ("y", "Artist Y", "2001"))
    previous = retry.old_db
    for name, artist in ((B, "Artist X"), (C, "Artist W"), (D, "Artist V")):
        previous = retry.update_to(name, previous, artist)
        retry.run()
        assert retry.asked() == set()

    kept = retry.ledger("SELECT DISTINCT dump FROM lrclib_changes")

    assert retry.row("y") == ("no_match", D)
    assert kept == [(D,)]


def gone_misses_under_a(retry: Retry, kept: str = "kept") -> None:
    """Add a miss y under A beside a synced track, which holds no change set back."""
    retry.add(kept, "Artist K", "Song K", "2001")
    retry.live_a()
    retry.found("Artist K", "Song K")
    retry.run()
    retry.add("y", "Artist Y", "Song y", "2001")
    retry.answer(*NOT_FOUND)
    assert retry.run().no_match == 1
    retry.seen_before_a("y")


def three_updates(retry: Retry, *artists: str) -> None:
    """Update to B, C and D in turn, with a run after each."""
    previous = retry.old_db
    for name, artist in zip((B, C, D), artists, strict=True):
        previous = retry.update_to(name, previous, artist)
        retry.run()


def test_a_deleted_miss_lets_old_change_sets_be_pruned(retry: Retry) -> None:
    gone_misses_under_a(retry)
    track = retry.lib / "y.flac"
    saved = retry.tmp / "y.flac.saved"
    shutil.move(track, saved)
    # B's diff holds y's artist, so its change set is what says y must be asked again.
    three_updates(retry, "Artist Y", "Artist W", "Artist V")

    kept = retry.ledger("SELECT DISTINCT dump FROM lrclib_changes")

    assert kept == [(D,)]
    assert retry.row("y") == ("no_match", A)
    assert retry.unseen("y") == D

    shutil.move(saved, track)
    retry.answer(*NOT_FOUND)
    retry.run()

    # B's change set is gone, so only the mark says the update since A could change y.
    assert retry.asked() == {"Artist Y"}
    assert retry.row("y") == ("no_match", D)
    assert retry.unseen("y") is None


def test_a_miss_back_under_its_own_dump_is_reached_without_a_lookup(retry: Retry) -> None:
    gone_misses_under_a(retry)
    track = retry.lib / "y.flac"
    saved = retry.tmp / "y.flac.saved"
    shutil.move(track, saved)
    retry.run()
    # y names the live dump, so it holds no change set back and needs no mark.
    assert retry.unseen("y") is None
    retry.ledger("UPDATE tracks SET unseen_dump = ? WHERE path = ?", (A, os.fsencode(track)))
    shutil.move(saved, track)

    retry.run()

    # Nothing changed since A, so nothing is asked, and the walk reaching it clears the mark.
    assert retry.asked() == set()
    assert retry.unseen("y") is None


def test_a_returning_miss_keeps_its_mark_until_a_lookup_answers(
    retry: Retry, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone_misses_under_a(retry)
    track = retry.lib / "y.flac"
    saved = retry.tmp / "y.flac.saved"
    shutil.move(track, saved)
    three_updates(retry, "Artist Y", "Artist W", "Artist V")
    assert retry.ledger("SELECT DISTINCT dump FROM lrclib_changes") == [(D,)]
    shutil.move(saved, track)
    lookup = pipeline._lookup
    failures = [None]

    def fails_once(*args: object) -> object:
        return failures.pop() if failures else lookup(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline, "_lookup", fails_once)
    retry.run()
    # LRCLIB couldn't answer, so nothing was written, and B's change set is gone: only the mark
    # still says y must be asked.
    assert retry.row("y") == ("no_match", A)
    assert retry.unseen("y") == D

    retry.answer(*NOT_FOUND)
    retry.run()

    assert retry.asked() == {"Artist Y"}
    assert retry.row("y") == ("no_match", D)
    assert retry.unseen("y") is None


def test_a_rewrite_under_an_older_dump_keeps_the_mark(retry: Retry) -> None:
    gone_misses_under_a(retry)
    track = retry.lib / "y.flac"
    saved = retry.tmp / "y.flac.saved"
    shutil.move(track, saved)
    three_updates(retry, "Artist Y", "Artist W", "Artist V")
    assert retry.ledger("SELECT DISTINCT dump FROM lrclib_changes") == [(D,)]
    assert retry.unseen("y") == D
    shutil.move(saved, track)
    # The stat moves and the tags don't, so a run under y's own dump A rewrites its row unasked.
    stamp = 1_700_000_000_000_000_000
    os.utime(track, ns=(stamp, stamp))
    retry.run(dump_id=A, flag=True)
    assert retry.asked() == set()
    # B's change set is gone, so the row still answers for A only and keeps D's mark.
    assert retry.row("y") == ("no_match", A)
    assert retry.unseen("y") == D

    retry.answer(*NOT_FOUND)
    retry.run()

    assert retry.asked() == {"Artist Y"}
    assert retry.row("y") == ("no_match", D)
    assert retry.unseen("y") is None


def test_a_miss_behind_a_foreign_lrc_lets_old_change_sets_be_pruned(retry: Retry) -> None:
    gone_misses_under_a(retry)
    # Another tool's untimed .lrc: every run stops at it, so y's row is never written again.
    (retry.lib / "y.lrc").write_text("not timed\n", encoding="utf-8")
    three_updates(retry, "Artist Y", "Artist W", "Artist V")

    assert retry.ledger("SELECT DISTINCT dump FROM lrclib_changes") == [(D,)]
    assert retry.row("y") == ("no_match", A)
    assert retry.unseen("y") == D


def test_a_miss_outside_the_music_root_lets_old_change_sets_be_pruned(retry: Retry) -> None:
    gone_misses_under_a(retry)
    # As after music_root moved: the row's path is no longer under the root walked.
    elsewhere = os.fsencode(retry.tmp / "old-root" / "y.flac")
    retry.ledger(
        "UPDATE tracks SET path = ? WHERE path = ?", (elsewhere, os.fsencode(retry.lib / "y.flac"))
    )
    (retry.lib / "y.flac").unlink()
    three_updates(retry, "Artist Y", "Artist W", "Artist V")

    assert retry.ledger("SELECT DISTINCT dump FROM lrclib_changes") == [(D,)]
    found = retry.ledger("SELECT dump_id, unseen_dump FROM tracks WHERE path = ?", (elsewhere,))
    assert found == [(A, D)]


def test_an_older_dump_id_override_moves_no_row_back(retry: Retry) -> None:
    misses_under_a(retry, ("y", "Artist Y", "2001"), ("g", "Artist G", "2001"))
    retry.update_to_b("Artist X")
    retry.run()
    assert (retry.row("y"), retry.row("g")) == (("no_match", B), ("no_match", B))
    (retry.lib / "g.flac").unlink()

    retry.run(dump_id=A, flag=True)

    # The override is older than live B: y keeps B, and nothing is marked under A.
    assert retry.row("y") == ("no_match", B)
    assert retry.unseen("g") is None


def test_an_excluded_miss_lets_old_change_sets_be_pruned(retry: Retry) -> None:
    gone_misses_under_a(retry)
    retry.settings = retry.settings.model_copy(update={"excludes": ["y.flac"]})
    three_updates(retry, "Artist X", "Artist W", "Artist V")

    assert retry.ledger("SELECT DISTINCT dump FROM lrclib_changes") == [(D,)]
    assert retry.unseen("y") == D


def test_a_run_on_part_of_the_library_marks_nothing_unseen(retry: Retry) -> None:
    sub = retry.lib / "sub"
    sub.mkdir()
    retry.add("kept", "Artist K", "Song K", "2001")
    shutil.move(retry.lib / "kept.flac", sub / "kept.flac")
    retry.live_a()
    retry.found("Artist K", "Song K")
    retry.run()
    retry.add("y", "Artist Y", "Song y", "2001")
    retry.answer(*NOT_FOUND)
    retry.run()
    retry.seen_before_a("y")
    retry.update_to_b("Artist X")

    retry.run(sub)

    # Only sub was walked, which says nothing of y.
    assert retry.unseen("y") is None
    assert retry.row("y") == ("no_match", A)


def test_a_miss_in_a_folder_that_cant_be_listed_isnt_unseen(retry: Retry) -> None:
    sub = retry.lib / "sub"
    sub.mkdir()
    retry.add("kept", "Artist K", "Song K", "2001")
    retry.live_a()
    retry.found("Artist K", "Song K")
    retry.run()
    retry.add("y", "Artist Y", "Song y", "2001")
    shutil.move(retry.lib / "y.flac", sub / "y.flac")
    retry.answer(*NOT_FOUND)
    retry.run()
    path = os.fsencode(sub / "y.flac")
    retry.ledger("UPDATE tracks SET first_seen_at = ? WHERE path = ?", (BEFORE_A, path))
    retry.update_to_b("Artist X")
    sub.chmod(0)
    try:
        counts = retry.run()
    finally:
        sub.chmod(0o755)

    assert counts.folder_unreadable == 1
    found = retry.ledger("SELECT dump_id, unseen_dump FROM tracks WHERE path = ?", (path,))
    assert found == [(A, None)]


def test_a_dry_run_marks_nothing_unseen(retry: Retry, monkeypatch: pytest.MonkeyPatch) -> None:
    gone_misses_under_a(retry)
    (retry.lib / "y.flac").unlink()
    retry.update_to_b("Artist X")
    # A dry run's writes go to a memory copy, so the file can't show a mark it made: watch the call.
    marked: list[list[bytes]] = []
    set_unseen = db.set_unseen

    def spy(conn: db.Connection, unseen: list[bytes], seen: list[bytes], dump_id: str) -> None:
        marked.append(list(unseen))
        set_unseen(conn, unseen, seen, dump_id)

    monkeypatch.setattr(db, "set_unseen", spy)

    retry.run(dry_run=True)

    assert marked == []
    # The same walk, not dry, does mark y, so the spy sees what a dry run would have done.
    retry.run()
    assert marked == [[os.fsencode(retry.lib / "y.flac")]]
    assert retry.unseen("y") == B


def test_a_miss_under_an_unrecorded_dump_id_keeps_its_dump(retry: Retry) -> None:
    misses_under_a(retry, ("y", "Artist Y", "2001"))
    retry.update_to_b("Artist X")
    retry.ledger("DELETE FROM lrclib_dumps WHERE name = ?", (B,))

    retry.run(dump_id=C)

    # C isn't a dump the retry rule knows, so naming it would make y's next check blind.
    assert retry.row("y")[1] != C


def test_a_track_released_in_or_after_the_dumps_year_is_asked(retry: Retry) -> None:
    misses_under_a(retry, ("old", "Artist Old", "2001"), ("new", "Artist New", "2026-03-01"))
    retry.update_to_b("Artist X")

    retry.run()

    assert retry.asked() == {"Artist New"}


def test_a_track_first_seen_after_the_dump_is_asked(retry: Retry) -> None:
    misses_under_a(retry, ("old", "Artist Old", "2001"), ("new", "Artist New", "2001"))
    # The new track was first seen now, which is after A's date.
    retry.ledger(
        "UPDATE tracks SET first_seen_at = '2026-11-01T00:00:00+00:00' WHERE path = ?",
        (os.fsencode(retry.lib / "new.flac"),),
    )
    retry.update_to_b("Artist X")

    retry.run()

    assert retry.asked() == {"Artist New"}


def test_with_no_first_seen_the_files_ctime_counts(retry: Retry) -> None:
    misses_under_a(retry, ("old", "Artist Old", "2001"), ("new", "Artist New", "2001"))
    retry.ledger(
        "UPDATE tracks SET first_seen_at = NULL WHERE path = ?",
        (os.fsencode(retry.lib / "new.flac"),),
    )
    retry.update_to_b("Artist X")

    retry.run()

    # The file was copied today, after A's date.
    assert retry.asked() == {"Artist New"}


def test_a_row_with_no_keys_is_asked(retry: Retry) -> None:
    misses_under_a(retry, ("old", "Artist Old", "2001"), ("keyless", "Artist K", "2001"))
    retry.ledger(
        "UPDATE tracks SET artist_key = NULL, title_key = NULL WHERE path = ?",
        (os.fsencode(retry.lib / "keyless.flac"),),
    )
    retry.update_to_b("Artist X")

    retry.run()

    assert retry.asked() == {"Artist K"}


def test_a_row_from_an_unknown_dump_is_asked(retry: Retry) -> None:
    misses_under_a(retry, ("old", "Artist Old", "2001"), ("odd", "Artist U", "2001"))
    retry.ledger(
        "UPDATE tracks SET dump_id = 'lrclib-db-dump-20250101T000000Z.sqlite3.gz' WHERE path = ?",
        (os.fsencode(retry.lib / "odd.flac"),),
    )
    retry.update_to_b("Artist X")

    retry.run()

    assert retry.asked() == {"Artist U"}


def test_an_unstable_diff_asks_every_soft_row(retry: Retry) -> None:
    misses_under_a(retry, ("y", "Artist Y", "2001"), ("z", "Artist Z", "2001"))
    retry.update_to_b("Artist X", unstable=True)

    retry.run()

    assert retry.asked() == {"Artist Y", "Artist Z"}


def test_full_asks_every_soft_row(retry: Retry) -> None:
    misses_under_a(retry, ("y", "Artist Y", "2001"), ("z", "Artist Z", "2001"))
    retry.update_to_b("Artist X")

    retry.run(full=True)

    assert retry.asked() == {"Artist Y", "Artist Z"}


def test_a_stale_lrc_is_rechecked_only_when_its_key_changed(retry: Retry) -> None:
    retry.live_a()
    for name, artist in (("s1", "Artist X"), ("s2", "Artist S")):
        retry.add(name, artist, f"Song {name}", "2001")
        retry.found(artist, f"Song {name}")
        retry.run()
    retry.ledger("UPDATE tracks SET reason = 'lrc_stale'")
    retry.seen_before_a("s1", "s2")
    assert retry.ledger("SELECT DISTINCT outcome, dump_id FROM tracks") == [("synced", A)]
    retry.update_to_b("Artist X")

    retry.run()

    assert retry.reads == ["s1.flac"]
    # The update couldn't change s2's answer, so it names B, and A's change sets can go.
    assert retry.row("s2") == ("synced", B)


@pytest.mark.parametrize("origin", ["proved", "timed"])
def test_a_stale_lrc_plyrix_cant_replace_lets_old_change_sets_be_pruned(
    retry: Retry, origin: str
) -> None:
    retry.live_a()
    # Released in A's year, so the update since its dump would always count as able to change it.
    track = retry.add("s", "Artist S", "Song s", "2026")
    retry.found("Artist S", "Song s")
    retry.run()
    retry.ledger("UPDATE tracks SET reason = 'lrc_stale'")
    retry.ledger("UPDATE sidecars SET origin = ?", (origin,))
    retry.seen_before_a("s")
    previous = retry.old_db
    for name, artist in ((B, "Artist S"), (C, "Artist W"), (D, "Artist V")):
        previous = retry.update_to(name, previous, artist)
        retry.run()
        # Nothing can replace it, so it is neither looked up nor read again.
        assert retry.asked() == set()
        assert retry.reads == []
        assert retry.row("s") == ("synced", name)

    kept = retry.ledger("SELECT DISTINCT dump FROM lrclib_changes")

    assert kept == [(D,)]
    assert track.with_suffix(".lrc").exists()


def test_a_stale_lrc_plyrix_wrote_is_still_rechecked_after_an_update(retry: Retry) -> None:
    retry.live_a()
    retry.add("s", "Artist S", "Song s", "2026")
    retry.found("Artist S", "Song s")
    retry.run()
    retry.ledger("UPDATE tracks SET reason = 'lrc_stale'")
    retry.seen_before_a("s")
    retry.update_to_b("Artist X")

    retry.run()

    # Its own .lrc can be replaced, and the year says B may hold a better one, so it is read.
    assert retry.reads == ["s.flac"]


def test_run_full_is_a_cli_flag(run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    help_text = run_cli(["run", "--help"]).stdout.decode()
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copyfile(FIXTURES / "t.flac", lib / "t.flac")

    proc = run_cli(["run", "--full", str(lib)], {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert "--full" in help_text
    assert proc.returncode == 0, proc.stderr
    assert Settings.model_fields["full"].default is False
