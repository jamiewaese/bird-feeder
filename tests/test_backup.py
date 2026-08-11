from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from python_tools.backup_library import backup_library


class BackupLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.library = root / "library"
        self.destination = root / "backup"
        (self.library / "media/yard/video").mkdir(parents=True)
        self.destination.mkdir()
        self.media = self.library / "media/yard/video/bird.mp4"
        self.media.write_bytes(b"bird-video")
        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker VALUES ('ok')")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_incremental_copy_and_consistent_catalog(self) -> None:
        first = backup_library(self.library, self.destination)
        self.assertEqual(first, {"files": 1, "bytes": len(b"bird-video")})
        copied = self.destination / "files/media/yard/video/bird.mp4"
        self.assertEqual(copied.read_bytes(), b"bird-video")

        catalogs = list((self.destination / "catalogs").glob("catalog-*.sqlite3"))
        self.assertEqual(len(catalogs), 1)
        connection = sqlite3.connect(catalogs[0])
        try:
            value = connection.execute("SELECT value FROM marker").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(value, "ok")

        second = backup_library(self.library, self.destination)
        self.assertEqual(second, {"files": 0, "bytes": 0})

    def test_does_not_propagate_source_deletion(self) -> None:
        backup_library(self.library, self.destination)
        self.media.unlink()
        backup_library(self.library, self.destination)
        copied = self.destination / "files/media/yard/video/bird.mp4"
        self.assertEqual(copied.read_bytes(), b"bird-video")

    def test_rejects_nested_destination(self) -> None:
        nested = self.library / "backup"
        nested.mkdir()
        with self.assertRaises(ValueError):
            backup_library(self.library, nested)


if __name__ == "__main__":
    unittest.main()
