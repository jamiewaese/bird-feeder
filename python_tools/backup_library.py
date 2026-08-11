"""Incrementally back up the gallery to a separately mounted filesystem."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path


SKIPPED_TOP_LEVEL = {"incoming", ".deleting", "mobile-v1", "mobile-v2"}


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_if_changed(source: Path, destination: Path) -> bool:
    try:
        source_stat = source.stat()
        destination_stat = destination.stat()
        if (
            source_stat.st_size == destination_stat.st_size
            and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
        ):
            return False
    except FileNotFoundError:
        pass
    _atomic_copy(source, destination)
    return True


def _backup_catalog(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def backup_library(
    library: Path,
    destination: Path,
    *,
    required_mount: Path | None = None,
    catalog_retention_days: int | None = None,
) -> dict[str, int]:
    library = library.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve()
    if required_mount is not None:
        required_mount = required_mount.expanduser().resolve(strict=True)
        if not required_mount.is_mount():
            raise RuntimeError(f"backup filesystem is not mounted: {required_mount}")
        if not _inside(destination, required_mount):
            raise ValueError("destination is outside the required backup mount")
    if _inside(destination, library) or _inside(library, destination):
        raise ValueError("library and backup destination must not contain one another")
    if catalog_retention_days is not None and catalog_retention_days < 1:
        raise ValueError("catalog retention must be at least one day")
    destination.mkdir(parents=True, exist_ok=True)

    copied_files = 0
    copied_bytes = 0
    for source in library.rglob("*"):
        relative = source.relative_to(library)
        if not relative.parts or relative.parts[0] in SKIPPED_TOP_LEVEL:
            continue
        if (
            source.name.startswith("catalog.sqlite3")
            or source.name.endswith(".part")
            or source.is_symlink()
            or not source.is_file()
        ):
            continue
        resolved_source = source.resolve(strict=True)
        if not _inside(resolved_source, library):
            raise ValueError(f"source escaped library: {relative}")
        target = destination / "files" / relative
        if _copy_if_changed(resolved_source, target):
            copied_files += 1
            copied_bytes += resolved_source.stat().st_size

    catalog = library / "catalog.sqlite3"
    if catalog.is_file():
        catalog_dir = destination / "catalogs"
        catalog_target = catalog_dir / f"catalog-{date.today().isoformat()}.sqlite3"
        _backup_catalog(catalog, catalog_target)
        if catalog_retention_days is not None:
            cutoff = date.today() - timedelta(days=catalog_retention_days)
            for candidate in catalog_dir.glob("catalog-????-??-??.sqlite3"):
                try:
                    candidate_date = date.fromisoformat(
                        candidate.stem.removeprefix("catalog-")
                    )
                except ValueError:
                    continue
                if candidate_date < cutoff:
                    candidate.unlink()

    return {"files": copied_files, "bytes": copied_bytes}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up the bird gallery to a separately mounted filesystem"
    )
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--require-mount", type=Path)
    parser.add_argument("--catalog-retention-days", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = backup_library(
        args.library,
        args.destination,
        required_mount=args.require_mount,
        catalog_retention_days=args.catalog_retention_days,
    )
    print(f"backup complete: {result['files']} changed files, {result['bytes']} bytes")


if __name__ == "__main__":
    main()
