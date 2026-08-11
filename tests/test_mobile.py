from __future__ import annotations

import os
import tempfile
import time
import unittest
from stat import S_IMODE
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from camera.mobile import MobileVideoError, MobileVideoPreparer


class MobileVideoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.library = Path(self.temporary.name)
        self.relative = "yard/video/260809/092443_150_031_P.mp4"
        self.source = self.library / "media" / self.relative
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"camera-mp4")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepares_conservative_mp4_once_and_reuses_cache(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            calls.append(command)
            Path(command[-1]).write_bytes(b"phone-compatible-mp4")
            return SimpleNamespace(returncode=0, stderr="")

        preparer = MobileVideoPreparer(self.library)
        with (
            patch("camera.mobile.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("camera.mobile.subprocess.run", side_effect=fake_run),
        ):
            first = preparer.prepare(self.relative)
            second = preparer.prepare(self.relative)

        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), b"phone-compatible-mp4")
        self.assertEqual(S_IMODE(first.stat().st_mode), 0o660)
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertIn("+discardcorrupt", command)
        self.assertIn("scale=w='min(1280,iw)':h=-2:flags=lanczos,format=yuv420p", command)
        self.assertEqual(command[command.index("-profile:v") + 1], "main")
        self.assertEqual(command[command.index("-level:v") + 1], "3.1")
        self.assertEqual(command[command.index("-tag:v") + 1], "avc1")
        self.assertEqual(command[command.index("-movflags") + 1], "+faststart")
        self.assertEqual(command[command.index("-brand") + 1], "mp42")
        self.assertEqual(first.stat().st_mtime_ns, self.source.stat().st_mtime_ns)

    def test_future_dated_source_is_transcoded_once_and_then_cached(self) -> None:
        future_mtime_ns = time.time_ns() + 60_000_000_000
        os.utime(self.source, ns=(future_mtime_ns, future_mtime_ns))
        calls = 0

        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            Path(command[-1]).write_bytes(b"phone-compatible-mp4")
            return SimpleNamespace(returncode=0, stderr="")

        preparer = MobileVideoPreparer(self.library)
        with (
            patch("camera.mobile.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("camera.mobile.subprocess.run", side_effect=fake_run),
        ):
            first = preparer.prepare(self.relative)
            second = preparer.prepare(self.relative)

        self.assertEqual(first, second)
        self.assertEqual(calls, 1)
        self.assertEqual(first.stat().st_mtime_ns, future_mtime_ns)

    def test_source_replacement_with_older_timestamp_invalidates_cache(self) -> None:
        calls = 0

        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            Path(command[-1]).write_bytes(f"prepared-{calls}".encode())
            return SimpleNamespace(returncode=0, stderr="")

        preparer = MobileVideoPreparer(self.library)
        with (
            patch("camera.mobile.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("camera.mobile.subprocess.run", side_effect=fake_run),
        ):
            first = preparer.prepare(self.relative)
            original_mtime_ns = self.source.stat().st_mtime_ns
            self.source.write_bytes(b"replacement-camera-mp4")
            older_mtime_ns = original_mtime_ns - 1_000_000_000
            os.utime(self.source, ns=(older_mtime_ns, older_mtime_ns))
            second = preparer.prepare(self.relative)

        self.assertEqual(first, second)
        self.assertEqual(calls, 2)
        self.assertEqual(second.read_bytes(), b"prepared-2")
        self.assertEqual(second.stat().st_mtime_ns, older_mtime_ns)

    def test_rejects_paths_outside_media_library(self) -> None:
        preparer = MobileVideoPreparer(self.library)
        for path in ("../secret.mp4", "/tmp/secret.mp4", "yard/video/file.jpg"):
            with self.subTest(path=path), self.assertRaises(MobileVideoError):
                preparer.prepare(path)

    def test_cached_never_creates_a_missing_prepared_copy(self) -> None:
        preparer = MobileVideoPreparer(self.library)
        self.assertIsNone(preparer.cached(self.relative))
        self.assertFalse((self.library / "mobile-v2").exists())

        destination = self.library / "mobile-v2" / self.relative
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"already-prepared")
        source_mtime_ns = self.source.stat().st_mtime_ns
        os.utime(destination, ns=(source_mtime_ns, source_mtime_ns))

        self.assertEqual(preparer.cached(self.relative), destination.resolve())
        self.assertEqual(destination.read_bytes(), b"already-prepared")

    def test_downloader_prepares_share_copies_even_after_transfer_failure(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        service = (
            project_root / "deploy/systemd/bird-feeder-ubox-download.service"
        ).read_text()

        self.assertIn(
            "ExecStopPost=/usr/bin/python3 -m python_tools.prepare_mobile_videos",
            service,
        )
        self.assertNotIn("ExecStartPost=", service)


if __name__ == "__main__":
    unittest.main()
