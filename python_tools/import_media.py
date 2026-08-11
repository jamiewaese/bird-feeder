"""Import a mounted B4 card into the Raspberry Pi media library."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from camera.sdcard import FilesystemMediaSource, MediaImporter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently import the B4 /video and /snaps card layout. "
            "No source or library files are deleted."
        )
    )
    parser.add_argument("--source", type=Path, required=True, help="mounted card root")
    parser.add_argument("--library", type=Path, required=True, help="Pi library root")
    parser.add_argument("--camera-id", default="b4-yard", help="safe camera identifier")
    parser.add_argument("--dry-run", action="store_true", help="report without copying")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = FilesystemMediaSource(args.source, source_id=args.camera_id)
    result = MediaImporter(args.library).sync(source, dry_run=args.dry_run)
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
