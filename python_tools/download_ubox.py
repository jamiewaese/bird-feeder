"""Download recent UBox SD events over the LAN and import them."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from camera.sdcard import FilesystemMediaSource, MediaImporter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download recent UBox SD media over the LAN and import it."
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--camera-id", default="yard")
    parser.add_argument("--lookback-hours", type=int, default=36)
    return parser


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
        native_failed = False
        event_index = 0
        while event_index < 240:
            event_exists = True
            for file_type in (2, 1):
                command = [
                    str(executable),
                    str(transport),
                    str(staging),
                    str(library / "media" / args.camera_id),
                    str(args.lookback_hours),
                    str(event_index),
                    str(file_type),
                ]
                returncode = 1
                for attempt in range(3):
                    native = subprocess.run(command, env=environment, check=False)
                    returncode = native.returncode
                    if returncode in {0, 10, 11}:
                        break
                    if attempt < 2:
                        time.sleep(30)
                if returncode == 11:
                    event_exists = False
                    break
                if returncode not in {0, 10}:
                    native_failed = True
                    break
                time.sleep(30)
            if native_failed or not event_exists:
                break
            event_index += 1

        source = FilesystemMediaSource(staging, source_id=args.camera_id)
        imported = MediaImporter(library).sync(source)
        print(json.dumps(asdict(imported), indent=2, sort_keys=True))

    if imported.failed or native_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
