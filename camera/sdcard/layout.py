"""Parse the B4's observed FAT32 media layout without guessing semantics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional


DATE_RE = re.compile(r"^[0-9]{6}$")
MEDIA_RE = re.compile(
    r"^(?P<time>[0-9]{6})_"
    r"(?P<subsecond>[0-9]{3})_"
    r"(?P<duration>[0-9]{3})_"
    r"(?P<event>[A-Za-z0-9]+)\."
    r"(?P<extension>mp4|jpg)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedMediaPath:
    """Fields encoded in one observed B4 media path.

    ``duration_code`` and ``event_code`` deliberately retain neutral names:
    their meanings are plausible but not yet proven by controlled examples.
    """

    relative_path: PurePosixPath
    kind: str
    date_code: str
    time_code: str
    subsecond_code: str
    duration_code: str
    event_code: str
    pair_key: str


def parse_media_path(path: PurePosixPath) -> Optional[ParsedMediaPath]:
    """Return parsed metadata for a valid ``video`` or ``snaps`` path."""

    if len(path.parts) != 3:
        return None

    top, date_code, filename = path.parts
    if top == "video":
        expected_extension = "mp4"
        kind = "video"
    elif top == "snaps":
        expected_extension = "jpg"
        kind = "snapshot"
    else:
        return None

    if not DATE_RE.fullmatch(date_code):
        return None

    match = MEDIA_RE.fullmatch(filename)
    if match is None or match.group("extension").lower() != expected_extension:
        return None

    stem = filename.rsplit(".", 1)[0]
    return ParsedMediaPath(
        relative_path=path,
        kind=kind,
        date_code=date_code,
        time_code=match.group("time"),
        subsecond_code=match.group("subsecond"),
        duration_code=match.group("duration"),
        event_code=match.group("event"),
        pair_key=f"{date_code}/{stem}",
    )
