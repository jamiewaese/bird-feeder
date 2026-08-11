"""Idempotently copy source media into a Raspberry Pi media library."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from camera.classification import CLASSIFICATION_SCHEMA, ensure_classification_schema

from .source import MediaObject, MediaSource


DELETED_PAIRS_SCHEMA = """
CREATE TABLE IF NOT EXISTS deleted_pairs (
    source_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (source_id, pair_key)
);
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('video', 'snapshot')),
    date_code TEXT NOT NULL,
    time_code TEXT NOT NULL,
    subsecond_code TEXT NOT NULL,
    duration_code TEXT NOT NULL,
    event_code TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    source_modified_ns INTEGER,
    sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE (source_id, source_key)
);
CREATE INDEX IF NOT EXISTS media_capture_order
    ON media (date_code DESC, time_code DESC, subsecond_code DESC);
CREATE INDEX IF NOT EXISTS media_pair
    ON media (source_id, pair_key);
""" + DELETED_PAIRS_SCHEMA + CLASSIFICATION_SCHEMA


@dataclass(frozen=True)
class ImportResult:
    discovered: int = 0
    imported: int = 0
    unchanged: int = 0
    suppressed: int = 0
    pending: int = 0
    failed: int = 0


class MediaImporter:
    """Synchronize one media source without deleting anything."""

    def __init__(self, library_root: Path) -> None:
        self.library_root = library_root.expanduser().resolve()
        self.media_root = self.library_root / "media"
        self.catalog_path = self.library_root / "catalog.sqlite3"

    def sync(self, source: MediaSource, *, dry_run: bool = False) -> ImportResult:
        if not dry_run:
            self.library_root.mkdir(parents=True, exist_ok=True)
            self.media_root.mkdir(parents=True, exist_ok=True)

        connection = self._connect(read_only=dry_run and not self.catalog_path.exists())
        discovered = imported = unchanged = suppressed = pending = failed = 0
        try:
            for media in source.iter_media():
                discovered += 1
                deleted = connection.execute(
                    """
                    SELECT 1 FROM deleted_pairs
                    WHERE source_id = ? AND pair_key = ?
                    """,
                    (source.source_id, media.pair_key),
                ).fetchone()
                if deleted is not None:
                    suppressed += 1
                    continue
                destination_relative = Path(source.source_id).joinpath(
                    *media.relative_path.parts
                )
                destination = self.media_root / destination_relative
                row = connection.execute(
                    """
                    SELECT size_bytes, source_modified_ns
                    FROM media
                    WHERE source_id = ? AND source_key = ?
                    """,
                    (source.source_id, media.source_key),
                ).fetchone()
                if (
                    row is not None
                    and row[0] == media.size_bytes
                    and row[1] == media.modified_ns
                    and destination.is_file()
                ):
                    unchanged += 1
                    continue

                if dry_run:
                    pending += 1
                    continue

                try:
                    digest = self._copy_atomic(source, media, destination)
                    self._record(
                        connection,
                        source.source_id,
                        media,
                        destination_relative,
                        digest,
                    )
                    connection.commit()
                    imported += 1
                except (OSError, ValueError, sqlite3.DatabaseError):
                    connection.rollback()
                    failed += 1
        finally:
            connection.close()

        return ImportResult(
            discovered=discovered,
            imported=imported,
            unchanged=unchanged,
            suppressed=suppressed,
            pending=pending,
            failed=failed,
        )

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(":memory:")
        else:
            self.library_root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.catalog_path)
        connection.executescript(SCHEMA)
        ensure_classification_schema(connection)
        return connection

    @staticmethod
    def _copy_stream(source: BinaryIO, destination: BinaryIO) -> str:
        digest = hashlib.sha256()
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            destination.write(chunk)
            digest.update(chunk)
        return digest.hexdigest()

    def _copy_atomic(
        self,
        source: MediaSource,
        media: MediaObject,
        destination: Path,
    ) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                with source.open_media(media) as source_stream:
                    digest = self._copy_stream(source_stream, temporary)
                # NamedTemporaryFile starts at 0600 regardless of the service
                # umask. Imported media is shared with gallery processes via
                # the birdfeeder group, so preserve group-read/write access.
                os.fchmod(temporary.fileno(), 0o660)
                temporary.flush()
                os.fsync(temporary.fileno())

            copied_size = os.stat(temporary_name).st_size
            if copied_size != media.size_bytes:
                raise OSError(
                    f"source changed while copying: expected {media.size_bytes}, "
                    f"copied {copied_size}"
                )
            if media.modified_ns is not None:
                os.utime(temporary_name, ns=(media.modified_ns, media.modified_ns))
            os.replace(temporary_name, destination)
            temporary_name = None
            return digest
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _record(
        connection: sqlite3.Connection,
        source_id: str,
        media: MediaObject,
        destination_relative: Path,
        digest: str,
    ) -> None:
        imported_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO media (
                source_id, source_key, relative_path, kind, date_code,
                time_code, subsecond_code, duration_code, event_code,
                pair_key, size_bytes, source_modified_ns, sha256, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, source_key) DO UPDATE SET
                relative_path = excluded.relative_path,
                kind = excluded.kind,
                date_code = excluded.date_code,
                time_code = excluded.time_code,
                subsecond_code = excluded.subsecond_code,
                duration_code = excluded.duration_code,
                event_code = excluded.event_code,
                pair_key = excluded.pair_key,
                size_bytes = excluded.size_bytes,
                source_modified_ns = excluded.source_modified_ns,
                sha256 = excluded.sha256,
                imported_at = excluded.imported_at
            """,
            (
                source_id,
                media.source_key,
                destination_relative.as_posix(),
                media.kind,
                media.date_code,
                media.time_code,
                media.subsecond_code,
                media.duration_code,
                media.event_code,
                media.pair_key,
                media.size_bytes,
                media.modified_ns,
                digest,
                imported_at,
            ),
        )
