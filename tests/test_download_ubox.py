from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from python_tools.download_ubox import download_events


class DownloadEventsTests(unittest.TestCase):
    def test_continues_after_file_failure_and_only_delays_real_work(self) -> None:
        returncodes = iter(
            [
                1,
                1,
                1,  # event 0 JPEG exhausts its retries
                0,  # event 0 video already exists
                10,  # event 1 JPEG downloads
                0,  # event 1 video already exists
                11,  # end of events
            ]
        )
        commands: list[list[str]] = []
        sleeps: list[float] = []
        imported: list[bool] = []

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess:
            commands.append(command)
            return subprocess.CompletedProcess(command, next(returncodes))

        failed = download_events(
            executable=Path("/native/ubox-connect"),
            transport=Path("/native/libUBICAPIs.so"),
            staging=Path("/library/incoming/run"),
            existing_root=Path("/library/media/yard"),
            lookback_hours=36,
            environment={},
            run=run,
            sleep=sleeps.append,
            after_download=lambda: imported.append(True),
        )

        self.assertTrue(failed)
        self.assertEqual([command[-2:] for command in commands], [
            ["0", "2"], ["0", "2"], ["0", "2"], ["0", "1"],
            ["1", "2"], ["1", "1"], ["2", "2"],
        ])
        self.assertEqual(sleeps, [30, 30, 30])
        self.assertEqual(imported, [True])

    def test_stops_cleanly_at_end_of_events(self) -> None:
        returncodes = iter([0, 0, 11])
        sleeps: list[float] = []

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(command, next(returncodes))

        failed = download_events(
            executable=Path("ubox-connect"),
            transport=Path("libUBICAPIs.so"),
            staging=Path("staging"),
            existing_root=Path("media"),
            lookback_hours=24,
            environment={},
            run=run,
            sleep=sleeps.append,
        )

        self.assertFalse(failed)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
