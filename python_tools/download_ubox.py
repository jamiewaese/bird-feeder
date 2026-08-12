"""Download recent UBox SD events over the LAN and import them."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from camera.sdcard import FilesystemMediaSource, ImportResult, MediaImporter


NATIVE_PRESENT = 0
NATIVE_DOWNLOADED = 10
NATIVE_END_OF_EVENTS = 11
MAX_EVENTS = 240
SESSION_DELAY_SECONDS = 30


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download recent UBox SD media over the LAN and import it."
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--camera-id", default="yard")
    parser.add_argument("--lookback-hours", type=int, default=36)
    return parser


def download_events(
    *,
    executable: Path,
    transport: Path,
    staging: Path,
    existing_root: Path,
    lookback_hours: int,
    environment: dict[str, str],
    run=subprocess.run,
    sleep=time.sleep,
    after_download: Callable[[], None] | None = None,
) -> bool:
    """Download every reported event, returning whether any file failed.

    A single corrupt or lossy camera file must not prevent newer visits from
    being attempted. Existing files need no cooldown; retain the conservative
    delay only after retries and real transfers.
    """

    native_failed = False
    event_index = 0
    while event_index < MAX_EVENTS:
        event_exists = True
        for file_type in (2, 1):
            command = [
                str(executable),
                str(transport),
                str(staging),
                str(existing_root),
                str(lookback_hours),
                str(event_index),
                str(file_type),
            ]
            returncode = 1
            for attempt in range(3):
                native = run(command, env=environment, check=False)
                returncode = native.returncode
                if returncode in {
                    NATIVE_PRESENT,
                    NATIVE_DOWNLOADED,
                    NATIVE_END_OF_EVENTS,
                }:
                    break
                if attempt < 2:
                    sleep(SESSION_DELAY_SECONDS)

            if returncode == NATIVE_END_OF_EVENTS:
                event_exists = False
                break
            if returncode not in {NATIVE_PRESENT, NATIVE_DOWNLOADED}:
                native_failed = True
                continue
            if returncode == NATIVE_DOWNLOADED:
                if after_download is not None:
                    after_download()
                sleep(SESSION_DELAY_SECONDS)

        if not event_exists:
            break
        event_index += 1

    return native_failed


def main() -> int:
    args = build_parser().parse_args()
    if not 1 <= args.lookback_hours <= 168:
        raise SystemExit("--lookback-hours must be from 1 through 168")

    library = args.library.expanduser().resolve()
    native_dir = args.native_dir.expanduser().resolve()
    executable = native_dir / "ubox-connect"
    transport = native_dir / "libUBICAPIs.so"
    compatibility = native_dir / "liblog.so"
    for required in (executable, transport, compatibility):
        if not required.is_file():
            raise SystemExit(f"required UBox component is missing: {required}")
    if not os.environ.get("UBOX_UID") or not os.environ.get("UBOX_PASSWORD"):
        raise SystemExit("UBOX_UID and UBOX_PASSWORD must be set")

    incoming = library / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["LD_LIBRARY_PATH"] = str(native_dir)
    environment["LD_PRELOAD"] = str(compatibility)

    with tempfile.TemporaryDirectory(prefix=".ubox-", dir=incoming) as staging_name:
        staging = Path(staging_name)
        import_results: list[ImportResult] = []

        def import_staging() -> None:
            source = FilesystemMediaSource(
                staging,
                source_id=args.camera_id,
                delete_suppressed=True,
            )
            import_results.append(MediaImporter(library).sync(source))

        native_failed = download_events(
            executable=executable,
            transport=transport,
            staging=staging,
            existing_root=library / "media" / args.camera_id,
            lookback_hours=args.lookback_hours,
            environment=environment,
            after_download=import_staging,
        )

        source = FilesystemMediaSource(
            staging,
            source_id=args.camera_id,
            delete_suppressed=True,
        )
        final_import = MediaImporter(library).sync(source)
        imported = ImportResult(
            discovered=final_import.discovered,
            imported=sum(result.imported for result in import_results)
            + final_import.imported,
            unchanged=final_import.unchanged,
            suppressed=final_import.suppressed,
            pending=final_import.pending,
            failed=sum(result.failed for result in import_results)
            + final_import.failed,
        )
        print(json.dumps(asdict(imported), indent=2, sort_keys=True))

    if imported.failed or native_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
