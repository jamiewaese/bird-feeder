"""Replaceable media-source boundary for card and future network access."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, ContextManager, Iterator, Protocol

from .layout import parse_media_path


@dataclass(frozen=True)
class MediaObject:
    """One immutable object advertised by a camera media source."""

    source_key: str
    relative_path: PurePosixPath
    kind: str
    date_code: str
    time_code: str
    subsecond_code: str
    duration_code: str
    event_code: str
    pair_key: str
    size_bytes: int
    modified_ns: int | None


class MediaSource(Protocol):
    """Minimal contract shared by mounted-card and future UBox sources."""

    source_id: str

    def iter_media(self) -> Iterator[MediaObject]:
        """Yield media objects in deterministic order."""

    def open_media(self, media: MediaObject) -> ContextManager[BinaryIO]:
        """Open an advertised object as a binary stream."""


def _validate_source_id(source_id: str) -> str:
    if not source_id or any(
        not (character.isascii() and (character.isalnum() or character in "-_"))
        for character in source_id
    ):
        raise ValueError("source_id must contain only ASCII letters, digits, '-' or '_'")
    return source_id


class FilesystemMediaSource:
    """Read the B4 layout from a mounted card or a read-only card image."""

    def __init__(self, root: Path, source_id: str = "b4-yard") -> None:
        self.root = root.expanduser().resolve()
        self.source_id = _validate_source_id(source_id)
        if not self.root.is_dir():
            raise FileNotFoundError(f"media source root does not exist: {self.root}")

    def iter_media(self) -> Iterator[MediaObject]:
        entries: list[MediaObject] = []
        video_top = "video" if (self.root / "video").is_dir() else "videos"
        for source_top, canonical_top in ((video_top, "video"), ("snaps", "snaps")):
            top_path = self.root / source_top
            if not top_path.is_dir() or top_path.is_symlink():
                continue
            for date_path in sorted(top_path.iterdir(), key=lambda path: path.name):
                if not date_path.is_dir() or date_path.is_symlink():
                    continue
                for path in sorted(date_path.iterdir(), key=lambda item: item.name):
                    if path.is_symlink() or not path.is_file():
                        continue
                    source_relative = PurePosixPath(source_top, date_path.name, path.name)
                    canonical_relative = PurePosixPath(
                        canonical_top, date_path.name, path.name
                    )
                    parsed = parse_media_path(canonical_relative)
                    if parsed is None:
                        continue
                    stat = path.stat()
                    entries.append(
                        MediaObject(
                            source_key=source_relative.as_posix(),
                            relative_path=canonical_relative,
                            kind=parsed.kind,
                            date_code=parsed.date_code,
                            time_code=parsed.time_code,
                            subsecond_code=parsed.subsecond_code,
                            duration_code=parsed.duration_code,
                            event_code=parsed.event_code,
                            pair_key=parsed.pair_key,
                            size_bytes=stat.st_size,
                            modified_ns=stat.st_mtime_ns,
                        )
                    )
        yield from sorted(entries, key=lambda entry: entry.source_key)

    @contextmanager
    def open_media(self, media: MediaObject) -> Iterator[BinaryIO]:
        relative = PurePosixPath(media.source_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("media source key escapes the source root")
        path = self.root.joinpath(*relative.parts)
        resolved = path.resolve(strict=True)
        if self.root not in resolved.parents:
            raise ValueError("media source path escapes the source root")
        if resolved.is_symlink() or not resolved.is_file():
            raise ValueError("media source path is not a regular file")
        with resolved.open("rb") as stream:
            yield stream
