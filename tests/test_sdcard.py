from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from camera.sdcard import FilesystemMediaSource, MediaImporter
from camera.sdcard.layout import parse_media_path


class LayoutTests(unittest.TestCase):
    def test_parse_observed_video_path(self) -> None:
        parsed = parse_media_path(
            PurePosixPath("video/260809/092443_150_031_P.mp4")
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.kind, "video")
        self.assertEqual(parsed.date_code, "260809")
        self.assertEqual(parsed.time_code, "092443")
        self.assertEqual(parsed.subsecond_code, "150")
        self.assertEqual(parsed.duration_code, "031")
        self.assertEqual(parsed.event_code, "P")
        self.assertEqual(parsed.pair_key, "260809/092443_150_031_P")

    def test_reject_mismatched_and_unobserved_paths(self) -> None:
        invalid = (
            "video/260809/092443_150_031_P.jpg",
            "snaps/260809/092443_150_031_P.mp4",
            "video/not-a-date/092443_150_031_P.mp4",
            "other/260809/092443_150_031_P.mp4",
            "video/260809/nested/092443_150_031_P.mp4",
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                self.assertIsNone(parse_media_path(PurePosixPath(candidate)))


class ImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source_root = root / "card"
        self.library_root = root / "library"
        (self.source_root / "video" / "260809").mkdir(parents=True)
        (self.source_root / "snaps" / "260809").mkdir(parents=True)
        self.video = self.source_root / "video/260809/092443_150_031_P.mp4"
        self.snapshot = self.source_root / "snaps/260809/092443_150_031_P.jpg"
        self.video.write_bytes(b"fake-mp4-data")
        self.snapshot.write_bytes(b"fake-jpeg-data")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source(self) -> FilesystemMediaSource:
        return FilesystemMediaSource(self.source_root, "yard")

    def test_discovers_only_valid_regular_media(self) -> None:
        (self.source_root / "video/260809/notes.txt").write_text("ignore")
        objects = list(self._source().iter_media())
        self.assertEqual([item.kind for item in objects], ["snapshot", "video"])
        self.assertEqual(len(objects), 2)

    def test_accepts_backup_videos_alias_without_changing_output_layout(self) -> None:
        self.video.unlink()
        (self.source_root / "video/260809").rmdir()
        (self.source_root / "video").rmdir()
        alias = self.source_root / "videos/260809"
        alias.mkdir(parents=True)
        (alias / "092443_150_031_P.mp4").write_bytes(b"backup-video")

        objects = list(self._source().iter_media())
        video = next(item for item in objects if item.kind == "video")
        self.assertEqual(video.source_key, "videos/260809/092443_150_031_P.mp4")
        self.assertEqual(
            video.relative_path.as_posix(),
            "video/260809/092443_150_031_P.mp4",
        )

    def test_import_is_idempotent_and_catalogued(self) -> None:
        importer = MediaImporter(self.library_root)
        first = importer.sync(self._source())
        second = importer.sync(self._source())

        self.assertEqual(first.discovered, 2)
        self.assertEqual(first.imported, 2)
        self.assertEqual(first.failed, 0)
        self.assertEqual(second.unchanged, 2)
        self.assertEqual(second.imported, 0)

        copied_video = (
            self.library_root / "media/yard/video/260809/092443_150_031_P.mp4"
        )
        self.assertEqual(copied_video.read_bytes(), b"fake-mp4-data")
        self.assertEqual(stat.S_IMODE(copied_video.stat().st_mode), 0o660)

        connection = sqlite3.connect(self.library_root / "catalog.sqlite3")
        try:
            rows = connection.execute(
                "SELECT kind, sha256 FROM media ORDER BY kind"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], ["snapshot", "video"])
        self.assertEqual(rows[1][1], hashlib.sha256(b"fake-mp4-data").hexdigest())

    def test_changed_source_replaces_library_copy(self) -> None:
        importer = MediaImporter(self.library_root)
        importer.sync(self._source())
        original_stat = self.video.stat()
        self.video.write_bytes(b"replacement-video-data")
        os.utime(
            self.video,
            ns=(original_stat.st_mtime_ns + 1_000_000_000,) * 2,
        )

        result = importer.sync(self._source())
        copied_video = (
            self.library_root / "media/yard/video/260809/092443_150_031_P.mp4"
        )
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(copied_video.read_bytes(), b"replacement-video-data")

    def test_ephemeral_source_deletes_files_suppressed_by_tombstone(self) -> None:
        importer = MediaImporter(self.library_root)
        importer.sync(self._source())
        connection = sqlite3.connect(self.library_root / "catalog.sqlite3")
        try:
            connection.execute(
                """
                INSERT INTO deleted_pairs (source_id, pair_key, deleted_at)
                VALUES ('yard', '260809/092443_150_031_P', '2026-08-11T00:00:00Z')
                """
            )
            connection.execute("DELETE FROM media")
            connection.commit()
        finally:
            connection.close()

        result = importer.sync(
            FilesystemMediaSource(
                self.source_root,
                "yard",
                delete_suppressed=True,
            )
        )

        self.assertEqual(result.suppressed, 2)
        self.assertEqual(result.failed, 0)
        self.assertFalse(self.snapshot.exists())
        self.assertFalse(self.video.exists())

    def test_dry_run_does_not_create_library(self) -> None:
        result = MediaImporter(self.library_root).sync(self._source(), dry_run=True)
        self.assertEqual(result.pending, 2)
        self.assertFalse(self.library_root.exists())


if __name__ == "__main__":
    unittest.main()
