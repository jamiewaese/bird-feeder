"""Pre-generate messaging-app-compatible copies of catalogued videos."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from camera.mobile import MobileVideoError, MobileVideoPreparer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare cached phone-compatible copies of imported MP4 videos."
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--max-videos", type=int, default=0, help="zero means all")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_videos < 0:
        raise SystemExit("--max-videos cannot be negative")
    library = args.library.expanduser().resolve()
    connection = sqlite3.connect(library / "catalog.sqlite3")
    try:
        query = "SELECT relative_path FROM media WHERE kind = 'video' ORDER BY id"
        paths = [row[0] for row in connection.execute(query)]
    finally:
        connection.close()
    if args.max_videos:
        paths = paths[: args.max_videos]

    preparer = MobileVideoPreparer(library)
    prepared = failed = 0
    for relative_path in paths:
        try:
            preparer.prepare(relative_path)
            prepared += 1
        except MobileVideoError as error:
            failed += 1
            print(f"failed {relative_path}: {error}")
    print(json.dumps({"discovered": len(paths), "prepared": prepared, "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
