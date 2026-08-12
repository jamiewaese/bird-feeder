"""Small LAN gallery for media imported from the B4 camera card."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from camera.mobile import MobileVideoError, MobileVideoPreparer

from camera.classification import ensure_classification_schema
from camera.sdcard.importer import DELETED_PAIRS_SCHEMA


GALLERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stars (
    source_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    starred_at TEXT NOT NULL,
    star_count INTEGER NOT NULL DEFAULT 1 CHECK (star_count >= 0),
    PRIMARY KEY (source_id, pair_key)
);
CREATE TABLE IF NOT EXISTS classification_overrides (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    common_name TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    certainty TEXT NOT NULL CHECK (certainty IN ('certain', 'likely', 'uncertain')),
    notes TEXT NOT NULL,
    sex TEXT NOT NULL CHECK (sex IN ('male', 'female', 'indeterminate')),
    sex_evidence TEXT NOT NULL,
    interesting_fact TEXT NOT NULL,
    reason TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);
""" + DELETED_PAIRS_SCHEMA


AMAZON_BIRD_FEEDER_URL = (
    "https://www.amazon.ca/s?k=smart+bird+feeder+with+camera"
)
GITHUB_REPOSITORY_URL = "https://github.com/jamiewaese/bird-feeder"


@dataclass(frozen=True)
class GalleryPair:
    source_id: str
    pair_key: str
    date_code: str
    time_code: str
    snapshot_path: str | None = None
    video_path: str | None = None
    star_count: int = 0
    is_bird: bool | None = None
    common_name: str | None = None
    scientific_name: str | None = None
    certainty: str | None = None
    classification_notes: str | None = None
    sex: str | None = None
    age_class: str | None = None
    bird_count: int | None = None
    behavior: str | None = None
    sex_evidence: str | None = None
    age_evidence: str | None = None
    interesting_fact: str | None = None

    @property
    def starred(self) -> bool:
        """Retain the convenient boolean view used by older API consumers."""
        return self.star_count > 0

    @property
    def captured_datetime(self) -> datetime | None:
        try:
            return datetime.strptime(self.date_code + self.time_code, "%y%m%d%H%M%S")
        except ValueError:
            return None

    @property
    def date_label(self) -> str:
        value = self.captured_datetime
        return value.strftime("%B %-d, %Y") if value else self.date_code

    @property
    def time_label(self) -> str:
        value = self.captured_datetime
        return value.strftime("%-I:%M %p") if value else self.time_code

    @property
    def captured_label(self) -> str:
        value = self.captured_datetime
        if value is None:
            return f"{self.date_code} {self.time_code}"
        return value.strftime("%B %-d, %Y at %-I:%M:%S %p")

    @property
    def date_value(self) -> str:
        value = self.captured_datetime
        return value.strftime("%Y-%m-%d") if value else ""

    @property
    def sort_value(self) -> str:
        value = self.captured_datetime
        return value.strftime("%Y%m%d%H%M%S") if value else "0"


@dataclass(frozen=True)
class SpeciesSummary:
    key: str
    common_name: str
    scientific_name: str | None
    video_count: int
    male_video_count: int
    female_video_count: int
    unknown_sex_video_count: int
    thumbnail_path: str | None


@dataclass(frozen=True)
class AnimalSummary:
    key: str
    common_name: str
    scientific_name: str | None
    video_count: int
    male_video_count: int
    female_video_count: int
    unknown_sex_video_count: int
    thumbnail_path: str | None


_EMPTY_CLASSIFICATION_NAMES = {
    "empty frame",
    "no animal detected",
    "no animal or bird detected",
    "no bird detected",
}


def _is_animal_visitor(pair: GalleryPair) -> bool:
    """Distinguish a named non-bird visitor from an empty feeder frame."""
    return bool(
        pair.is_bird is False
        and pair.common_name
        and pair.common_name.strip().casefold() not in _EMPTY_CLASSIFICATION_NAMES
    )


def _species_key(pair: GalleryPair) -> str:
    """Return a stable client-side filter key for an identified bird."""
    if pair.is_bird is not True or not pair.common_name:
        return ""
    identity = pair.scientific_name or pair.common_name
    return identity.strip().casefold()


def _animal_key(pair: GalleryPair) -> str:
    """Return a stable client-side filter key for a named animal visitor."""
    if not _is_animal_visitor(pair):
        return ""
    identity = pair.scientific_name or pair.common_name
    return identity.strip().casefold() if identity else ""


def _species_summaries(pairs: list[GalleryPair]) -> list[SpeciesSummary]:
    """Summarize playable bird captures, most frequent species first."""
    summaries: dict[str, dict[str, object]] = {}
    for pair in pairs:
        key = _species_key(pair)
        if not key or pair.video_path is None:
            continue
        summary = summaries.setdefault(
            key,
            {
                "key": key,
                "common_name": pair.common_name,
                "scientific_name": pair.scientific_name,
                "video_count": 0,
                "male_video_count": 0,
                "female_video_count": 0,
                "unknown_sex_video_count": 0,
                "thumbnail_path": None,
            },
        )
        summary["video_count"] = int(summary["video_count"]) + 1
        sex_count_key = {
            "male": "male_video_count",
            "female": "female_video_count",
        }.get(pair.sex or "", "unknown_sex_video_count")
        summary[sex_count_key] = int(summary[sex_count_key]) + 1
        if summary["thumbnail_path"] is None and pair.snapshot_path:
            summary["thumbnail_path"] = pair.snapshot_path

    result = [SpeciesSummary(**summary) for summary in summaries.values()]
    return sorted(result, key=lambda item: (-item.video_count, item.common_name.casefold()))


def _animal_summaries(pairs: list[GalleryPair]) -> list[AnimalSummary]:
    """Summarize playable non-bird animal captures by identified visitor."""
    summaries: dict[str, dict[str, object]] = {}
    for pair in pairs:
        key = _animal_key(pair)
        if not key or pair.video_path is None:
            continue
        summary = summaries.setdefault(
            key,
            {
                "key": key,
                "common_name": pair.common_name,
                "scientific_name": pair.scientific_name,
                "video_count": 0,
                "male_video_count": 0,
                "female_video_count": 0,
                "unknown_sex_video_count": 0,
                "thumbnail_path": None,
            },
        )
        summary["video_count"] = int(summary["video_count"]) + 1
        sex_count_key = {
            "male": "male_video_count",
            "female": "female_video_count",
        }.get(pair.sex or "", "unknown_sex_video_count")
        summary[sex_count_key] = int(summary[sex_count_key]) + 1
        if summary["thumbnail_path"] is None and pair.snapshot_path:
            summary["thumbnail_path"] = pair.snapshot_path

    result = [AnimalSummary(**summary) for summary in summaries.values()]
    return sorted(result, key=lambda item: (-item.video_count, item.common_name.casefold()))


def _sex_breakdown_markup(male: int, female: int, unknown: int) -> str:
    """Format sex counts as a compact color key for the activity chart."""
    parts = []
    for sex, count in (("male", male), ("female", female), ("unknown", unknown)):
        if count:
            parts.append(
                f'<button class="sex-key sex-key-{sex}" type="button" '
                f'data-sex-filter="{sex}" aria-pressed="false">{count} {sex}</button>'
            )
    return '<span class="sex-separator" aria-hidden="true"> · </span>'.join(parts)


def _catalog_path(library_root: Path) -> Path:
    return library_root / "catalog.sqlite3"


def _connect_catalog(library_root: Path, *, read_only: bool = True) -> sqlite3.Connection:
    catalog = _catalog_path(library_root)
    if not catalog.is_file():
        raise FileNotFoundError(catalog)
    if read_only:
        connection = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True, timeout=5)
    else:
        connection = sqlite3.connect(catalog, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _ensure_gallery_schema(library_root: Path) -> None:
    if not _catalog_path(library_root).is_file():
        return
    connection = _connect_catalog(library_root, read_only=False)
    try:
        connection.executescript(GALLERY_SCHEMA)
        star_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(stars)")
        }
        if "star_count" not in star_columns:
            # Existing stars were boolean rows, so each one becomes one star.
            connection.execute(
                "ALTER TABLE stars ADD COLUMN star_count INTEGER NOT NULL DEFAULT 1"
            )
        ensure_classification_schema(connection)
        connection.commit()
    finally:
        connection.close()


def list_pairs(
    library_root: Path,
    *,
    limit: int = 1000,
    ensure_schema: bool = True,
) -> list[GalleryPair]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    if ensure_schema:
        _ensure_gallery_schema(library_root)
    try:
        connection = _connect_catalog(library_root)
    except FileNotFoundError:
        return []

    try:
        rows = connection.execute(
            """
            SELECT
                media.source_id,
                media.pair_key,
                media.date_code,
                media.time_code,
                media.kind,
                media.relative_path,
                classifications.is_bird,
                COALESCE(classification_overrides.common_name, classifications.common_name)
                    AS common_name,
                COALESCE(classification_overrides.scientific_name, classifications.scientific_name)
                    AS scientific_name,
                COALESCE(classification_overrides.certainty, classifications.certainty)
                    AS certainty,
                COALESCE(classification_overrides.notes, classifications.notes)
                    AS classification_notes,
                COALESCE(classification_overrides.sex, classifications.sex) AS sex,
                classifications.age_class,
                classifications.bird_count,
                classifications.behavior,
                COALESCE(classification_overrides.sex_evidence, classifications.sex_evidence)
                    AS sex_evidence,
                classifications.age_evidence,
                COALESCE(classification_overrides.interesting_fact, classifications.interesting_fact)
                    AS interesting_fact,
                COALESCE(stars.star_count, 0) AS star_count
            FROM media
            LEFT JOIN stars
             ON stars.source_id = media.source_id
             AND stars.pair_key = media.pair_key
            LEFT JOIN classifications
              ON classifications.media_id = media.id
            LEFT JOIN classification_overrides
              ON classification_overrides.media_id = media.id
            ORDER BY
                media.date_code DESC,
                media.time_code DESC,
                media.subsecond_code DESC
            """
        ).fetchall()
    finally:
        connection.close()

    pairs: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (row["source_id"], row["pair_key"])
        pair = pairs.setdefault(
            key,
            {
                "source_id": row["source_id"],
                "pair_key": row["pair_key"],
                "date_code": row["date_code"],
                "time_code": row["time_code"],
                "snapshot_path": None,
                "video_path": None,
                "star_count": int(row["star_count"]),
                "is_bird": None,
                "common_name": None,
                "scientific_name": None,
                "certainty": None,
                "classification_notes": None,
                "sex": None,
                "age_class": None,
                "bird_count": None,
                "behavior": None,
                "sex_evidence": None,
                "age_evidence": None,
                "interesting_fact": None,
            },
        )
        field = "video_path" if row["kind"] == "video" else "snapshot_path"
        pair[field] = row["relative_path"]
        if row["kind"] == "snapshot" and row["common_name"] is not None:
            pair["is_bird"] = bool(row["is_bird"])
            pair["common_name"] = row["common_name"]
            pair["scientific_name"] = row["scientific_name"]
            pair["certainty"] = row["certainty"]
            pair["classification_notes"] = row["classification_notes"]
            pair["sex"] = row["sex"]
            pair["age_class"] = row["age_class"]
            pair["bird_count"] = row["bird_count"]
            pair["behavior"] = row["behavior"]
            pair["sex_evidence"] = row["sex_evidence"]
            pair["age_evidence"] = row["age_evidence"]
            pair["interesting_fact"] = row["interesting_fact"]

    return [GalleryPair(**values) for values in list(pairs.values())[:limit]]


def _find_video_pair(
    library_root: Path,
    video_path: str,
    *,
    ensure_schema: bool = True,
) -> GalleryPair | None:
    for pair in list_pairs(library_root, limit=1000, ensure_schema=ensure_schema):
        if pair.video_path == video_path:
            return pair
    return None


def _video_neighbors(
    library_root: Path,
    video_path: str,
    *,
    ensure_schema: bool = True,
) -> tuple[GalleryPair | None, GalleryPair | None]:
    """Return the previous and next playable captures in gallery order."""
    video_pairs = [
        pair
        for pair in list_pairs(
            library_root,
            limit=1000,
            ensure_schema=ensure_schema,
        )
        if pair.video_path
    ]
    for index, pair in enumerate(video_pairs):
        if pair.video_path == video_path:
            previous_pair = video_pairs[index - 1] if index > 0 else None
            next_pair = video_pairs[index + 1] if index + 1 < len(video_pairs) else None
            return previous_pair, next_pair
    return None, None


def _set_star_contribution(
    library_root: Path,
    source_id: str,
    pair_key: str,
    starred: bool,
) -> int:
    """Atomically add or remove one browser's star and return the global total."""
    _ensure_gallery_schema(library_root)
    connection = _connect_catalog(library_root, read_only=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            """
            SELECT 1 FROM media
            WHERE source_id = ? AND pair_key = ? AND kind = 'video'
            """,
            (source_id, pair_key),
        ).fetchone()
        if exists is None:
            raise KeyError("video pair not found")
        timestamp = datetime.now(timezone.utc).isoformat()
        if starred:
            connection.execute(
                """
                INSERT INTO stars (source_id, pair_key, starred_at, star_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT (source_id, pair_key) DO UPDATE SET
                    starred_at = excluded.starred_at,
                    star_count = stars.star_count + 1
                """,
                (source_id, pair_key, timestamp),
            )
        else:
            connection.execute(
                """
                UPDATE stars
                SET star_count = MAX(star_count - 1, 0), starred_at = ?
                WHERE source_id = ? AND pair_key = ?
                """,
                (timestamp, source_id, pair_key),
            )
        row = connection.execute(
            "SELECT star_count FROM stars WHERE source_id = ? AND pair_key = ?",
            (source_id, pair_key),
        ).fetchone()
        star_count = int(row["star_count"]) if row is not None else 0
        if star_count == 0:
            connection.execute(
                "DELETE FROM stars WHERE source_id = ? AND pair_key = ?",
                (source_id, pair_key),
            )
        connection.commit()
        return star_count
    finally:
        connection.close()


def _delete_pair(library_root: Path, source_id: str, pair_key: str) -> dict[str, int]:
    """Permanently remove one catalogued capture pair and its local files."""
    _ensure_gallery_schema(library_root)
    media_root = library_root / "media"
    connection = _connect_catalog(library_root, read_only=False)
    staged: list[tuple[Path, Path]] = []
    trash_root = library_root / ".deleting" / uuid4().hex
    deleted_bytes = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT id, relative_path, size_bytes
            FROM media
            WHERE source_id = ? AND pair_key = ?
            """,
            (source_id, pair_key),
        ).fetchall()
        if not rows:
            raise KeyError("capture pair not found")

        for index, row in enumerate(rows):
            relative = PurePosixPath(row["relative_path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid catalogued media path")
            candidate = media_root.joinpath(*relative.parts)
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError:
                continue
            if media_root not in resolved.parents or not resolved.is_file():
                raise ValueError("catalogued media escaped the library")
            trash_root.mkdir(parents=True, exist_ok=True)
            staged_path = trash_root / f"{index}-{resolved.name}"
            resolved.replace(staged_path)
            staged.append((staged_path, resolved))
            deleted_bytes += int(row["size_bytes"])

        media_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in media_ids)
        connection.execute(
            f"DELETE FROM classification_attempts WHERE media_id IN ({placeholders})",
            media_ids,
        )
        connection.execute(
            f"DELETE FROM classifications WHERE media_id IN ({placeholders})",
            media_ids,
        )
        connection.execute(
            "DELETE FROM stars WHERE source_id = ? AND pair_key = ?",
            (source_id, pair_key),
        )
        connection.execute(
            """
            INSERT INTO deleted_pairs (source_id, pair_key, deleted_at)
            VALUES (?, ?, ?)
            ON CONFLICT (source_id, pair_key) DO UPDATE SET
                deleted_at = excluded.deleted_at
            """,
            (source_id, pair_key, datetime.now(timezone.utc).isoformat()),
        )
        connection.execute(
            "DELETE FROM media WHERE source_id = ? AND pair_key = ?",
            (source_id, pair_key),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        for staged_path, original_path in reversed(staged):
            original_path.parent.mkdir(parents=True, exist_ok=True)
            staged_path.replace(original_path)
        raise
    finally:
        connection.close()

    for staged_path, _ in staged:
        staged_path.unlink(missing_ok=True)
    for cache_directory in ("mobile-v1", "mobile-v2"):
        mobile_root = library_root / cache_directory
        for row in rows:
            relative = PurePosixPath(row["relative_path"])
            if relative.suffix.casefold() != ".mp4":
                continue
            cached = mobile_root.joinpath(*relative.parts)
            try:
                resolved_cached = cached.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if mobile_root in resolved_cached.parents and resolved_cached.is_file():
                resolved_cached.unlink(missing_ok=True)
    if trash_root.exists():
        trash_root.rmdir()
    deleting_root = trash_root.parent
    if deleting_root.exists() and not any(deleting_root.iterdir()):
        deleting_root.rmdir()
    return {"files": len(rows), "bytes": deleted_bytes}


def _media_url(relative_path: str) -> str:
    return "/media/" + quote(relative_path, safe="/")


def _watch_url(relative_path: str) -> str:
    return "/watch/" + quote(relative_path, safe="/")


def _download_url(relative_path: str) -> str:
    return "/download/" + quote(relative_path, safe="/")


def _share_url(relative_path: str) -> str:
    return "/share/" + quote(relative_path, safe="/")


def _filename_part(value: str, *, fallback: str) -> str:
    """Return a readable, cross-platform-safe shared-file name component."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-")
    return normalized or fallback


def _share_filename(pair: GalleryPair, *, location: str) -> str:
    species = _filename_part(pair.common_name or "Bird", fallback="Bird")
    place = _filename_part(location, fallback="Toronto")
    captured = pair.captured_datetime
    if captured is None:
        date_part = _filename_part(pair.date_code, fallback="Unknown-date")
        time_part = _filename_part(pair.time_code, fallback="Unknown-time")
    else:
        date_part = captured.strftime("%Y-%m-%d")
        time_part = captured.strftime("%H-%M-%S")
    return f"{species}_{place}_{date_part}_{time_part}.mp4"


def _share_button(
    pair: GalleryPair,
    *,
    location: str,
    secondary: bool = False,
    preload: bool = False,
) -> str:
    if pair.video_path is None:
        return ""
    filename = _share_filename(pair, location=location)
    action_class = "secondary-action" if secondary else "primary-action"
    preload_attribute = " data-preload-share" if preload else ""
    return (
        f'<button type="button" class="{action_class} share-button" '
        f'data-share-video{preload_attribute} '
        f'data-video-url="{html.escape(_share_url(pair.video_path), quote=True)}" '
        f'data-video-name="{html.escape(filename, quote=True)}" '
        'aria-label="Share video">'
        '<span aria-hidden="true">↗</span> <span data-share-label>Share</span>'
        '</button>'
    )


def _star_button(pair: GalleryPair, *, large: bool = False) -> str:
    if pair.video_path is None:
        return ""
    label = f"☆ Star · {pair.star_count}"
    classes = "star-button star-button-large" if large else "star-button star-button-card"
    return (
        f'<button type="button" class="{classes}" data-star-button '
        f'data-source-id="{html.escape(pair.source_id, quote=True)}" '
        f'data-pair-key="{html.escape(pair.pair_key, quote=True)}" '
        f'data-star-count="{pair.star_count}" '
        'data-locally-starred="false" aria-pressed="false" '
        f'aria-label="Add a star; {pair.star_count} so far">{label}</button>'
    )


def _delete_button(pair: GalleryPair, *, large: bool = False) -> str:
    classes = "delete-button delete-button-large" if large else "delete-button"
    label = '<span class="delete-label">Delete capture</span>' if large else ""
    return (
        f'<button type="button" class="{classes}" data-delete-button '
        f'data-source-id="{html.escape(pair.source_id, quote=True)}" '
        f'data-pair-key="{html.escape(pair.pair_key, quote=True)}" '
        f'aria-label="Delete capture" title="Delete capture">'
        '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 20 20" '
        'fill="none" stroke="currentColor" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M3 5.5h14M7.5 5.5V3.5h5v2M15 5.5l-.8 11H5.8L5 5.5M8 9v4.5M12 9v4.5"/>'
        f"</svg>{label}</button>"
    )


def _classification_label(
    pair: GalleryPair,
    *,
    detailed: bool = True,
    include_heading: bool = True,
) -> str:
    if pair.common_name is None:
        return ""
    common_name = html.escape(pair.common_name)
    scientific_name = (
        f'<p class="scientific-name"><i>{html.escape(pair.scientific_name)}</i></p>'
        if pair.scientific_name
        else ""
    )
    detail_html = ""
    if pair.is_bird:
        count = str(pair.bird_count) if pair.bird_count is not None else "—"
        sex = {
            "male": "Male",
            "female": "Female",
            "indeterminate": "Unknown",
        }.get(pair.sex or "", "—")
        age = {
            "adult": "Adult",
            "juvenile": "Juvenile",
            "immature": "Immature",
            "indeterminate": "Unknown",
        }.get(pair.age_class or "", "—")
        behavior_html = (
            '<div class="observation"><span>Observed</span>'
            f"<p>{html.escape(pair.behavior)}</p></div>"
            if detailed and pair.behavior
            else ""
        )
        note_sections = ""
        fact_html = (
            '<aside class="classification-fact"><span>Species note</span>'
            f"<p>{html.escape(pair.interesting_fact)}</p></aside>"
            if detailed and pair.interesting_fact
            else ""
        )
        if behavior_html or fact_html:
            note_sections = (
                '<div class="classification-notes">'
                f"{fact_html}{behavior_html}"
                "</div>"
            )
        detail_html = (
            '<dl class="classification-stats">'
            f"<div><dt>Sex</dt><dd>{html.escape(sex)}</dd></div>"
            f"<div><dt>Age</dt><dd>{html.escape(age)}</dd></div>"
            f"<div><dt>Count</dt><dd>{html.escape(count)}</dd></div>"
            "</dl>"
            f"{note_sections}"
        )
    elif _is_animal_visitor(pair):
        sex = {
            "male": "Male",
            "female": "Female",
            "indeterminate": "Unknown",
        }.get(pair.sex or "", "—")
        sex_html = (
            '<dl class="classification-stats classification-stats-animal">'
            f"<div><dt>Sex</dt><dd>{html.escape(sex)}</dd></div>"
            "</dl>"
        )
        behavior_html = (
            '<div class="observation"><span>Observed</span>'
            f"<p>{html.escape(pair.behavior)}</p></div>"
            if detailed and pair.behavior
            else ""
        )
        fact_html = (
            '<aside class="classification-fact"><span>Visitor note</span>'
            f"<p>{html.escape(pair.interesting_fact)}</p></aside>"
            if detailed and pair.interesting_fact
            else ""
        )
        if behavior_html or fact_html:
            detail_html = (
                f"{sex_html}"
                '<div class="classification-notes">'
                f"{fact_html}{behavior_html}"
                "</div>"
            )
        else:
            detail_html = sex_html
    elif pair.classification_notes or pair.behavior:
        description = pair.classification_notes or pair.behavior or ""
        detail_html = f'<p class="empty-frame-note">{html.escape(description)}</p>'

    heading_html = (
        '<div class="species-heading-row">'
        f'<h2 class="species-name">{common_name}</h2>'
        "</div>"
        f"{scientific_name}"
        if include_heading
        else ""
    )
    return (
        '<div class="classification">'
        f"{heading_html}{detail_html}"
        "</div>"
    )


def _shared_script(csrf_token: str, *, deletes_enabled: bool = True) -> str:
    script = """
<script>
  const csrfToken = "__CSRF_TOKEN__";
  let activeSpecies = "";
  let activeSex = "";
  const archivePageSize = 24;
  let archivePage = 1;
  const preparedShareFiles = new Map();
  const shareFilePromises = new Map();
  const starStorageKey = "bird-feeder-starred-v1";
  const loadLocalStars = () => {
    try {
      const stored = JSON.parse(window.localStorage.getItem(starStorageKey) || "[]");
      return new Set(Array.isArray(stored) ? stored.filter((value) => typeof value === "string") : []);
    } catch (error) {
      return new Set();
    }
  };
  let locallyStarred = loadLocalStars();
  const starIdentity = (button) => `${button.dataset.sourceId}|${button.dataset.pairKey}`;
  const saveLocalStars = () => {
    try {
      window.localStorage.setItem(starStorageKey, JSON.stringify(Array.from(locallyStarred)));
    } catch (error) {
      // Keep the in-memory state for this page when browser storage is unavailable.
    }
  };
  const renderStarButton = (button) => {
    const starCount = Number(button.dataset.starCount);
    const starred = locallyStarred.has(starIdentity(button));
    button.dataset.locallyStarred = String(starred);
    button.setAttribute("aria-pressed", String(starred));
    button.setAttribute(
      "aria-label",
      starred ? `Remove your star; ${starCount} total` : `Add a star; ${starCount} so far`
    );
    button.textContent = starred ? `★ Starred · ${starCount}` : `☆ Star · ${starCount}`;
  };
  const refreshStarButtons = () => {
    document.querySelectorAll("[data-star-button]").forEach(renderStarButton);
  };

  const syncDateControlStates = () => {
    document.querySelectorAll("[data-date-control]").forEach((control) => {
      const input = control.querySelector('input[type="date"]');
      control.dataset.hasValue = String(Boolean(input?.value));
    });
  };

  const prepareShareFile = (button) => {
    const videoUrl = button.dataset.videoUrl;
    const filename = button.dataset.videoName || "bird-feeder-video.mp4";
    if (preparedShareFiles.has(videoUrl)) {
      return Promise.resolve(preparedShareFiles.get(videoUrl));
    }
    if (shareFilePromises.has(videoUrl)) return shareFilePromises.get(videoUrl);

    const preparation = fetch(videoUrl)
      .then((response) => {
        if (!response.ok) throw new Error("Could not load video");
        return response.blob();
      })
      .then((blob) => {
        // Declare an exact media type. Messages can otherwise treat a browser-
        // constructed attachment as a generic document instead of an inline video.
        const file = new File([blob], filename, { type: "video/mp4" });
        preparedShareFiles.set(videoUrl, file);
        return file;
      })
      .finally(() => shareFilePromises.delete(videoUrl));
    shareFilePromises.set(videoUrl, preparation);
    return preparation;
  };

  const shareVideo = async (button) => {
    const label = button.querySelector("[data-share-label]");
    const videoUrl = button.dataset.videoUrl;
    let file = preparedShareFiles.get(videoUrl);

    try {
      if (!file) {
        button.disabled = true;
        if (label) label.textContent = "Preparing…";
        file = await prepareShareFile(button);
        button.disabled = false;
        if (label) label.textContent = "Ready — tap Share";
        return;
      }

      // Share only the video. Supplying title/text makes Messages create a
      // separate text bubble and can push the MP4 through its document flow.
      const shareData = { files: [file] };
      if (navigator.share &&
          (!navigator.canShare || navigator.canShare(shareData))) {
        if (label) label.textContent = "Share";
        try {
          await navigator.share(shareData);
        } catch (error) {
          if (error.name === "AbortError") return;
          throw error;
        }
      } else {
        throw new Error("File sharing is not supported by this browser");
      }
    } catch (error) {
      preparedShareFiles.delete(videoUrl);
      button.disabled = false;
      if (label) label.textContent = "Share";
      window.alert("Sharing did not open. Try again in Safari on your iPhone or iPad.");
    }
  };

  const svgNamespace = "http://www.w3.org/2000/svg";
  const activitySexStyles = {
    male: { label: "Male", color: "#4f739d", dash: "" },
    female: { label: "Female", color: "#bd6b87", dash: "5 3" },
    unknown: { label: "Unknown sex", color: "#858177", dash: "2 3" }
  };
  const animalActivityStyles = {
    male: activitySexStyles.male,
    female: activitySexStyles.female,
    unknown: { label: "Unknown sex", color: "#8b603b", dash: "" }
  };

  const hourlyTimeLabel = (hour) => {
    const formatHour = (value) => {
      if (value === 0) return "12 AM";
      if (value === 12) return "12 PM";
      return `${value > 12 ? value - 12 : value} ${value >= 12 ? "PM" : "AM"}`;
    };
    return `${formatHour(hour)}–${formatHour((hour + 1) % 24)}`;
  };

  const createSvgElement = (name, attributes = {}) => {
    const element = document.createElementNS(svgNamespace, name);
    Object.entries(attributes).forEach(([attribute, value]) => {
      element.setAttribute(attribute, String(value));
    });
    return element;
  };

  let hourlyActivityCards = [];
  const renderHourlyActivity = (cards) => {
    hourlyActivityCards = cards;
    const species = new Map();
    cards.forEach((card) => {
      if (card.dataset.hasVideo !== "true") return;
      const isBird = card.dataset.isBird === "true";
      if (!isBird && card.dataset.isAnimal !== "true") return;
      const key = card.dataset.species;
      if (!key) return;
      const item = species.get(key) || {
        key,
        name: card.dataset.speciesLabel || key,
        isBird,
        monthlyVideos: Array(12).fill(0),
        hourlyBySex: {
          male: Array(24).fill(0),
          female: Array(24).fill(0),
          unknown: Array(24).fill(0)
        }
      };
      species.set(key, item);
      const month = Number(card.dataset.date.slice(5, 7)) - 1;
      if (Number.isInteger(month) && month >= 0 && month < 12) {
        item.monthlyVideos[month] += 1;
      }
      const hour = Number(card.dataset.timestamp.slice(8, 10));
      if (!Number.isInteger(hour) || hour < 0 || hour > 23) return;
      const sightingCount = isBird
        ? Math.max(1, Number(card.dataset.birdCount) || 1)
        : 1;
      const sex = card.dataset.sex === "male" || card.dataset.sex === "female"
        ? card.dataset.sex
        : "unknown";
      item.hourlyBySex[sex][hour] += sightingCount;
    });

    document.querySelectorAll("[data-hourly-activity]").forEach((chart, chartIndex) => {
      const item = species.get(chart.dataset.chartSpecies);
      const svg = chart.querySelector("[data-hourly-svg]");
      const detail = chart.querySelector("[data-hourly-detail]");
      const tooltip = chart.querySelector("[data-hourly-tooltip]");
      const plot = chart.querySelector(".activity-plot");
      chart.hidden = !item;
      if (!item || !svg || !detail || !tooltip || !plot) return;

      svg.replaceChildren();

      const title = createSvgElement("title");
      title.textContent = item.isBird
        ? `${item.name} sightings by hour, sex, and month`
        : `${item.name} sightings by hour, sex, and month`;
      const description = createSvgElement("desc");
      const monthNames = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
      ];
      const seriesDescription = item.isBird
        ? "Blue solid lines indicate males, pink dashed lines indicate females, and gray dotted lines indicate unknown sex. "
        : "Blue solid lines indicate males, pink dashed lines indicate females, and the brown solid line indicates unknown-sex sightings. ";
      description.textContent = seriesDescription + "Shaded areas indicate nighttime hours; moon and sun icons label nighttime and daytime. Monthly video counts: " +
        item.monthlyVideos.map((count, month) => `${monthNames[month]}: ${count}`).join(", ") + ".";
      svg.append(title, description);

      const width = Math.max(220, Math.round(plot.clientWidth || chart.clientWidth || 960));
      const height = Math.max(76, Math.round(plot.clientHeight || 0));
      const left = 30;
      const right = 12;
      const top = 9;
      const bottom = 51;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const greatest = Math.max(...Object.values(item.hourlyBySex).flat());
      const yMax = greatest <= 4 ? 4 : greatest <= 8 ? 8 : greatest <= 16 ? 16 : Math.ceil(greatest / 5) * 5;
      const xPosition = (hour) => left + (hour / 23) * plotWidth;
      const yPosition = (value) => top + plotHeight - (value / yMax) * plotHeight;
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

      const nightTransitionHours = 1.5;
      const dawnGradientId = `activity-night-dawn-${chartIndex}`;
      const duskGradientId = `activity-night-dusk-${chartIndex}`;
      const nightDefs = createSvgElement("defs");
      const dawnGradient = createSvgElement("linearGradient", {
        id: dawnGradientId,
        gradientUnits: "userSpaceOnUse",
        x1: xPosition(6 - nightTransitionHours),
        x2: xPosition(6),
        y1: 0,
        y2: 0
      });
      dawnGradient.append(
        createSvgElement("stop", {
          offset: "0%", "stop-color": "#4c586e", "stop-opacity": "0.09"
        }),
        createSvgElement("stop", {
          offset: "100%", "stop-color": "#4c586e", "stop-opacity": "0"
        })
      );
      const duskGradient = createSvgElement("linearGradient", {
        id: duskGradientId,
        gradientUnits: "userSpaceOnUse",
        x1: xPosition(20),
        x2: xPosition(20 + nightTransitionHours),
        y1: 0,
        y2: 0
      });
      duskGradient.append(
        createSvgElement("stop", {
          offset: "0%", "stop-color": "#4c586e", "stop-opacity": "0"
        }),
        createSvgElement("stop", {
          offset: "100%", "stop-color": "#4c586e", "stop-opacity": "0.09"
        })
      );
      nightDefs.append(dawnGradient, duskGradient);
      svg.append(nightDefs);

      const night = createSvgElement("g", { class: "activity-night", "aria-hidden": "true" });
      night.append(
        createSvgElement("rect", {
          x: left,
          y: top,
          width: xPosition(6) - left,
          height: plotHeight,
          fill: `url(#${dawnGradientId})`
        }),
        createSvgElement("rect", {
          x: xPosition(20),
          y: top,
          width: width - right - xPosition(20),
          height: plotHeight,
          fill: `url(#${duskGradientId})`
        })
      );
      svg.append(night);

      const moonStarsPath = "M240,96a8,8,0,0,1-8,8H216v16a8,8,0,0,1-16,0V104H184a8,8,0,0,1,0-16h16V72a8,8,0,0,1,16,0V88h16A8,8,0,0,1,240,96ZM144,56h8v8a8,8,0,0,0,16,0V56h8a8,8,0,0,0,0-16h-8V32a8,8,0,0,0-16,0v8h-8a8,8,0,0,0,0,16Zm72.77,97a8,8,0,0,1,1.43,8A96,96,0,1,1,95.07,37.8a8,8,0,0,1,10.6,9.06A88.07,88.07,0,0,0,209.14,150.33,8,8,0,0,1,216.77,153Zm-19.39,14.88c-1.79.09-3.59.14-5.38.14A104.11,104.11,0,0,1,88,64c0-1.79,0-3.59.14-5.38A80,80,0,1,0,197.38,167.86Z";
      const sunPath = "M120,40V16a8,8,0,0,1,16,0V40a8,8,0,0,1-16,0Zm72,88a64,64,0,1,1-64-64A64.07,64.07,0,0,1,192,128Zm-16,0a48,48,0,1,0-48,48A48.05,48.05,0,0,0,176,128ZM58.34,69.66A8,8,0,0,0,69.66,58.34l-16-16A8,8,0,0,0,42.34,53.66Zm0,116.68-16,16a8,8,0,0,0,11.32,11.32l16-16a8,8,0,0,0-11.32-11.32ZM192,72a8,8,0,0,0,5.66-2.34l16-16a8,8,0,0,0-11.32-11.32l-16,16A8,8,0,0,0,192,72Zm5.66,114.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32-11.32ZM48,128a8,8,0,0,0-8-8H16a8,8,0,0,0,0,16H40A8,8,0,0,0,48,128Zm80,80a8,8,0,0,0-8,8v24a8,8,0,0,0,16,0V216A8,8,0,0,0,128,208Zm112-88H216a8,8,0,0,0,0,16h24a8,8,0,0,0,0-16Z";
      const daypartIcons = createSvgElement("g", {
        class: "activity-daypart-icons", "aria-hidden": "true"
      });
      const moonStars = createSvgElement("g", {
        class: "activity-daypart-icon",
        transform: `translate(${left + 6} ${top + 5}) scale(0.078125)`
      });
      moonStars.append(createSvgElement("path", { d: moonStarsPath }));
      const eveningMoonStars = createSvgElement("g", {
        class: "activity-daypart-icon",
        transform: `translate(${xPosition(20) + 6} ${top + 5}) scale(0.078125)`
      });
      eveningMoonStars.append(createSvgElement("path", { d: moonStarsPath }));
      const sun = createSvgElement("g", {
        class: "activity-daypart-icon",
        transform: `translate(${xPosition(6) + 8} ${top + 5}) scale(0.078125)`
      });
      sun.append(createSvgElement("path", { d: sunPath }));
      daypartIcons.append(moonStars, eveningMoonStars, sun);
      svg.append(daypartIcons);

      const grid = createSvgElement("g", { class: "activity-grid", "aria-hidden": "true" });
      [0, yMax].forEach((value) => {
        const y = yPosition(value);
        grid.append(createSvgElement("line", { x1: left, y1: y, x2: width - right, y2: y }));
        const label = createSvgElement("text", { x: left - 7, y: y + 4, "text-anchor": "end" });
        label.textContent = String(value);
        grid.append(label);
      });

      const xAxis = createSvgElement("g", { class: "activity-x-axis", "aria-hidden": "true" });
      [[0, "12 AM", "start"], [6, "6 AM", "middle"], [12, "Noon", "middle"], [18, "6 PM", "middle"], [23, "11 PM", "end"]]
        .forEach(([hour, label, anchor]) => {
          const tick = createSvgElement("text", {
            x: xPosition(hour), y: top + plotHeight + 18, "text-anchor": anchor
          });
          tick.textContent = label;
          xAxis.append(tick);
        });
      svg.append(grid, xAxis);

      const abbreviatedMonths = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
      const monthLabels = plotWidth >= 480
        ? abbreviatedMonths
        : plotWidth >= 210
          ? abbreviatedMonths.map((label) => label[0])
          : Array(12).fill("");
      const monthMaximum = Math.max(1, ...item.monthlyVideos);
      const monthWidth = plotWidth / 12;
      const monthRow = createSvgElement("g", { class: "activity-months" });
      item.monthlyVideos.forEach((count, month) => {
        const intensity = count === 0 ? 0.05 : 0.16 + 0.72 * (count / monthMaximum);
        const group = createSvgElement("g", {
          class: "activity-month", "data-month": String(month + 1)
        });
        const monthTitle = createSvgElement("title");
        monthTitle.textContent = `${monthNames[month]}: ${count} ${count === 1 ? "video" : "videos"}`;
        const cell = createSvgElement("rect", {
          class: "activity-month-cell",
          x: (left + month * monthWidth + 1).toFixed(2),
          y: height - 22,
          width: Math.max(1, monthWidth - 2).toFixed(2),
          height: 18,
          rx: 3,
          opacity: intensity.toFixed(3)
        });
        group.append(monthTitle, cell);
        if (monthLabels[month]) {
          const label = createSvgElement("text", {
            class: "activity-month-label",
            x: (left + (month + 0.5) * monthWidth).toFixed(2),
            y: height - 9,
            "text-anchor": "middle",
            "data-on-dark": String(intensity >= 0.55)
          });
          label.textContent = monthLabels[month];
          group.append(label);
        }
        monthRow.append(group);
      });
      svg.append(monthRow);

      const showPoint = (point, sex, hour) => {
        svg.querySelectorAll(".activity-point[data-selected='true']")
          .forEach((candidate) => candidate.removeAttribute("data-selected"));
        point.dataset.selected = "true";
        const sightings = item.hourlyBySex[sex][hour];
        const style = item.isBird ? activitySexStyles[sex] : animalActivityStyles[sex];
        detail.textContent = item.isBird
          ? `${item.name}, ${style.label.toLowerCase()}, ${hourlyTimeLabel(hour)}: ${sightings} ${sightings === 1 ? "bird" : "birds"}`
          : `${item.name}, ${style.label.toLowerCase()}, ${hourlyTimeLabel(hour)}: ${sightings} ${sightings === 1 ? "sighting" : "sightings"}`;
        tooltip.textContent = item.isBird
          ? `${style.label} · ${hourlyTimeLabel(hour)} · ${sightings}`
          : `${style.label} · ${hourlyTimeLabel(hour)} · ${sightings} ${sightings === 1 ? "sighting" : "sightings"}`;
        tooltip.hidden = false;
        tooltip.style.left = `${Math.max(54, Math.min(plot.clientWidth - 54, xPosition(hour)))}px`;
        tooltip.style.top = `${yPosition(sightings)}px`;
      };

      const hidePoint = () => {
        svg.querySelectorAll(".activity-point[data-selected='true']")
          .forEach((candidate) => candidate.removeAttribute("data-selected"));
        tooltip.hidden = true;
        detail.textContent = `${item.name} hourly activity`;
      };

      ["unknown", "female", "male"].forEach((sex) => {
        const hourly = item.hourlyBySex[sex];
        const observedHours = hourly
          .map((value, hour) => value > 0 ? hour : -1)
          .filter((hour) => hour >= 0);
        if (observedHours.length === 0) return;
        const style = item.isBird ? activitySexStyles[sex] : animalActivityStyles[sex];
        const firstHour = Math.max(0, observedHours[0] - 1);
        const lastHour = Math.min(23, observedHours[observedHours.length - 1] + 1);
        const group = createSvgElement("g", {
          class: "activity-series",
          "data-sex": sex,
          style: `--series-color: ${style.color}`
        });
        const pathData = hourly
          .slice(firstHour, lastHour + 1)
          .map((value, index) => {
            const hour = firstHour + index;
            return `${index === 0 ? "M" : "L"}${xPosition(hour).toFixed(2)} ${yPosition(value).toFixed(2)}`;
          })
          .join(" ");
        const path = createSvgElement("path", { class: "activity-line", d: pathData });
        if (style.dash) path.setAttribute("stroke-dasharray", style.dash);
        group.append(path);
        hourly.forEach((value, hour) => {
          if (value === 0) return;
          const point = createSvgElement("circle", {
            class: "activity-point",
            cx: xPosition(hour).toFixed(2),
            cy: yPosition(value).toFixed(2),
            r: value === greatest ? 3.5 : 2.5,
            role: "button",
            tabindex: "0",
            "aria-label": item.isBird
              ? `${item.name}, ${style.label.toLowerCase()}, ${hourlyTimeLabel(hour)}: ${value} ${value === 1 ? "bird" : "birds"}`
              : `${item.name}, ${style.label.toLowerCase()}, ${hourlyTimeLabel(hour)}: ${value} ${value === 1 ? "sighting" : "sightings"}`
          });
          point.addEventListener("mouseenter", () => showPoint(point, sex, hour));
          point.addEventListener("mouseleave", hidePoint);
          point.addEventListener("focus", () => showPoint(point, sex, hour));
          point.addEventListener("blur", hidePoint);
          point.addEventListener("click", () => showPoint(point, sex, hour));
          group.append(point);
        });
        svg.append(group);
      });
      hidePoint();
    });
  };

  let hourlyActivityResizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(hourlyActivityResizeTimer);
    hourlyActivityResizeTimer = window.setTimeout(
      () => renderHourlyActivity(hourlyActivityCards),
      80
    );
  });

  // Prepare the attachment before the user taps. This keeps the eventual
  // navigator.share call inside a fresh tap, which iOS requires reliably.
  document.querySelectorAll("[data-share-video][data-preload-share]").forEach((button) => {
    prepareShareFile(button).catch(() => {});
  });

  const refreshSpecies = (cards) => {
    const speciesButtons = Array.from(document.querySelectorAll("[data-species-filter]"));
    speciesButtons.forEach((button) => {
      const species = button.dataset.speciesFilter;
      const matchingCards = cards.filter(
        (card) => card.dataset.hasVideo === "true" && card.dataset.species === species
      );
      if (matchingCards.length === 0) {
        if (activeSpecies === species) {
          activeSpecies = "";
          activeSex = "";
        }
        const speciesRow = button.closest("[data-species-row]");
        if (speciesRow) speciesRow.remove();
        else button.remove();
        return;
      }
      const count = button.querySelector("[data-species-count]");
      if (count) count.textContent = String(matchingCards.length);
      button.dataset.videoCount = String(matchingCards.length);
      const countLabel = matchingCards.length === 1 ? "video" : "videos";
      const countSuffix = button.querySelector("[data-species-count-label]");
      if (countSuffix) countSuffix.textContent = countLabel;
      const sexCounts = { male: 0, female: 0, unknown: 0 };
      matchingCards.forEach((card) => {
        const sex = card.dataset.sex;
        if (sex === "male" || sex === "female") sexCounts[sex] += 1;
        else sexCounts.unknown += 1;
      });
      const sexParts = [
        { sex: "male", count: sexCounts.male },
        { sex: "female", count: sexCounts.female },
        { sex: "unknown", count: sexCounts.unknown }
      ].filter((part) => part.count > 0);
      const speciesCard = button.closest(".species-card");
      const sexBreakdown = speciesCard?.querySelector("[data-species-sex-breakdown]");
      if (sexBreakdown) {
        sexBreakdown.replaceChildren();
        sexParts.forEach((part, index) => {
          if (index > 0) {
            const separator = document.createElement("span");
            separator.className = "sex-separator";
            separator.setAttribute("aria-hidden", "true");
            separator.textContent = " · ";
            sexBreakdown.append(separator);
          }
          const key = document.createElement("button");
          key.type = "button";
          key.className = `sex-key sex-key-${part.sex}`;
          key.dataset.sexFilter = part.sex;
          key.setAttribute(
            "aria-pressed",
            String(activeSpecies === species && activeSex === part.sex)
          );
          key.textContent = `${part.count} ${part.sex}`;
          sexBreakdown.append(key);
        });
      }
      const thumbnailCard = matchingCards.find((card) => card.dataset.snapshotUrl);
      const thumbnail = button.querySelector("[data-species-thumbnail]");
      const placeholder = button.querySelector("[data-species-placeholder]");
      if (thumbnail && thumbnailCard) {
        thumbnail.src = thumbnailCard.dataset.snapshotUrl;
        thumbnail.hidden = false;
        if (placeholder) placeholder.hidden = true;
      } else if (thumbnail) {
        thumbnail.hidden = true;
        if (placeholder) placeholder.hidden = false;
      }
      const selected = activeSpecies === species;
      button.dataset.active = String(selected);
      button.setAttribute("aria-pressed", String(selected));
      if (speciesCard) speciesCard.dataset.active = String(selected);
    });
    document.querySelectorAll(".summary-grid").forEach((summaryGrid) => {
      Array.from(summaryGrid.querySelectorAll("[data-species-filter]"))
        .sort((left, right) => {
          const countOrder = Number(right.dataset.videoCount) - Number(left.dataset.videoCount);
          return countOrder || left.dataset.speciesName.localeCompare(right.dataset.speciesName);
        })
        .forEach((button) => {
          summaryGrid.append(button.closest("[data-species-row]") || button);
        });
    });
    const activeButton = speciesButtons.find(
      (button) => button.dataset.speciesFilter === activeSpecies
    );
    document.querySelectorAll("[data-clear-species]").forEach((button) => {
      button.hidden = !activeButton || button.dataset.visitorKind !== activeButton.dataset.visitorKind;
    });
    renderHourlyActivity(cards);
  };

  const refreshArchive = ({ resetPage = false } = {}) => {
    const gallery = document.querySelector("[data-gallery]");
    if (!gallery) return;
    const cards = Array.from(gallery.querySelectorAll("[data-card]"));
    const from = document.querySelector("[data-date-from]")?.value || "";
    const to = document.querySelector("[data-date-to]")?.value || "";
    const filter = document.querySelector("[data-filter]")?.value || "birds";
    const sort = document.querySelector("[data-sort]")?.value || "newest";

    cards.sort((left, right) => {
      if (sort === "oldest") return left.dataset.timestamp.localeCompare(right.dataset.timestamp);
      if (sort === "starred") {
        const starOrder = Number(right.dataset.starCount) - Number(left.dataset.starCount);
        if (starOrder) return starOrder;
      }
      return right.dataset.timestamp.localeCompare(left.dataset.timestamp);
    });

    const matchingCards = cards.filter((card) => {
      const dateMatches = (!from || card.dataset.date >= from) && (!to || card.dataset.date <= to);
      const filterMatches =
        filter === "all" ||
        (filter === "starred" && Number(card.dataset.starCount) > 0) ||
        (filter === "identified" && card.dataset.classified === "true") ||
        (filter === "unclassified" && card.dataset.classified !== "true") ||
        (filter === "birds" && card.dataset.isBird === "true") ||
        (filter === "multiple-birds" && Number(card.dataset.birdCount) > 1) ||
        (filter === "animals" && card.dataset.isAnimal === "true") ||
        (filter === "empty" && card.dataset.isBird === "false" && card.dataset.isAnimal !== "true");
      const speciesMatches = !activeSpecies ||
        (card.dataset.hasVideo === "true" && card.dataset.species === activeSpecies);
      const cardSex = card.dataset.sex === "male" || card.dataset.sex === "female"
        ? card.dataset.sex
        : "unknown";
      const sexMatches = !activeSex || cardSex === activeSex;
      return dateMatches && filterMatches && speciesMatches && sexMatches;
    });

    if (resetPage) archivePage = 1;
    const pageCount = Math.max(1, Math.ceil(matchingCards.length / archivePageSize));
    archivePage = Math.min(Math.max(archivePage, 1), pageCount);
    const pageStart = (archivePage - 1) * archivePageSize;
    const pageCards = new Set(matchingCards.slice(pageStart, pageStart + archivePageSize));

    cards.forEach((card) => {
      card.hidden = !pageCards.has(card);
      if (!card.hidden) {
        card.querySelectorAll("img[data-src]").forEach((image) => {
          image.src = image.dataset.src;
          image.removeAttribute("data-src");
        });
      }
      gallery.append(card);
    });

    const visibleCount = document.querySelector("[data-visible-count]");
    if (visibleCount) visibleCount.textContent = String(pageCards.size);
    const noResults = document.querySelector("[data-no-results]");
    if (noResults) noResults.hidden = matchingCards.length !== 0 || cards.length === 0;
    const pagination = document.querySelector("[data-pagination]");
    if (pagination) pagination.hidden = matchingCards.length === 0;
    const pageStatus = document.querySelector("[data-page-status]");
    if (pageStatus) {
      const first = matchingCards.length === 0 ? 0 : pageStart + 1;
      const last = Math.min(pageStart + archivePageSize, matchingCards.length);
      pageStatus.textContent = `Showing ${first}–${last} of ${matchingCards.length}`;
    }
    const pageNumber = document.querySelector("[data-page-number]");
    if (pageNumber) pageNumber.textContent = `Page ${archivePage} of ${pageCount}`;
    const previousPage = document.querySelector("[data-page-previous]");
    if (previousPage) previousPage.disabled = archivePage <= 1;
    const nextPage = document.querySelector("[data-page-next]");
    if (nextPage) nextPage.disabled = archivePage >= pageCount;
    const resultTotal = document.querySelector("[data-result-total]");
    if (resultTotal) resultTotal.textContent = String(cards.length);
    const emptyArchive = document.querySelector("[data-empty-archive]");
    if (emptyArchive) emptyArchive.hidden = cards.length !== 0;
    refreshSpecies(cards);
  };

  document.addEventListener("click", async (event) => {
    const shareButton = event.target.closest("[data-share-video]");
    if (shareButton) {
      await shareVideo(shareButton);
      return;
    }

    const starButton = event.target.closest("[data-star-button]");
    if (starButton) {
      const identity = starIdentity(starButton);
      const starred = !locallyStarred.has(identity);
      starButton.disabled = true;
      try {
        const response = await fetch("/api/stars", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken
          },
          body: JSON.stringify({
            camera_id: starButton.dataset.sourceId,
            pair_key: starButton.dataset.pairKey,
            starred: starred
          })
        });
        if (!response.ok) throw new Error("Could not save star");
        const payload = await response.json();
        const starCount = Number(payload.star_count);
        document.querySelectorAll("[data-star-button]").forEach((candidate) => {
          if (candidate.dataset.sourceId === starButton.dataset.sourceId &&
              candidate.dataset.pairKey === starButton.dataset.pairKey) {
            candidate.dataset.starCount = String(starCount);
          }
        });
        if (starred) locallyStarred.add(identity);
        else locallyStarred.delete(identity);
        saveLocalStars();
        refreshStarButtons();
        document.querySelectorAll("[data-card]").forEach((card) => {
          if (card.dataset.sourceId === starButton.dataset.sourceId &&
              card.dataset.pairKey === starButton.dataset.pairKey) {
            card.dataset.starCount = String(starCount);
          }
        });
        refreshArchive();
      } catch (error) {
        window.alert("The star could not be saved. Please try again.");
      } finally {
        starButton.disabled = false;
      }
      return;
    }

    const deleteButton = event.target.closest("[data-delete-button]");
    if (deleteButton) {
      const confirmed = window.confirm(
        "Permanently delete this capture? This removes the video and its matching snapshot from the Pi. This cannot be undone."
      );
      if (!confirmed) return;
      deleteButton.disabled = true;
      try {
        const response = await fetch("/api/delete", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken
          },
          body: JSON.stringify({
            camera_id: deleteButton.dataset.sourceId,
            pair_key: deleteButton.dataset.pairKey,
            confirmed: true
          })
        });
        if (!response.ok) throw new Error("Could not delete capture");
        const card = deleteButton.closest("[data-card]");
        if (card) {
          card.remove();
          refreshArchive();
        } else {
          window.location.assign("/");
        }
      } catch (error) {
        window.alert("The capture could not be deleted. Nothing else was changed.");
        deleteButton.disabled = false;
      }
      return;
    }

    if (event.target.closest("[data-reset-controls]")) {
      document.querySelector("[data-date-from]").value = "";
      document.querySelector("[data-date-to]").value = "";
      document.querySelector("[data-filter]").value = "birds";
      document.querySelector("[data-sort]").value = "newest";
      activeSpecies = "";
      activeSex = "";
      syncDateControlStates();
      refreshArchive({ resetPage: true });
      return;
    }

    const sectionToggle = event.target.closest("[data-section-toggle]");
    if (sectionToggle) {
      const content = document.getElementById(
        sectionToggle.getAttribute("aria-controls")
      );
      if (!content) return;
      const expanded = sectionToggle.getAttribute("aria-expanded") === "true";
      const nextExpanded = !expanded;
      sectionToggle.setAttribute("aria-expanded", String(nextExpanded));
      sectionToggle.setAttribute(
        "aria-label",
        `${nextExpanded ? "Collapse" : "Expand"} ${sectionToggle.dataset.sectionLabel}`
      );
      content.hidden = !nextExpanded;
      if (nextExpanded) {
        window.requestAnimationFrame(() => renderHourlyActivity(hourlyActivityCards));
      }
      return;
    }

    const sexFilterButton = event.target.closest("[data-sex-filter]");
    if (sexFilterButton) {
      const speciesButton = sexFilterButton.closest(".species-card")
        ?.querySelector("[data-species-filter]");
      if (!speciesButton) return;
      const species = speciesButton.dataset.speciesFilter;
      const sex = sexFilterButton.dataset.sexFilter;
      const sameSelection = activeSpecies === species && activeSex === sex;
      activeSpecies = species;
      activeSex = sameSelection ? "" : sex;
      document.querySelector("[data-filter]").value =
        speciesButton.dataset.visitorKind === "animal" ? "animals" : "birds";
      refreshArchive({ resetPage: true });
      document.querySelector("[data-gallery]")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const speciesButton = event.target.closest("[data-species-filter]");
    if (speciesButton) {
      const species = speciesButton.dataset.speciesFilter;
      activeSpecies = activeSpecies === species ? "" : species;
      activeSex = "";
      if (activeSpecies) {
        document.querySelector("[data-filter]").value =
          speciesButton.dataset.visitorKind === "animal" ? "animals" : "birds";
      }
      refreshArchive({ resetPage: true });
      document.querySelector("[data-gallery]")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    if (event.target.closest("[data-clear-species]")) {
      activeSpecies = "";
      activeSex = "";
      refreshArchive({ resetPage: true });
      return;
    }

    if (event.target.closest("[data-page-previous]")) {
      archivePage -= 1;
      refreshArchive();
      document.querySelector("[data-gallery]")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    if (event.target.closest("[data-page-next]")) {
      archivePage += 1;
      refreshArchive();
      document.querySelector("[data-gallery]")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

  });

  document.querySelectorAll("[data-date-from], [data-date-to], [data-filter], [data-sort]")
    .forEach((control) => control.addEventListener("change", () => {
      if (control.matches('[type="date"]')) syncDateControlStates();
      refreshArchive({ resetPage: true });
    }));
  window.addEventListener("storage", (event) => {
    if (event.key !== starStorageKey) return;
    locallyStarred = loadLocalStars();
    refreshStarButtons();
  });
  refreshStarButtons();
  syncDateControlStates();
  refreshArchive();
</script>
"""
    if deletes_enabled:
        return script.replace("__CSRF_TOKEN__", csrf_token)

    delete_start = script.index(
        '    const deleteButton = event.target.closest("[data-delete-button]");'
    )
    delete_end = script.index(
        '    if (event.target.closest("[data-reset-controls]"))',
        delete_start,
    )
    return (script[:delete_start] + script[delete_end:]).replace(
        "__CSRF_TOKEN__", csrf_token
    )


def _shared_styles() -> str:
    return """
    :root {
      --ink: #17362b;
      --muted: #66776f;
      --paper: #f5f2e9;
      --card: #fffef9;
      --leaf: #247956;
      --leaf-dark: #123f30;
      --leaf-pale: #e5f0e8;
      --line: #d9d9cc;
      --star: #a96100;
      --star-paper: #fff0bd;
      --danger: #a43c32;
      --danger-paper: #fff0ec;
      --shadow: 0 18px 48px rgb(27 58 46 / 10%);
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 0%, rgb(205 224 203 / 58%), transparent 28rem),
        linear-gradient(180deg, #faf8f1 0, var(--paper) 38rem);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 16px;
      line-height: 1.45;
    }
    a, button, input, select { -webkit-tap-highlight-color: transparent; }
    button, input, select { font: inherit; }
    button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible {
      outline: 3px solid rgb(36 121 86 / 28%);
      outline-offset: 2px;
    }
    .eyebrow {
      margin: 0 0 5px;
      color: var(--leaf);
      font-size: .86rem;
      font-weight: 700;
    }
    .primary-action, .secondary-action, .back-button, .star-button, .delete-button {
      display: inline-flex;
      min-height: 42px;
      align-items: center;
      justify-content: center;
      gap: 7px;
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 9px 14px;
      font: inherit;
      font-weight: 750;
      line-height: 1;
      text-decoration: none;
      cursor: pointer;
    }
    .primary-action { color: white; background: var(--leaf); box-shadow: 0 7px 16px rgb(36 121 86 / 20%); }
    .primary-action:hover, .primary-action:focus { background: var(--leaf-dark); transform: translateY(-1px); }
    .secondary-action, .back-button {
      color: var(--ink);
      border-color: var(--line);
      background: rgb(255 255 255 / 62%);
    }
    .secondary-action:hover, .secondary-action:focus,
    .back-button:hover, .back-button:focus { border-color: var(--leaf); background: #edf3ed; }
    .star-button { color: var(--ink); border-color: var(--line); background: var(--card); }
    .star-button-card {
      position: absolute;
      z-index: 2;
      top: 14px;
      right: 14px;
      min-height: 38px;
      padding: 7px 12px;
      border-color: rgb(255 255 255 / 62%);
      background: rgb(255 254 249 / 90%);
      box-shadow: 0 6px 18px rgb(13 31 24 / 16%);
      backdrop-filter: blur(10px);
    }
    .star-button[data-locally-starred="true"] { color: #704300; border-color: #e5bd68; background: var(--star-paper); }
    .star-button:hover, .star-button:focus { border-color: var(--star); }
    .star-button:disabled, .delete-button:disabled, .share-button:disabled { cursor: wait; opacity: .6; }
    .delete-button { width: 42px; padding-inline: 0; color: var(--danger); border-color: transparent; background: transparent; }
    .delete-button-large { width: auto; padding-inline: 18px; }
    .delete-button:hover, .delete-button:focus { border-color: #e1aaa1; background: var(--danger-paper); }
    .actions { display: flex; flex-wrap: wrap; gap: 9px; align-items: center; }
    .classification { display: grid; gap: 0; margin: 0; }
    .species-heading-row { display: flex; flex-wrap: wrap; gap: 9px; align-items: center; }
    .species-name { margin: 0; color: var(--leaf-dark); font-size: clamp(1.55rem, 4vw, 2rem); font-weight: 800; line-height: 1.05; }
    .scientific-name { margin: 6px 0 0; color: var(--muted); font-size: .95rem; line-height: 1.35; }
    .classification-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 18px 0 0; padding: 0; overflow: hidden; border: 1px solid rgb(23 54 43 / 9%); border-radius: 14px; background: #f4f7f2; }
    .classification-stats div { min-width: 0; padding: 10px 12px 11px; }
    .classification-stats div + div { border-left: 1px solid rgb(23 54 43 / 9%); }
    .classification-stats dt { margin: 0 0 2px; color: var(--muted); font-size: .73rem; font-weight: 600; }
    .classification-stats dd { margin: 0; overflow: hidden; color: var(--ink); font-size: .93rem; font-weight: 750; text-overflow: ellipsis; white-space: nowrap; }
    .classification-stats-animal { grid-template-columns: minmax(0, 1fr); }
    .classification-notes { display: grid; gap: 10px; margin-top: 14px; }
    .classification-fact { min-width: 0; margin: 0; padding: 15px 16px; border: 1px solid rgb(36 121 86 / 18%); border-radius: 14px; background: var(--leaf-pale); }
    .classification-fact span { color: var(--leaf-dark); font-size: .78rem; font-weight: 800; }
    .classification-fact p { margin: 5px 0 0; color: var(--ink); font-size: 1.03rem; line-height: 1.52; }
    .observation { display: flex; min-width: 0; gap: 7px; align-items: baseline; margin: 0; padding: 1px 3px 0; }
    .observation span { flex: none; color: var(--muted); font-size: .72rem; font-weight: 700; }
    .observation p { margin: 0; color: var(--muted); font-size: .78rem; line-height: 1.4; }
    .empty-frame-note { margin: 12px 0 0; color: var(--muted); font-size: .96rem; line-height: 1.5; }
    @media (max-width: 620px) {
      .classification-notes { grid-template-columns: 1fr; }
    }
    [hidden] { display: none !important; }
"""


def _about_species_card(pairs: list[GalleryPair]) -> str:
    """Render a real, code-native Northern Cardinal summary card for the story."""
    cardinal_pairs = [
        pair
        for pair in pairs
        if pair.is_bird is True
        and pair.video_path is not None
        and (pair.common_name or "").strip().casefold() == "northern cardinal"
    ]
    hourly = {
        "male": [0] * 24,
        "female": [0] * 24,
        "unknown": [0] * 24,
    }
    monthly = [0] * 12
    thumbnail_path: str | None = None

    if cardinal_pairs:
        for pair in cardinal_pairs:
            sex = pair.sex if pair.sex in {"male", "female"} else "unknown"
            captured = pair.captured_datetime
            if captured is not None:
                hourly[sex][captured.hour] += max(1, pair.bird_count or 1)
                monthly[captured.month - 1] += 1
            if thumbnail_path is None and pair.snapshot_path:
                thumbnail_path = pair.snapshot_path
        male_count = sum(pair.sex == "male" for pair in cardinal_pairs)
        female_count = sum(pair.sex == "female" for pair in cardinal_pairs)
        unknown_count = len(cardinal_pairs) - male_count - female_count
        video_count = len(cardinal_pairs)
    else:
        # Keep the example useful in an empty development archive. Production
        # replaces these values with the current Northern Cardinal observations.
        hourly["male"] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 4, 3, 2, 4, 0, 2, 3, 0, 4, 1, 0, 0, 0]
        hourly["female"] = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 1, 8, 0, 6, 10, 0, 0, 0]
        hourly["unknown"][20] = 1
        monthly[7] = 68
        male_count, female_count, unknown_count, video_count = 28, 39, 1, 68

    thumbnail = (
        f'<img src="{_media_url(thumbnail_path)}" alt="" loading="lazy">'
        if thumbnail_path
        else '<span class="species-card-placeholder" aria-hidden="true">⌁</span>'
    )
    sex_parts = []
    for sex, count in (
        ("male", male_count),
        ("female", female_count),
        ("unknown", unknown_count),
    ):
        if count:
            sex_parts.append(
                f'<span class="sex-key sex-key-{sex}">{count} {sex}</span>'
            )
    sex_breakdown = '<span class="sex-separator" aria-hidden="true"> · </span>'.join(sex_parts)

    width, height = 760, 154
    left, right, top, bottom = 30, 12, 9, 51
    plot_width = width - left - right
    plot_height = height - top - bottom
    greatest = max(value for values in hourly.values() for value in values)
    y_max = 4 if greatest <= 4 else 8 if greatest <= 8 else 16 if greatest <= 16 else ((greatest + 4) // 5) * 5

    def x_position(hour: int) -> float:
        return left + (hour / 23) * plot_width

    def y_position(value: int) -> float:
        return top + plot_height - (value / y_max) * plot_height

    def hour_label(hour: int) -> str:
        def format_hour(value: int) -> str:
            if value == 0:
                return "12 AM"
            if value == 12:
                return "12 PM"
            return f"{value - 12 if value > 12 else value} {'PM' if value >= 12 else 'AM'}"

        return f"{format_hour(hour)}–{format_hour((hour + 1) % 24)}"

    svg_parts = [
        '<svg class="activity-svg" viewBox="0 0 760 154" role="img" '
        'aria-labelledby="about-cardinal-chart-title about-cardinal-chart-description">',
        '<title id="about-cardinal-chart-title">Northern Cardinal sightings by hour, sex, and month</title>',
        '<desc id="about-cardinal-chart-description">Blue solid lines indicate males, pink dashed lines indicate females, and gray dotted lines indicate unknown sex.</desc>',
        f'<rect class="activity-night" x="{left}" y="{top}" width="{x_position(6) - left:.2f}" height="{plot_height}" />',
        f'<rect class="activity-night" x="{x_position(20):.2f}" y="{top}" width="{width - right - x_position(20):.2f}" height="{plot_height}" />',
        f'<text class="activity-daypart" x="{left + 8}" y="{top + 16}">☾</text>',
        f'<text class="activity-daypart" x="{x_position(6) + 8:.2f}" y="{top + 16}">☀</text>',
        f'<text class="activity-daypart" x="{x_position(20) + 8:.2f}" y="{top + 16}">☾</text>',
    ]
    for value in (0, y_max):
        y = y_position(value)
        svg_parts.append(
            f'<line class="activity-grid-line" x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" />'
        )
        svg_parts.append(
            f'<text class="activity-axis-label" x="{left - 7}" y="{y + 4:.2f}" text-anchor="end">{value}</text>'
        )
    for hour, label, anchor in (
        (0, "12 AM", "start"),
        (6, "6 AM", "middle"),
        (12, "Noon", "middle"),
        (18, "6 PM", "middle"),
        (23, "11 PM", "end"),
    ):
        svg_parts.append(
            f'<text class="activity-axis-label" x="{x_position(hour):.2f}" y="{top + plot_height + 18}" text-anchor="{anchor}">{label}</text>'
        )

    month_labels = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    month_maximum = max(1, *monthly)
    month_width = plot_width / 12
    for month, (label, count) in enumerate(zip(month_labels, monthly)):
        intensity = 0.05 if count == 0 else 0.16 + 0.72 * (count / month_maximum)
        cell_x = left + month * month_width + 1
        label_x = left + (month + 0.5) * month_width
        svg_parts.append(
            f'<rect class="activity-month-cell" x="{cell_x:.2f}" y="{height - 22}" width="{max(1, month_width - 2):.2f}" height="18" rx="3" opacity="{intensity:.3f}"><title>{label.title()}: {count} videos</title></rect>'
        )
        svg_parts.append(
            f'<text class="activity-month-label" x="{label_x:.2f}" y="{height - 9}" text-anchor="middle" data-on-dark="{str(intensity >= 0.55).lower()}">{label}</text>'
        )

    series_styles = {
        "unknown": ("#858177", "2 3", "Unknown sex"),
        "female": ("#bd6b87", "5 3", "Female"),
        "male": ("#4f739d", "", "Male"),
    }
    for sex in ("unknown", "female", "male"):
        values = hourly[sex]
        observed = [hour for hour, value in enumerate(values) if value > 0]
        if not observed:
            continue
        first_hour = max(0, observed[0] - 1)
        last_hour = min(23, observed[-1] + 1)
        path = " ".join(
            f"{'M' if hour == first_hour else 'L'}{x_position(hour):.2f} {y_position(values[hour]):.2f}"
            for hour in range(first_hour, last_hour + 1)
        )
        color, dash, label = series_styles[sex]
        dash_attribute = f' stroke-dasharray="{dash}"' if dash else ""
        svg_parts.append(
            f'<path class="activity-line" d="{path}" style="--series-color: {color}"{dash_attribute} />'
        )
        for hour, value in enumerate(values):
            if value == 0:
                continue
            count_label = "bird" if value == 1 else "birds"
            svg_parts.append(
                f'<circle class="activity-point" cx="{x_position(hour):.2f}" cy="{y_position(value):.2f}" r="{3.5 if value == greatest else 2.5}" style="--series-color: {color}" tabindex="0" role="img" aria-label="Northern Cardinal, {label.lower()}, {hour_label(hour)}: {value} {count_label}"><title>{label} · {hour_label(hour)} · {value}</title></circle>'
            )
    svg_parts.append("</svg>")

    return (
        '<figure class="species-card-example">'
        '<div class="species-card-example-scroll" tabindex="0" '
        'aria-label="Example Northern Cardinal species card">'
        '<div class="species-row species-card-shell about-species-card">'
        '<div class="species-card">'
        '<div class="species-card-main">'
        f'<span class="species-card-thumbnail">{thumbnail}</span>'
        '<span class="species-card-copy">'
        '<strong>Northern Cardinal</strong>'
        '<span class="species-card-scientific"><i>Cardinalis cardinalis</i></span>'
        '<span class="species-card-count">'
        f'<b>{video_count}</b> <span>{"video" if video_count == 1 else "videos"}</span>'
        '</span></span></div>'
        f'<div class="species-card-sex">{sex_breakdown}</div>'
        '</div>'
        '<section class="activity-chart" aria-label="Northern Cardinal activity chart">'
        f'<div class="activity-plot">{"".join(svg_parts)}</div>'
        '</section>'
        '</div></div>'
        '<figcaption>The female Northern Cardinal is more active late in the day, at least this one is.</figcaption>'
        '</figure>'
    )


def _render_about(
    *,
    photo_available: bool = False,
    pi_photo_available: bool = False,
    amazon_photo_available: bool = False,
    subscription_photo_available: bool = False,
    pairs: list[GalleryPair] | None = None,
) -> bytes:
    """Render the public project story and a plain-language system overview."""
    feeder_photo = (
        '<img src="/about-feeder.jpg" '
        'alt="The Backyard Birds camera feeder mounted on a pole in the garden">'
        if photo_available
        else """
        <div class="photo-placeholder" data-future-feeder-photo role="img"
          aria-label="Placeholder for a future photo of the bird feeder">
          <svg viewBox="0 0 520 350" aria-hidden="true">
            <path d="M260 42v48M180 105h160l-24 62H204z" />
            <path d="M212 167v92h96v-92M191 259h138M235 259v48M285 259v48" />
            <circle cx="238" cy="203" r="10" />
            <path d="M320 205c36-32 74-19 78 14-23 5-48 0-67-14M352 204l13-18" />
            <path class="ground" d="M82 308c94-17 276-17 356 0" />
          </svg>
          <span>Feeder photo coming soon</span>
          <small>A photo will be added here when it’s ready.</small>
        </div>
        """
    )
    pi_photo = (
        '<img src="/about-raspberry-pi.jpg" '
        'alt="The Raspberry Pi 3 running Backyard Birds beside its USB backup drive and network cables">'
        if pi_photo_available
        else """
        <div class="photo-placeholder" data-future-pi-photo role="img"
          aria-label="Placeholder for a future photo of the Raspberry Pi running Backyard Birds">
          <svg viewBox="0 0 520 350" aria-hidden="true">
            <rect x="124" y="78" width="272" height="184" rx="26" />
            <rect x="158" y="112" width="91" height="72" rx="8" />
            <circle cx="326" cy="134" r="23" />
            <path d="M311 134h30M326 119v30M171 219h110M301 219h45" />
            <path d="M396 128h37v38h-37M396 190h53v42h-53" />
            <path d="M169 78V52M205 78V52M241 78V52" />
            <path d="M216 291c24-27 64-27 88 0M238 309c12-13 32-13 44 0M258 326h2" />
            <path class="ground" d="M83 334h354" />
          </svg>
          <span>Basement Pi photo coming soon</span>
          <small>The hardware portrait will drop into this space.</small>
        </div>
        """
    )
    amazon_photo = (
        '<img src="/about-amazon.png" '
        'alt="Amazon Canada search results for camera bird feeders">'
        if amazon_photo_available
        else ""
    )
    subscription_photo = (
        '<img src="/about-subscription.jpg" '
        'alt="The feeder app showing a 360-day cloud-storage subscription for $59.99">'
        if subscription_photo_available
        else ""
    )
    species_card = _about_species_card(pairs or [])

    document = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>About the project — Backyard Birds</title>
  <style>
__SHARED_STYLES__
    .about-nav { max-width: 1180px; margin: 0 auto; padding: 22px 28px 0; }
    .about-shell { width: min(1180px, 100%); margin: 0 auto; padding: 36px 28px 80px; }
    .about-hero { max-width: 860px; padding: 38px 0 58px; border-bottom: 1px solid rgb(23 54 43 / 12%); }
    .about-hero h1 { margin: 8px 0 20px; font-size: clamp(3rem, 6vw, 5.4rem); font-weight: 800; letter-spacing: -.045em; line-height: .96; }
    .about-tldr { display: grid; grid-template-columns: auto 1fr; gap: 16px; align-items: baseline; max-width: 760px; margin-top: 28px; border-left: 3px solid var(--leaf); padding: 15px 18px; background: var(--leaf-pale); }
    .about-tldr strong { color: var(--leaf-dark); font-size: .78rem; font-weight: 850; letter-spacing: .04em; text-transform: uppercase; }
    .about-tldr p { margin: 0; color: #344d43; line-height: 1.55; }
    .story-grid { display: grid; grid-template-columns: minmax(190px, .55fr) minmax(0, 1.45fr); gap: 56px; padding: 64px 0 72px; }
    .section-label { margin: 0; color: var(--leaf); font-size: .76rem; font-weight: 800; text-transform: uppercase; }
    .story-grid h2, .diagram-heading h2, .resources-heading h2 { margin: 8px 0 0; font-size: clamp(1.9rem, 3.4vw, 3rem); line-height: 1.05; }
    .story-grid > div, .story-copy { min-width: 0; }
    .about-shell figure { max-width: 100%; box-sizing: border-box; }
    .story-copy { max-width: 720px; color: #344d43; font-size: 1.04rem; line-height: 1.72; }
    .story-copy p { margin: 0 0 1.35em; }
    .story-copy h3 { margin: 2em 0 .5em; color: var(--leaf-dark); font-size: 1.22rem; line-height: 1.25; }
    .story-copy h3:first-child { margin-top: 0; }
    .story-photo { width: 100%; max-width: 100%; box-sizing: border-box; overflow: hidden; margin: 1.8em 0; border-radius: 24px; background: #dce7dd; box-shadow: var(--shadow); }
    .story-photo > img { width: 100%; max-height: 540px; aspect-ratio: 4 / 3; display: block; object-fit: cover; object-position: center; }
    .story-photo figcaption { padding: 14px 18px; color: var(--muted); background: var(--card); font-size: .86rem; line-height: 1.5; }
    .subscription-shot { width: min(330px, 100%); overflow: hidden; margin: 1.8em auto; border: 1px solid rgb(23 54 43 / 12%); border-radius: 24px; background: #f7f9f7; box-shadow: var(--shadow); }
    .subscription-shot > img { width: 100%; display: block; }
    .subscription-shot figcaption { padding: 14px 18px; color: var(--muted); background: var(--card); font-size: .86rem; line-height: 1.5; }
    .species-card-example { margin: .5em 0 2.2em; }
    .species-card-example-scroll { width: 100%; overflow-x: auto; border-radius: 18px; }
    .about-species-card { display: grid; width: 720px; max-width: none; grid-template-columns: 270px 450px; overflow: hidden; border: 1px solid rgb(23 54 43 / 13%); border-radius: 18px; color: var(--ink); background: linear-gradient(135deg, #fffef9 0%, #fbfcf7 100%); box-shadow: 0 5px 18px rgb(27 58 46 / 7%); }
    .about-species-card .species-card { display: grid; width: 100%; grid-template-columns: minmax(0, 1fr); gap: 0; min-width: 0; align-items: center; padding: 11px 0; background: transparent; }
    .about-species-card .species-card-main { display: grid; width: 100%; grid-template-columns: 76px minmax(0, 1fr); gap: 12px; min-width: 0; align-items: center; padding: 0 11px; }
    .about-species-card .species-card-thumbnail { position: relative; display: block; width: 76px; aspect-ratio: 1; overflow: hidden; border: 2px solid rgb(255 255 255 / 92%); border-radius: 14px; background: #dfe7dc; box-shadow: 0 2px 8px rgb(27 58 46 / 14%), 0 0 0 1px rgb(23 54 43 / 7%); }
    .about-species-card .species-card-thumbnail img { width: 100%; height: 100%; display: block; object-fit: cover; }
    .about-species-card .species-card-placeholder { display: grid; width: 100%; height: 100%; place-items: center; color: var(--leaf); font-size: 1.8rem; }
    .about-species-card .species-card-copy { display: grid; min-width: 0; align-content: center; gap: 2px; }
    .about-species-card .species-card-copy strong { overflow: hidden; font-size: 1.03rem; font-weight: 780; line-height: 1.12; text-overflow: ellipsis; }
    .about-species-card .species-card-scientific { overflow: hidden; color: var(--muted); font-size: .77rem; text-overflow: ellipsis; white-space: nowrap; }
    .about-species-card .species-card-count { display: inline-flex; width: fit-content; align-items: baseline; gap: 4px; margin-top: 6px; border: 1px solid rgb(36 121 86 / 12%); border-radius: 999px; padding: 3px 8px; color: var(--muted); background: rgb(36 121 86 / 6%); font-size: .7rem; line-height: 1; }
    .about-species-card .species-card-count b { color: var(--leaf-dark); font-size: .86rem; font-variant-numeric: tabular-nums; }
    .about-species-card .species-card-sex { min-width: 0; padding: 8px 12px 0; font-size: .72rem; line-height: 1.25; font-variant-numeric: tabular-nums; }
    .about-species-card .sex-key { display: inline-block; margin-block: -4px; border: 1px solid transparent; border-radius: 999px; padding: 3px 7px; background: transparent; font: inherit; font-weight: 700; white-space: nowrap; }
    .about-species-card .sex-key-male { color: #4f739d; }
    .about-species-card .sex-key-female { color: #bd6b87; }
    .about-species-card .sex-key-unknown { color: #858177; }
    .about-species-card .sex-separator { color: var(--muted); font-weight: 500; }
    .about-species-card .activity-chart { position: relative; width: 100%; min-width: 0; min-height: 154px; overflow: hidden; border-left: 1px solid rgb(23 54 43 / 8%); }
    .about-species-card .activity-plot { position: relative; width: 100%; height: 100%; overflow: hidden; background: linear-gradient(180deg, rgb(247 249 245 / 72%), rgb(255 254 249 / 18%) 70%); }
    .about-species-card .activity-svg { width: 100%; height: 100%; display: block; overflow: hidden; }
    .about-species-card .activity-night { fill: #4c586e; opacity: .08; }
    .about-species-card .activity-daypart { fill: #66776f; font-size: 14px; opacity: .52; }
    .about-species-card .activity-grid-line { stroke: rgb(23 54 43 / 9%); stroke-width: 1; }
    .about-species-card .activity-axis-label { fill: #77837d; font-size: 10.5px; }
    .about-species-card .activity-month-cell { fill: #315a48; }
    .about-species-card .activity-month-label { fill: #425149; font-size: 9.5px; font-weight: 650; pointer-events: none; }
    .about-species-card .activity-month-label[data-on-dark="true"] { fill: #fffef9; }
    .about-species-card .activity-line { fill: none; stroke: var(--series-color); stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; }
    .about-species-card .activity-point { fill: #fffef9; stroke: var(--series-color); stroke-width: 2; outline: none; }
    .about-species-card .activity-point:hover, .about-species-card .activity-point:focus { r: 5px; fill: var(--series-color); stroke: var(--card); }
    .species-card-example figcaption { margin-top: 9px; color: var(--muted); font-size: .82rem; line-height: 1.45; }
    .photo-block { display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(260px, .7fr); gap: 26px; align-items: stretch; margin-bottom: 76px; }
    .photo-block-reverse { grid-template-columns: minmax(260px, .7fr) minmax(0, 1.3fr); }
    .photo-block figure { min-height: 420px; aspect-ratio: 4 / 3; overflow: hidden; margin: 0; border-radius: 26px; background: #dce7dd; box-shadow: var(--shadow); }
    .photo-block figure > img { width: 100%; height: 100%; display: block; object-fit: cover; object-position: center; }
    .photo-placeholder { height: 100%; min-height: 420px; display: grid; place-content: center; justify-items: center; padding: 30px; color: var(--leaf-dark); background: radial-gradient(circle at 50% 20%, #eff5ed, #dce7dd 70%); text-align: center; }
    .photo-placeholder svg { width: min(420px, 86%); fill: none; stroke: #668476; stroke-width: 5; stroke-linecap: round; stroke-linejoin: round; }
    .photo-placeholder .ground { stroke: #99ad9f; stroke-width: 3; }
    .photo-placeholder span { margin-top: 12px; font-size: 1.25rem; font-weight: 800; }
    .photo-placeholder small { margin-top: 5px; color: var(--muted); }
    .photo-caption { display: flex; flex-direction: column; justify-content: flex-end; padding: 28px; border: 1px solid rgb(23 54 43 / 10%); border-radius: 24px; background: rgb(255 254 249 / 72%); }
    .photo-caption strong { font-size: 1.5rem; line-height: 1.12; }
    .photo-caption p { margin: 12px 0 0; color: var(--muted); line-height: 1.58; }
    .diagram-section { margin: 0 0 76px; padding: 42px; overflow: hidden; border-radius: 30px; color: #f8fff9; background: #123f30; box-shadow: 0 24px 60px rgb(15 43 32 / 18%); }
    .diagram-heading { max-width: 700px; margin-bottom: 34px; }
    .diagram-heading .section-label { color: #9fd3b8; }
    .diagram-heading p:last-child { max-width: 620px; margin: 14px 0 0; color: #c4d9ce; }
    .process-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 22px; }
    .process-card { position: relative; min-width: 0; border: 1px solid #a9d1bc; border-radius: 18px; padding: 20px; color: var(--ink); background: #f9fbf5; }
    .process-card::after { position: absolute; top: 50%; right: -18px; color: #9fd3b8; content: "→"; font-size: 1.5rem; font-weight: 800; transform: translateY(-50%); }
    .process-card:last-child::after { content: none; }
    .process-step { display: block; margin-bottom: 14px; color: var(--leaf); font-size: .72rem; font-weight: 800; text-transform: uppercase; }
    .process-card h3 { margin: 0 0 9px; color: var(--leaf-dark); font-size: 1.12rem; line-height: 1.18; }
    .process-card p { margin: 0; color: #4d675c; font-size: .84rem; line-height: 1.48; }
    .process-tag { display: inline-block; margin-top: 14px; border-radius: 999px; padding: 5px 9px; color: var(--leaf-dark); background: var(--leaf-pale); font-size: .7rem; font-weight: 750; }
    .diagram-caption { max-width: 760px; margin: 26px 0 0; color: #c4d9ce; font-size: .86rem; }
    .resources { margin-top: 4px; }
    .resources-heading { max-width: 650px; margin-bottom: 22px; }
    .amazon-panel { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(280px, .85fr); gap: 26px; align-items: stretch; margin-bottom: 30px; }
    .amazon-shot { min-height: 300px; overflow: hidden; border: 1px solid rgb(23 54 43 / 12%); border-radius: 20px; background: #ece9df; box-shadow: var(--shadow); }
    .amazon-shot:empty { display: none; }
    .amazon-shot img { width: 100%; height: 100%; display: block; object-fit: cover; object-position: top; }
    .amazon-copy { display: flex; flex-direction: column; justify-content: center; padding: 28px; border: 1px solid rgb(23 54 43 / 10%); border-radius: 20px; background: var(--card); }
    .amazon-copy h3 { margin: 0; font-size: 1.45rem; line-height: 1.16; }
    .amazon-copy p { margin: 12px 0 0; color: var(--muted); }
    .amazon-copy .resource-link { margin-top: 20px; }
    .affiliate-note { font-size: .82rem; }
    .link-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .resource-link { display: grid; grid-template-columns: 1fr auto; gap: 18px; align-items: center; padding: 22px 24px; border: 1px solid rgb(23 54 43 / 12%); border-radius: 18px; color: var(--ink); background: var(--card); box-shadow: 0 10px 30px rgb(27 58 46 / 6%); text-decoration: none; transition: border-color .18s ease, transform .18s ease, box-shadow .18s ease; }
    .resource-link:hover { border-color: var(--leaf); transform: translateY(-2px); box-shadow: 0 16px 38px rgb(27 58 46 / 12%); }
    .resource-link strong { display: block; font-size: 1.08rem; }
    .resource-link small { display: block; margin-top: 4px; color: var(--muted); }
    .resource-link > span:last-child { color: var(--leaf); font-size: 1.55rem; }
    .about-footer { display: flex; gap: 18px; justify-content: space-between; margin-top: 72px; padding-top: 22px; border-top: 1px solid rgb(23 54 43 / 12%); color: var(--muted); font-size: .88rem; }
    @media (max-width: 820px) {
      .story-grid, .photo-block, .amazon-panel { grid-template-columns: 1fr; gap: 30px; }
      .photo-block-reverse figure { order: -1; }
      .photo-caption { min-height: 210px; }
      .diagram-section { padding: 30px 22px; }
      .process-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .process-card::after { content: none; }
    }
    @media (max-width: 600px) {
      .about-nav { padding: 18px 16px 0; }
      .about-shell { padding: 20px 16px 52px; }
      .about-hero { padding: 22px 0 46px; }
      .about-hero h1 { font-size: clamp(2.7rem, 13vw, 3.6rem); }
      .about-tldr { grid-template-columns: 1fr; gap: 6px; }
      .story-grid { padding: 48px 0; }
      .photo-block, .diagram-section { margin-bottom: 52px; }
      .photo-block figure, .photo-placeholder { min-height: 330px; }
      .about-species-card { width: 100%; grid-template-columns: 1fr; }
      .about-species-card .species-card { padding: 14px 0; }
      .about-species-card .activity-chart { min-height: 170px; border-top: 1px solid rgb(23 54 43 / 8%); border-left: 0; }
      .process-grid, .link-list { grid-template-columns: 1fr; }
      .amazon-shot { min-height: 240px; }
      .about-footer { display: grid; }
    }
  </style>
</head>
<body>
  <nav class="about-nav" aria-label="Site navigation">
    <a class="back-button" href="/">← Back to the birds</a>
  </nav>
  <main class="about-shell">
    <header class="about-hero">
      <p class="eyebrow">About this project</p>
      <h1>What is this?</h1>
      <div class="about-tldr" aria-label="Summary">
        <strong>TL;DR</strong>
        <p>A ten-year-old Raspberry Pi downloads each day’s bird photos and videos, sends the stills to OpenAI for identification, and runs this gallery from my basement.</p>
      </div>
    </header>

    <section class="story-grid" aria-labelledby="story-title">
      <div>
        <p class="section-label">The project</p>
        <h2 id="story-title">Why I built it</h2>
      </div>
      <div class="story-copy">
        <h3>The camera</h3>
        <p>My brother bought the feeder on Amazon. It’s one of several nearly identical, no-name models sold there. When a bird lands, it saves a photo and a short video to its microSD card. It also comes with an iOS app that can identify the species.</p>
        <p>What the box, the manual and the Amazon listing don’t mention is that downloading those photos through the app requires a subscription of $59.99 a year. I thought that was a lot to pay for access to files already stored on a camera I owned.</p>
        <figure class="subscription-shot">
          __SUBSCRIPTION_PHOTO__
          <figcaption>The app’s 360-day cloud-storage plan is $59.99.</figcaption>
        </figure>
        <figure class="story-photo">
          __FEEDER_PHOTO__
          <figcaption>The feeder in my backyard. The camera is built into the front and points at the seed tray.</figcaption>
        </figure>
        <p>Sure, I could also remove the microSD card from the camera every couple of days and copy the images manually, but that’s so old school.</p>

        <h3>Using an old Raspberry Pi</h3>
        <p>I had a Raspberry Pi 3 that was about ten years old. I asked Codex whether it could take the app out of the loop and handle the downloads instead. He thought it was a great idea, but he thinks everything is a great idea.</p>

        <h3>Getting files off the camera</h3>
        <p>This was the difficult part. The camera has no public API and it doesn’t provide a normal web, FTP, RTSP, or ONVIF interface. The first step was to watch what happened when I used the iOS app. It finds the camera on my home network with a UDP broadcast and then opens a direct connection to it. That was useful, but the actual conversation was a proprietary binary protocol. There was no normal address or folder for the Pi to connect to.</p>
        <p>Instead of trying to recreate the entire protocol, Codex looked at the Android version of the same app and found the manufacturer’s transport library. That library already knew how to authenticate with the camera and exchange messages. Codex built a small compatibility layer so it could run on the Raspberry Pi, then wrote the parts needed to list the recordings, request each JPEG and MP4, check every chunk, retry anything missing, and save complete files to the archive.</p>
        <p>Getting the files off the camera exposed another problem. Chrome and Android phones could open the MP4s, but iOS could not, and they couldn’t be sent through iMessage. The videos had unusual dimensions and were missing some of the structure and metadata Apple expects from an MP4.</p>
        <p>To fix that, the Pi runs each video through FFmpeg after it is downloaded. The script creates a separate iPhone-friendly H.264/AAC copy with even dimensions, standard colour and codec settings, and the playback index moved to the beginning of the file. The original from the camera is left untouched, and the converted copy is cached for sharing and downloading.</p>

        <h3>What happens each night</h3>
        <p>The downloader starts at 2:30 a.m. and downloads the previous day’s worth of files. The camera was unreliable if we tried to move too much in one connection, so the Pi opens a new authenticated session for every JPEG and MP4, downloads one file at a time, and waits 30 seconds before the next one. A failed file gets up to three attempts. Files arrive in 1,200-byte chunks; the downloader checks offsets and lengths and asks for missing chunks again.</p>
        <p>At 6:00 a.m., another job sends the still image paired with each new video to OpenAI. It asks for a species identification and a short fact. The gallery then shows the video, identification, fact, stars, and the summary charts at the top of the page.</p>

        <h3>The server</h3>
        <p>This website runs on the same Raspberry Pi 3 in my basement. It connects to the camera and router over Wi-Fi, uses an old iPad charger for power, and backs up the archive to a USB key.</p>
        <p>I bought the domain through Cloudflare for about $10 a year. The Pi keeps an outgoing Cloudflare Tunnel connection open, and Cloudflare sends visits to the domain through that tunnel to the website running on the Pi. That means I can host the site from my basement without opening a port on my router.</p>

        <h3>Reading the species charts</h3>
        <p>Each species card at the top of the gallery has a line chart showing when that bird has visited. The horizontal axis runs from midnight to 11 p.m., and the vertical axis shows how many birds were recorded in each one-hour window. Blue is male, pink is female, and gray means the sex couldn’t be determined. Tap or hover over a point to see the exact hour and count. The shaded sections at either end are nighttime, and the row along the bottom shows which months produced the most videos.</p>
        <p>The charts provide a quick overview of the counts at each time of day for each species. The position of the peaks makes it easy to see that one species tends to arrive early in the morning while another visits later in the day.</p>
        __SPECIES_CARD__

        <h3>Why I’m sharing it</h3>
        <p>This has been a labour of love. I built it with Codex, and Codex in turn is built on the work of a great many people: decades of software, research, documentation, and shared ideas. None of this appeared from nowhere. So I’m putting the code online for free, in the same spirit, for anyone who wants to use it, change it, or learn from it. If it also saves somebody $59.99 a year or gives an old Raspberry Pi a job, even better.</p>
      </div>
    </section>

    <section class="diagram-section" aria-labelledby="diagram-title">
      <div class="diagram-heading">
        <div>
          <p class="section-label">Under the hood</p>
          <h2 id="diagram-title">How it works</h2>
        </div>
        <p>The camera records each visit. The Raspberry Pi collects and organizes the files, sends an image for identification, and runs the website.</p>
      </div>
      <div class="process-grid" role="list" aria-label="How the system works">
        <article class="process-card" role="listitem">
          <span class="process-step">1 · Camera</span>
          <h3>Save the visit</h3>
          <p>The feeder writes a JPEG and MP4 to its microSD card.</p>
        </article>
        <article class="process-card" role="listitem">
          <span class="process-step">2 · 2:30 a.m.</span>
          <h3>Download the files</h3>
          <p>The Pi opens one session per file, checks every chunk, and waits 30 seconds between transfers.</p>
          <span class="process-tag">USB key backup</span>
        </article>
        <article class="process-card" role="listitem">
          <span class="process-step">3 · 6:00 a.m.</span>
          <h3>Identify the bird</h3>
          <p>OpenAI receives the paired still image and returns a species name and short fact.</p>
        </article>
        <article class="process-card" role="listitem">
          <span class="process-step">4 · Website</span>
          <h3>Show the results</h3>
          <p>The same Pi serves the videos, identifications, stars, filters, and species charts.</p>
          <span class="process-tag">Cloudflare Tunnel → browser</span>
        </article>
      </div>
      <p class="diagram-caption">The gallery still runs on the basement Pi; Cloudflare Tunnel provides its public route without opening a router port. Administrative tools stay on the trusted home network.</p>
    </section>

    <section class="photo-block photo-block-reverse" aria-labelledby="pi-photo-title">
      <div class="photo-caption">
        <p class="section-label">In the basement</p>
        <strong id="pi-photo-title">The Raspberry Pi 3</strong>
        <p>The Pi is in the basement, connected over Wi-Fi and powered by an old iPad charger. The red USB key is used for backups.</p>
      </div>
      <figure>
        __PI_PHOTO__
      </figure>
    </section>

    <section class="resources" aria-labelledby="links-title">
      <div class="resources-heading">
        <p class="section-label">Similar products</p>
        <h2 id="links-title">Camera bird feeders</h2>
      </div>
      <div class="amazon-panel">
        <div class="amazon-shot">__AMAZON_PHOTO__</div>
        <div class="amazon-copy">
          <h3>Similar feeders on Amazon</h3>
          <p>There are many cameras like this sold under different names. This search shows the general type of feeder I’m using.</p>
          <p class="affiliate-note">This isn’t an affiliate link. I don’t get paid or receive anything if you buy one.</p>
          <a class="resource-link" href="__AMAZON_URL__" target="_blank" rel="noopener noreferrer">
            <span><strong>Search Amazon Canada</strong><small>See similar camera bird feeders.</small></span><span aria-hidden="true">↗</span>
          </a>
        </div>
      </div>
      <div class="link-list">
        <a class="resource-link" href="__GITHUB_URL__" target="_blank" rel="noopener noreferrer">
          <span><strong>Follow the project on GitHub</strong><small>The source code will live here as the project grows.</small></span><span aria-hidden="true">↗</span>
        </a>
        <a class="resource-link" href="/">
          <span><strong>Return to the bird gallery</strong><small>Meet the latest backyard visitors.</small></span><span aria-hidden="true">→</span>
        </a>
      </div>
    </section>
    <footer class="about-footer">
      <span>Backyard Birds · Toronto, Canada</span>
      <span>Running on a Raspberry Pi 3</span>
    </footer>
  </main>
</body>
</html>
"""
    return (
        document.replace("__SHARED_STYLES__", _shared_styles())
        .replace("__FEEDER_PHOTO__", feeder_photo)
        .replace("__PI_PHOTO__", pi_photo)
        .replace("__AMAZON_PHOTO__", amazon_photo)
        .replace("__SUBSCRIPTION_PHOTO__", subscription_photo)
        .replace("__SPECIES_CARD__", species_card)
        .replace("__AMAZON_URL__", html.escape(AMAZON_BIRD_FEEDER_URL, quote=True))
        .replace("__GITHUB_URL__", html.escape(GITHUB_REPOSITORY_URL, quote=True))
        .encode("utf-8")
    )


def _render_index(
    pairs: list[GalleryPair],
    csrf_token: str,
    *,
    public_read_only: bool = False,
) -> bytes:
    cards: list[str] = []
    initial_bird_cards = 0
    for pair in pairs:
        escaped_label = html.escape(pair.captured_label)
        eager_thumbnail = pair.is_bird is True and initial_bird_cards < 24
        if pair.is_bird is True:
            initial_bird_cards += 1
        if pair.snapshot_path:
            snapshot_url = _media_url(pair.snapshot_path)
            if eager_thumbnail:
                image_source = f'src="{snapshot_url}"'
            else:
                image_source = f'data-src="{snapshot_url}"'
            image_content = (
                f'<img {image_source} '
                f'alt="Bird feeder capture from {escaped_label}" loading="lazy" '
                'decoding="async">'
            )
        else:
            image_content = '<div class="placeholder">No snapshot available</div>'

        image = image_content

        if pair.video_path:
            card_url = _watch_url(pair.video_path)
            card_label = f"Watch capture from {escaped_label}"
            actions = (
                ""
                if public_read_only
                else f'<div class="actions card-utility-actions">{_delete_button(pair)}</div>'
            )
        else:
            assert pair.snapshot_path is not None
            card_url = _media_url(pair.snapshot_path)
            card_label = f"View capture from {escaped_label}"
            actions = (
                ""
                if public_read_only
                else f'<div class="actions card-utility-actions">{_delete_button(pair)}</div>'
            )

        classification = _classification_label(pair, detailed=False)
        if not classification:
            classification = (
                '<div class="classification classification-pending">'
                '<h2 class="species-name">Species pending</h2>'
                '<p class="empty-frame-note">Awaiting bird identification</p>'
                "</div>"
            )

        cards.append(
            """
            <article class="card" data-card{default_hidden} {capture_identity}
              data-date="{date_value}"
              data-timestamp="{sort_value}" data-star-count="{star_count}"
              data-classified="{classified}" data-is-bird="{is_bird}"
              data-is-animal="{is_animal}"
              data-bird-count="{bird_count}"
              data-has-video="{has_video}" data-species="{species_key}"
              data-species-label="{species_label}"
              data-sex="{sex}"
              data-snapshot-url="{snapshot_url}">
              <a class="card-open-link" href="{card_url}"
                aria-label="{card_label}"></a>
              <div class="media">
                {image}
                {star_button}
                <span class="time-chip">{time}</span>
              </div>
              <div class="details">
                <p class="capture-date">{date}</p>
                {classification}
                {actions}
              </div>
            </article>
            """.format(
                image=image,
                default_hidden="" if pair.is_bird is True else " hidden",
                capture_identity=(
                    f'data-source-id="{html.escape(pair.source_id, quote=True)}" '
                    f'data-pair-key="{html.escape(pair.pair_key, quote=True)}"'
                ),
                date_value=html.escape(pair.date_value, quote=True),
                sort_value=html.escape(pair.sort_value, quote=True),
                star_count=pair.star_count,
                classified=str(pair.common_name is not None).lower(),
                is_bird=(
                    "true" if pair.is_bird is True else "false" if pair.is_bird is False else ""
                ),
                is_animal=str(_is_animal_visitor(pair)).lower(),
                bird_count=(
                    str(pair.bird_count) if pair.bird_count is not None else ""
                ),
                has_video=str(pair.video_path is not None).lower(),
                species_key=html.escape(
                    _species_key(pair) or _animal_key(pair), quote=True
                ),
                species_label=html.escape(pair.common_name or "", quote=True),
                sex=html.escape(pair.sex or "", quote=True),
                snapshot_url=(
                    html.escape(_media_url(pair.snapshot_path), quote=True)
                    if pair.snapshot_path
                    else ""
                ),
                card_url=html.escape(card_url, quote=True),
                card_label=card_label,
                star_button=_star_button(pair),
                time=html.escape(pair.time_label),
                date=html.escape(pair.date_label),
                classification=classification,
                actions=actions,
            )
        )

    species_cards: list[str] = []
    for species in _species_summaries(pairs):
        thumbnail = (
            f'<img src="{_media_url(species.thumbnail_path)}" '
            f'alt="" loading="lazy" data-species-thumbnail>'
            if species.thumbnail_path
            else '<img alt="" data-species-thumbnail hidden>'
        )
        placeholder_hidden = " hidden" if species.thumbnail_path else ""
        scientific_name = (
            f'<span class="species-card-scientific"><i>{html.escape(species.scientific_name)}</i></span>'
            if species.scientific_name
            else ""
        )
        count_label = "video" if species.video_count == 1 else "videos"
        sex_breakdown = _sex_breakdown_markup(
            species.male_video_count,
            species.female_video_count,
            species.unknown_sex_video_count,
        )
        escaped_species_key = html.escape(species.key, quote=True)
        activity_label = html.escape(
            f"{species.common_name} sightings by hour, sex, and month", quote=True
        )
        species_cards.append(
            '<div class="species-row species-card-shell" data-species-row>'
            '<div class="species-card" data-active="false">'
            '<button class="species-card-main" type="button" '
            f'data-species-filter="{escaped_species_key}" '
            f'data-species-name="{html.escape(species.common_name.casefold(), quote=True)}" '
            'data-visitor-kind="bird" '
            f'data-video-count="{species.video_count}" '
            'data-active="false" aria-pressed="false">'
            '<span class="species-card-thumbnail">'
            f'{thumbnail}<span class="species-card-placeholder" aria-hidden="true" '
            f'data-species-placeholder{placeholder_hidden}>⌁</span></span>'
            '<span class="species-card-copy">'
            f'<strong>{html.escape(species.common_name)}</strong>{scientific_name}'
            '<span class="species-card-count">'
            f'<b data-species-count>{species.video_count}</b> '
            f'<span data-species-count-label>{count_label}</span>'
            '</span></span>'
            '</button>'
            f'<div class="species-card-sex" data-species-sex-breakdown>{sex_breakdown}</div>'
            '</div>'
            '<section class="activity-chart" data-hourly-activity '
            f'data-chart-species="{escaped_species_key}" '
            f'aria-label="{activity_label}" hidden>'
            '<div class="activity-plot">'
            '<svg class="activity-svg" data-hourly-svg role="img"></svg>'
            '<div class="activity-tooltip" data-hourly-tooltip role="status" hidden></div>'
            '</div>'
            '<p class="activity-detail" data-hourly-detail aria-live="polite"></p>'
            '</section>'
            '</div>'
        )

    if species_cards:
        species_content = (
            '<div class="species-grid summary-grid">'
            + "".join(species_cards)
            + "</div>"
        )
    else:
        species_content = (
            '<p class="species-empty">Identified bird species will appear here as videos are added.</p>'
        )

    animal_cards: list[str] = []
    for animal in _animal_summaries(pairs):
        thumbnail = (
            f'<img src="{_media_url(animal.thumbnail_path)}" '
            f'alt="" loading="lazy" data-species-thumbnail>'
            if animal.thumbnail_path
            else '<img alt="" data-species-thumbnail hidden>'
        )
        placeholder_hidden = " hidden" if animal.thumbnail_path else ""
        scientific_name = (
            f'<span class="species-card-scientific"><i>{html.escape(animal.scientific_name)}</i></span>'
            if animal.scientific_name
            else ""
        )
        count_label = "video" if animal.video_count == 1 else "videos"
        sex_breakdown = _sex_breakdown_markup(
            animal.male_video_count,
            animal.female_video_count,
            animal.unknown_sex_video_count,
        )
        escaped_animal_key = html.escape(animal.key, quote=True)
        activity_label = html.escape(
            f"{animal.common_name} sightings by hour, sex, and month", quote=True
        )
        animal_cards.append(
            '<div class="species-row species-card-shell animal-card-shell" data-species-row>'
            '<div class="species-card animal-card" data-active="false">'
            '<button class="species-card-main" type="button" '
            f'data-species-filter="{escaped_animal_key}" '
            f'data-species-name="{html.escape(animal.common_name.casefold(), quote=True)}" '
            'data-visitor-kind="animal" '
            f'data-video-count="{animal.video_count}" '
            'data-active="false" aria-pressed="false">'
            '<span class="species-card-thumbnail">'
            f'{thumbnail}<span class="species-card-placeholder" aria-hidden="true" '
            f'data-species-placeholder{placeholder_hidden}>⌁</span></span>'
            '<span class="species-card-copy">'
            f'<strong>{html.escape(animal.common_name)}</strong>{scientific_name}'
            '<span class="species-card-count">'
            f'<b data-species-count>{animal.video_count}</b> '
            f'<span data-species-count-label>{count_label}</span>'
            '</span></span>'
            '</button>'
            f'<div class="species-card-sex" data-species-sex-breakdown>{sex_breakdown}</div>'
            '</div>'
            '<section class="activity-chart animal-activity-chart" data-hourly-activity '
            f'data-chart-species="{escaped_animal_key}" '
            f'aria-label="{activity_label}" hidden>'
            '<div class="activity-plot">'
            '<svg class="activity-svg" data-hourly-svg role="img"></svg>'
            '<div class="activity-tooltip" data-hourly-tooltip role="status" hidden></div>'
            '</div>'
            '<p class="activity-detail" data-hourly-detail aria-live="polite"></p>'
            '</section>'
            '</div>'
        )

    if animal_cards:
        animal_content = (
            '<div class="animal-grid summary-grid">'
            + "".join(animal_cards)
            + "</div>"
        )
    else:
        animal_content = (
            '<p class="species-empty">Squirrels, rats, raccoons, and any other animals caught helping themselves will appear here.</p>'
        )

    initial_result_total = sum(pair.is_bird is True for pair in pairs)
    initial_page_count = max(1, (initial_result_total + 23) // 24)
    empty_hidden = " hidden" if cards else ""
    empty = """
      <section class="empty" data-empty-archive{hidden}>
        <div class="empty-icon" aria-hidden="true">⌁</div>
        <h2>No bird-feeder media yet</h2>
        <p>The gallery will populate after the first successful source sync.</p>
      </section>
    """.format(hidden=empty_hidden)

    document = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>Backyard Birds</title>
  <style>
{shared_styles}
    .hero {{ position: relative; overflow: hidden; border-bottom: 1px solid rgb(23 54 43 / 10%); }}
    .hero::after {{
      position: absolute; inset: -24rem -32rem auto auto; width: 54rem; aspect-ratio: 1.7;
      border: 1px solid rgb(36 121 86 / 8%); border-radius: 50%; content: "";
    }}
    .site-header {{ position: relative; z-index: 1; display: flex; max-width: 1480px; gap: 24px; align-items: center; justify-content: space-between; margin: 0 auto; padding: 22px 28px 18px; }}
    .site-header > p:first-child {{ margin-bottom: 10px; }}
    .site-links {{ display: flex; flex: 0 0 auto; gap: 8px; align-items: center; }}
    .site-links a {{ min-height: 40px; display: inline-flex; align-items: center; border: 1px solid rgb(36 121 86 / 24%); border-radius: 999px; padding: 8px 15px; color: var(--leaf-dark); background: rgb(255 254 249 / 68%); font-weight: 750; text-decoration: none; backdrop-filter: blur(10px); }}
    .site-links a:hover {{ border-color: var(--leaf); background: var(--leaf-pale); }}
    h1 {{ margin: 0; max-width: 850px; font-size: clamp(2.5rem, 5vw, 4.3rem); font-weight: 800; line-height: .94; }}
    .archive-shell {{ max-width: 1480px; margin: 0 auto; padding: 16px 28px 64px; container: archive / inline-size; }}
    .species-section {{ margin-bottom: 18px; padding: 18px; border: 1px solid rgb(23 54 43 / 10%); border-radius: 20px; background: rgb(255 254 249 / 72%); box-shadow: 0 12px 34px rgb(27 58 46 / 6%); }}
    .species-section-header {{ display: flex; gap: 20px; align-items: end; justify-content: space-between; margin-bottom: 14px; }}
    .species-section-header:has(+ [data-section-content][hidden]) {{ margin-bottom: 0; }}
    .species-section h2 {{ margin: 0; font-size: clamp(1.45rem, 3vw, 2rem); line-height: 1.1; }}
    .species-section-description {{ margin: 5px 0 0; color: var(--muted); font-size: .9rem; }}
    .species-section-actions {{ display: flex; flex: 0 0 auto; gap: 8px; align-items: center; }}
    .section-toggle {{ display: inline-grid; width: 40px; min-height: 40px; flex: 0 0 40px; place-items: center; border: 1px solid rgb(36 121 86 / 28%); border-radius: 999px; padding: 0; color: var(--leaf-dark); background: var(--card); cursor: pointer; }}
    .section-toggle:hover {{ border-color: var(--leaf); background: var(--leaf-pale); }}
    .section-toggle svg {{ width: 18px; height: 18px; transition: transform .18s ease; }}
    .section-toggle[aria-expanded="true"] svg {{ transform: rotate(180deg); }}
    .clear-species {{ flex: 0 0 auto; min-height: 38px; border: 1px solid var(--leaf); border-radius: 999px; padding: 7px 13px; color: var(--leaf-dark); background: var(--leaf-pale); font-weight: 750; cursor: pointer; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 220px), 1fr)); gap: 10px; }}
    .species-grid {{ grid-template-columns: minmax(0, 1fr); }}
    .species-row {{ display: grid; width: 100%; max-width: 100%; grid-template-columns: 300px minmax(0, 1fr); gap: 0; min-width: 0; align-items: stretch; }}
    .species-card-shell {{ overflow: hidden; border: 1px solid rgb(23 54 43 / 13%); border-radius: 18px; background: linear-gradient(135deg, #fffef9 0%, #fbfcf7 100%); box-shadow: 0 5px 18px rgb(27 58 46 / 5%); transition: border-color .18s ease, background .18s ease, transform .18s ease, box-shadow .18s ease; }}
    .species-card-shell:hover {{ border-color: rgb(36 121 86 / 38%); box-shadow: 0 9px 24px rgb(27 58 46 / 9%); transform: translateY(-1px); }}
    .species-card-shell:has(.species-card[data-active="true"]) {{ border-color: var(--leaf); background: var(--leaf-pale); box-shadow: 0 0 0 2px rgb(36 121 86 / 13%); }}
    .species-card {{ display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 10px 12px; min-width: 0; align-items: center; border: 1px solid rgb(23 54 43 / 12%); border-radius: 15px; padding: 11px; color: var(--ink); background: var(--card); text-align: left; cursor: pointer; transition: border-color .18s ease, background .18s ease, transform .18s ease; }}
    .species-row .species-card {{ width: 100%; grid-template-columns: minmax(0, 1fr); border: 0; border-radius: 0; padding-inline: 0; background: transparent; row-gap: 0; cursor: default; }}
    .species-row .species-card:hover {{ border-color: transparent; transform: none; }}
    .species-row .species-card[data-active="true"] {{ border-color: transparent; background: transparent; box-shadow: none; }}
    .species-card-main {{ display: grid; width: 100%; grid-template-columns: 76px minmax(0, 1fr); gap: 12px; min-width: 0; align-items: center; border: 0; padding: 0 11px; color: inherit; background: transparent; text-align: left; cursor: pointer; }}
    .species-card:hover {{ border-color: rgb(36 121 86 / 48%); transform: translateY(-1px); }}
    .species-card[data-active="true"] {{ border-color: var(--leaf); background: var(--leaf-pale); box-shadow: 0 0 0 2px rgb(36 121 86 / 13%); }}
    .animal-section {{ border-color: rgb(126 86 48 / 16%); background: rgb(251 247 238 / 78%); }}
    .animal-section .eyebrow, .animal-section .species-card-count b {{ color: #7b512f; }}
    .animal-grid {{ grid-template-columns: minmax(0, 1fr); }}
    .animal-card-shell:hover {{ border-color: rgb(126 86 48 / 38%); }}
    .animal-card-shell:has(.animal-card[data-active="true"]) {{ border-color: #8b603b; background: #f5eadb; box-shadow: 0 0 0 2px rgb(126 86 48 / 13%); }}
    .animal-card:hover {{ border-color: rgb(126 86 48 / 48%); }}
    .animal-card[data-active="true"] {{ border-color: #8b603b; background: #f5eadb; box-shadow: 0 0 0 2px rgb(126 86 48 / 13%); }}
    .species-card-thumbnail {{ position: relative; display: block; width: 76px; aspect-ratio: 1; overflow: hidden; border: 2px solid rgb(255 255 255 / 92%); border-radius: 14px; background: #dfe7dc; box-shadow: 0 2px 8px rgb(27 58 46 / 14%), 0 0 0 1px rgb(23 54 43 / 7%); }}
    .species-card-thumbnail img {{ width: 100%; height: 100%; display: block; object-fit: cover; }}
    .species-card-placeholder {{ display: grid; width: 100%; height: 100%; place-items: center; color: var(--leaf); font-size: 1.8rem; }}
    .species-card-copy {{ display: grid; min-width: 0; align-content: center; gap: 2px; }}
    .species-card-copy strong {{ overflow: hidden; font-size: 1.03rem; font-weight: 780; line-height: 1.12; text-overflow: ellipsis; }}
    .species-card-scientific {{ overflow: hidden; color: var(--muted); font-size: .77rem; text-overflow: ellipsis; white-space: nowrap; }}
    .species-card-count {{ display: inline-flex; width: fit-content; align-items: baseline; gap: 4px; margin-top: 6px; border: 1px solid rgb(36 121 86 / 12%); border-radius: 999px; padding: 3px 8px; color: var(--muted); background: rgb(36 121 86 / 6%); font-size: .7rem; line-height: 1; }}
    .species-card-count b {{ color: var(--leaf-dark); font-size: .86rem; font-variant-numeric: tabular-nums; }}
    .species-card-sex {{ grid-column: 1 / -1; display: block; min-width: 0; padding: 8px 12px 0; border: 0; font-size: .72rem; line-height: 1.25; font-variant-numeric: tabular-nums; white-space: normal; }}
    .sex-key {{ display: inline-block; margin-block: -4px; border: 1px solid transparent; border-radius: 999px; padding: 3px 7px; background: transparent; font: inherit; font-weight: 700; white-space: nowrap; cursor: pointer; transition: border-color .16s ease, background .16s ease; }}
    .sex-key-male {{ color: #4f739d; }}
    .sex-key-female {{ color: #bd6b87; }}
    .sex-key-unknown {{ color: #858177; }}
    .sex-separator {{ color: var(--muted); font-weight: 500; }}
    .sex-key-male[aria-pressed="true"] {{ border-color: rgb(79 115 157 / 26%); background: rgb(79 115 157 / 10%); }}
    .sex-key-female[aria-pressed="true"] {{ border-color: rgb(189 107 135 / 28%); background: rgb(189 107 135 / 10%); }}
    .sex-key-unknown[aria-pressed="true"] {{ border-color: rgb(133 129 119 / 28%); background: rgb(133 129 119 / 10%); }}
    .species-empty {{ margin: 0; border: 1px dashed #aab6ac; border-radius: 14px; padding: 20px; color: var(--muted); text-align: center; }}
    .activity-chart {{ position: relative; width: 100%; max-width: 100%; min-width: 0; min-height: 154px; overflow: hidden; border-left: 1px solid rgb(23 54 43 / 8%); }}
    .activity-plot {{ position: relative; width: 100%; max-width: 100%; height: 100%; min-width: 0; min-height: 0; overflow: hidden; background: linear-gradient(180deg, rgb(247 249 245 / 72%), rgb(255 254 249 / 18%) 70%); }}
    .activity-svg {{ position: absolute; inset: 0; display: block; width: 100%; max-width: 100%; height: 100%; overflow: hidden; }}
    .activity-daypart-icon {{ fill: #66776f; opacity: .52; }}
    .activity-grid line {{ stroke: rgb(23 54 43 / 9%); stroke-width: 1; }}
    .activity-grid text, .activity-x-axis text {{ fill: #77837d; font-size: 10.5px; }}
    .activity-month-cell {{ fill: #315a48; }}
    .animal-activity-chart .activity-month-cell {{ fill: #8b603b; }}
    .activity-month-label {{ fill: #425149; font-size: 9.5px; font-weight: 650; pointer-events: none; }}
    .activity-month-label[data-on-dark="true"] {{ fill: #fffef9; }}
    .activity-series {{ opacity: 1; }}
    .activity-line {{ fill: none; stroke: var(--series-color); stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; }}
    .activity-point {{ fill: #fffef9; stroke: var(--series-color); stroke-width: 2; cursor: pointer; transition: r .12s ease; }}
    .activity-point:hover, .activity-point:focus, .activity-point[data-selected="true"] {{ r: 5px; fill: var(--series-color); stroke: var(--card); outline: none; }}
    .activity-tooltip {{ position: absolute; z-index: 2; max-width: 150px; border: 1px solid rgb(23 54 43 / 12%); border-radius: 8px; padding: 6px 8px; color: var(--ink); background: var(--card); box-shadow: 0 7px 20px rgb(27 58 46 / 13%); font-size: .7rem; font-weight: 700; line-height: 1.25; pointer-events: none; text-align: center; transform: translate(-50%, calc(-100% - 10px)); }}
    .activity-detail {{ position: absolute; width: 1px; height: 1px; overflow: hidden; margin: -1px; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }}
    .toolbar {{
      display: grid; grid-template-columns: repeat(2, minmax(0, .9fr)) minmax(0, 1fr) minmax(0, 1.2fr) auto; gap: 9px;
      align-items: center; margin-bottom: 18px; padding: 10px;
      border: 1px solid rgb(23 54 43 / 10%); border-radius: 15px;
      background: rgb(255 254 249 / 78%); box-shadow: 0 10px 30px rgb(27 58 46 / 5%);
      backdrop-filter: blur(14px);
    }}
    .date-range {{ display: grid; grid-column: span 2; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; min-width: 0; }}
    .control {{ position: relative; min-width: 0; }}
    .date-control {{ overflow: hidden; border-radius: 10px; }}
    .control label {{
      position: absolute; z-index: 1; top: 5px; left: 12px; color: var(--muted);
      font-size: .65rem; font-weight: 750; line-height: 1; pointer-events: none;
    }}
    .control input, .control select {{
      width: 100%; min-height: 48px; border: 1px solid var(--line); border-radius: 10px;
      padding: 18px 11px 4px; color: var(--ink); background: #fffefb; line-height: 1.15;
    }}
    .control select {{
      padding-right: 38px; -webkit-appearance: none; appearance: none;
    }}
    .control-wide::after {{
      position: absolute; z-index: 1; top: 50%; right: 15px; width: 7px; height: 7px;
      border-right: 2px solid var(--ink); border-bottom: 2px solid var(--ink);
      content: ""; pointer-events: none; transform: translateY(-65%) rotate(45deg);
    }}
    .date-control input[type="date"] {{
      display: block; width: 100%; min-width: 0; max-width: 100%;
      inline-size: 100%; min-inline-size: 0; max-inline-size: 100%; padding-right: 12px;
      -webkit-appearance: none; appearance: none;
    }}
    .date-control input[type="date"]::-webkit-date-and-time-value {{
      width: 100%; min-width: 0; min-height: 1.15em; overflow: hidden; text-align: left;
    }}
    .date-control input[type="date"]::-webkit-datetime-edit {{ min-width: 0; overflow: hidden; padding: 0; }}
    .date-empty-state {{
      position: absolute; z-index: 2; left: 12px; bottom: 6px; color: var(--ink);
      font-size: .88rem; line-height: 1.15; pointer-events: none;
    }}
    .date-control[data-has-value="false"] input[type="date"] {{ color: transparent; }}
    .date-control[data-has-value="false"]:focus-within input[type="date"] {{ color: var(--ink); }}
    .date-control[data-has-value="true"] .date-empty-state,
    .date-control:focus-within .date-empty-state {{ display: none; }}
    .reset-button {{ min-height: 40px; border: 0; padding: 7px 9px; color: var(--leaf); background: transparent; font-size: .9rem; font-weight: 750; cursor: pointer; }}
    .gallery {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
      gap: 18px;
    }}
    .pagination {{ display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); gap: 14px; align-items: center; margin-top: 22px; }}
    .pagination-summary {{ margin: 0; color: var(--muted); font-size: .88rem; }}
    .pagination-page {{ margin: 0; color: var(--ink); font-size: .88rem; font-weight: 750; text-align: center; }}
    .pagination-actions {{ display: flex; gap: 8px; justify-content: flex-end; }}
    .page-button {{ min-height: 40px; border: 1px solid rgb(36 121 86 / 28%); border-radius: 999px; padding: 8px 14px; color: var(--leaf-dark); background: var(--card); font-weight: 750; cursor: pointer; }}
    .page-button:hover:not(:disabled) {{ border-color: var(--leaf); background: var(--leaf-pale); }}
    .page-button:disabled {{ cursor: default; opacity: .45; }}
    .card {{ position: relative; display: grid; grid-template-rows: auto 1fr; overflow: hidden; min-width: 0; border: 1px solid rgb(23 54 43 / 10%); border-radius: 18px; background: var(--card); box-shadow: 0 12px 32px rgb(27 58 46 / 9%); cursor: pointer; transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease; }}
    .card:hover {{ border-color: rgb(36 121 86 / 52%); transform: translateY(-6px); box-shadow: 0 24px 52px rgb(27 58 46 / 22%), 0 0 0 2px rgb(36 121 86 / 10%); }}
    .card:focus-within {{ outline: 3px solid rgb(36 121 86 / 38%); outline-offset: 3px; border-color: var(--leaf); }}
    .card-open-link {{ position: absolute; z-index: 2; inset: 0; border-radius: inherit; cursor: pointer; }}
    .card-open-link:focus-visible {{ outline: none; }}
    .media {{ position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: #dfe7dc; }}
    .media::after {{ position: absolute; inset: auto 0 0; height: 38%; content: ""; pointer-events: none; background: linear-gradient(transparent, rgb(7 24 17 / 54%)); }}
    .media img {{ width: 100%; height: 100%; display: block; object-fit: cover; transition: transform .5s ease; }}
    .card:hover .media img {{ transform: scale(1.045); }}
    .time-chip {{ position: absolute; z-index: 1; left: 13px; bottom: 11px; color: white; font-size: .8rem; font-weight: 800; text-shadow: 0 1px 5px rgb(0 0 0 / 40%); }}
    .placeholder {{ height: 100%; display: grid; place-items: center; color: var(--muted); }}
    .details {{ display: flex; min-width: 0; flex-direction: column; gap: 9px; padding: 14px; transition: background .2s ease; }}
    .card:hover .details {{ background: rgb(237 246 239 / 52%); }}
    .capture-date {{ order: -1; margin: 0; color: var(--muted); font-size: .77rem; font-weight: 600; }}
    .card .species-name {{ font-size: clamp(1.2rem, 1.5vw, 1.45rem); line-height: 1.08; }}
    .card .scientific-name {{ margin-top: 4px; font-size: .82rem; }}
    .card .classification-stats {{ margin-top: 10px; border-radius: 11px; }}
    .card .classification-stats div {{ padding: 7px 8px 8px; }}
    .card .classification-stats dt {{ font-size: .66rem; }}
    .card .classification-stats dd {{ font-size: .84rem; }}
    .card .empty-frame-note {{ margin-top: 6px; font-size: .84rem; line-height: 1.35; }}
    .classification-pending .species-name {{ color: var(--muted); }}
    .card .actions {{ gap: 6px; margin-top: auto; border-top: 1px solid rgb(23 54 43 / 8%); padding-top: 10px; }}
    .card .actions .primary-action, .card .actions .secondary-action {{ min-height: 36px; padding: 7px 10px; font-size: .83rem; }}
    .actions .primary-action {{ flex: 1 1 104px; }}
    .actions .delete-button {{ flex: 0 0 36px; width: 36px; min-height: 36px; margin-left: auto; }}
    .card .star-button, .card .delete-button {{ z-index: 3; }}
    .card-utility-actions {{ justify-content: flex-end; }}
    .star-button-card {{ top: 10px; right: 10px; min-height: 34px; padding: 6px 10px; font-size: .8rem; }}
    .empty, .no-results {{ grid-column: 1 / -1; text-align: center; border: 1px dashed #aab6ac; border-radius: 22px; padding: 58px 30px; background: rgb(255 253 248 / 65%); }}
    .empty-icon {{ font-size: 3rem; color: var(--leaf); }}
    .empty h2, .no-results h2 {{ margin: 4px 0 0; font-size: 1.6rem; font-weight: 750; line-height: 1.2; }}
    .empty p {{ margin-bottom: 0; color: var(--muted); }}
    @container archive (min-width: 578px) {{
      .species-row {{ grid-template-columns: calc((100cqw - 18px) / 2) minmax(0, 1fr); }}
    }}
    @container archive (min-width: 876px) {{
      .species-row {{ grid-template-columns: calc((100cqw - 36px) / 3) minmax(0, 1fr); }}
    }}
    @container archive (min-width: 1174px) {{
      .species-row {{ grid-template-columns: calc((100cqw - 54px) / 4) minmax(0, 1fr); }}
    }}
    @media (max-width: 900px) {{
      .toolbar {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .reset-button {{ justify-self: start; }}
    }}
    @media (max-width: 760px) and (orientation: portrait), (max-width: 520px) {{
      .species-row {{ grid-template-columns: minmax(0, 1fr); grid-template-rows: auto 154px; }}
      .species-row .species-card {{ width: 100%; }}
      .activity-chart {{ min-height: 0; border-top: 0; border-left: 0; padding-left: 0; }}
    }}
    @media (max-width: 560px) {{
      .hero::after {{ inset: -15rem -20rem auto auto; width: 38rem; aspect-ratio: 1.7; }}
      .site-header {{ padding: 20px 18px 16px; }}
      .site-links a {{ min-height: 38px; padding: 7px 12px; font-size: .88rem; }}
      h1 {{ font-size: clamp(2.5rem, 13vw, 3.7rem); }}
      .archive-shell {{ padding: 12px 14px 56px; }}
      .species-section {{ padding: 14px; }}
      .species-section-header {{ align-items: start; }}
      .toolbar {{ grid-template-columns: 1fr 1fr; gap: 8px; padding: 9px; }}
      .date-range {{ grid-column: 1 / -1; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
      .date-control input[type="date"] {{ padding-inline: 10px 6px; font-size: .92rem; }}
      .control-wide {{ grid-column: 1 / -1; }}
      .gallery {{ gap: 14px; }}
      .pagination {{ grid-template-columns: 1fr auto; }}
      .pagination-page {{ grid-row: 2; grid-column: 1 / -1; }}
      .pagination-actions {{ justify-content: flex-end; }}
      .card {{ border-radius: 16px; }}
      .actions .primary-action {{ flex: 1 1 auto; }}
      .actions .delete-button {{ margin-left: 0; }}
    }}
  </style>
</head>
<body>
  <div class="hero">
    <header class="site-header">
      <div>
        <p class="eyebrow">{location}</p>
        <h1>Backyard Birds</h1>
      </div>
      <nav class="site-links" aria-label="Site navigation">
        <a href="/about">About</a>
      </nav>
    </header>
  </div>
  <main class="archive-shell">
    <section class="species-section" aria-labelledby="species-section-title">
      <div class="species-section-header">
        <div>
          <p class="eyebrow">Species library</p>
          <h2 id="species-section-title">Birds we’ve seen</h2>
        </div>
        <div class="species-section-actions">
          <button class="clear-species" type="button" data-clear-species data-visitor-kind="bird" hidden>Show all videos</button>
          <button class="section-toggle" type="button" data-section-toggle
            data-section-label="Birds we’ve seen" aria-expanded="false"
            aria-controls="species-section-content" aria-label="Expand Birds we’ve seen">
            <svg aria-hidden="true" viewBox="0 0 16 16" fill="none"
              stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
              stroke-linejoin="round"><path d="m4 6 4 4 4-4"/></svg>
          </button>
        </div>
      </div>
      <div id="species-section-content" data-section-content hidden>
        {species_content}
      </div>
    </section>
    <section class="species-section animal-section" aria-labelledby="animal-section-title">
      <div class="species-section-header">
        <div>
          <p class="eyebrow">Wildlife log</p>
          <h2 id="animal-section-title">Other feeder visitors</h2>
        </div>
        <div class="species-section-actions">
          <button class="clear-species" type="button" data-clear-species data-visitor-kind="animal" hidden>Show all animal videos</button>
          <button class="section-toggle" type="button" data-section-toggle
            data-section-label="Other feeder visitors" aria-expanded="false"
            aria-controls="animal-section-content" aria-label="Expand Other feeder visitors">
            <svg aria-hidden="true" viewBox="0 0 16 16" fill="none"
              stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
              stroke-linejoin="round"><path d="m4 6 4 4 4-4"/></svg>
          </button>
        </div>
      </div>
      <div id="animal-section-content" data-section-content hidden>
        {animal_content}
      </div>
    </section>
    <section class="toolbar" aria-label="Archive controls">
      <div class="date-range" role="group" aria-label="Capture date range">
        <div class="control date-control" data-date-control data-has-value="false">
          <label for="date-from">From</label>
          <input id="date-from" type="date" data-date-from>
          <span class="date-empty-state" aria-hidden="true">Any date</span>
        </div>
        <div class="control date-control" data-date-control data-has-value="false">
          <label for="date-to">To</label>
          <input id="date-to" type="date" data-date-to>
          <span class="date-empty-state" aria-hidden="true">Any date</span>
        </div>
      </div>
      <div class="control control-wide">
        <label for="archive-sort">Sort by</label>
        <select id="archive-sort" data-sort>
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
          <option value="starred">Most starred</option>
        </select>
      </div>
      <div class="control control-wide">
        <label for="archive-filter">Filter</label>
        <select id="archive-filter" data-filter>
          <option value="all">All captures</option>
          <option value="starred">Has stars</option>
          <option value="identified">Identified species</option>
          <option value="unclassified">Awaiting identification</option>
          <option value="birds" selected>Bird detected</option>
          <option value="multiple-birds">Multiple birds detected</option>
          <option value="animals">Other animal detected</option>
          <option value="empty">No animal detected</option>
        </select>
      </div>
      <button class="reset-button" type="button" data-reset-controls>Reset</button>
    </section>
    <section class="gallery" data-gallery aria-live="polite">{cards}{empty}</section>
    <section class="no-results" data-no-results hidden>
      <h2>No captures match</h2>
      <p>Try widening the date range or choosing a different filter.</p>
    </section>
    <nav class="pagination" data-pagination aria-label="Gallery pages"{pagination_hidden}>
      <p class="pagination-summary" data-page-status>Showing 1–{initial_page_end} of {initial_result_total}</p>
      <p class="pagination-page" data-page-number aria-live="polite">Page 1 of {initial_page_count}</p>
      <div class="pagination-actions">
        <button class="page-button" type="button" data-page-previous disabled>Previous</button>
        <button class="page-button" type="button" data-page-next{next_page_disabled}>Next</button>
      </div>
    </nav>
  </main>
{script}
</body>
</html>
""".format(
        shared_styles=_shared_styles(),
        cards="".join(cards),
        empty=empty,
        initial_result_total=initial_result_total,
        initial_page_end=min(24, initial_result_total),
        initial_page_count=initial_page_count,
        pagination_hidden=" hidden" if initial_result_total == 0 else "",
        next_page_disabled=" disabled" if initial_page_count == 1 else "",
        location="Toronto" if public_read_only else "Toronto",
        species_content=species_content,
        animal_content=animal_content,
        script=_shared_script(csrf_token, deletes_enabled=not public_read_only),
    )
    return document.encode("utf-8")


def _render_watch(
    pair: GalleryPair,
    csrf_token: str,
    *,
    previous_pair: GalleryPair | None = None,
    next_pair: GalleryPair | None = None,
    autoplay: bool = False,
    public_read_only: bool = False,
) -> bytes:
    assert pair.video_path is not None
    share_location = "Toronto" if public_read_only else "Toronto"
    poster = (
        f' poster="{_media_url(pair.snapshot_path)}"' if pair.snapshot_path else ""
    )
    autoplay_attribute = " autoplay" if autoplay else ""
    navigation: list[str] = []
    for neighbor, direction, symbol in (
        (previous_pair, "previous", "‹"),
        (next_pair, "next", "›"),
    ):
        if neighbor is None or neighbor.video_path is None:
            continue
        label = f"{direction.title()} capture: {neighbor.captured_label}"
        neighbor_url = _watch_url(neighbor.video_path) + "?autoplay=1"
        navigation.append(
            f'<a class="gallery-nav gallery-nav-{direction}" '
            f'href="{html.escape(neighbor_url, quote=True)}" '
            f'aria-label="{html.escape(label, quote=True)}" '
            f'title="{html.escape(label, quote=True)}">'
            f'<span aria-hidden="true">{symbol}</span></a>'
        )
    navigation_html = "".join(navigation)
    watch_title = pair.common_name or pair.date_label
    scientific_name = (
        f'<p class="watch-scientific"><i>{html.escape(pair.scientific_name)}</i></p>'
        if pair.scientific_name
        else ""
    )
    identity_kicker = "Visitor" if pair.common_name else "Capture"
    next_video = ""
    next_script = ""
    player_sizing_script = """
<script>
  (() => {
    const video = document.querySelector("[data-watch-video]");
    const player = video?.closest("[data-video-player]");
    if (!video || !player) return;

    let resizeFrame = null;
    const fitPlayerToVideo = () => {
      if (!video.videoWidth || !video.videoHeight) return;

      const aspectRatio = video.videoWidth / video.videoHeight;
      player.style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;

      // Measure the full grid track before applying the height-aware width.
      // Capping the video element's height while leaving its frame full-width
      // is what creates letterboxing in Safari.
      player.style.removeProperty("width");
      const availableWidth = player.getBoundingClientRect().width;
      const viewportHeight = window.innerHeight;
      const maximumHeight = window.matchMedia("(max-width: 980px)").matches
        ? viewportHeight * 0.68
        : viewportHeight - 104;
      const fittedWidth = Math.min(
        availableWidth,
        Math.max(1, maximumHeight) * aspectRatio
      );
      player.style.width = `${fittedWidth}px`;
    };
    const schedulePlayerFit = () => {
      if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        fitPlayerToVideo();
      });
    };

    video.addEventListener("loadedmetadata", fitPlayerToVideo);
    window.addEventListener("resize", schedulePlayerFit);
    window.visualViewport?.addEventListener("resize", schedulePlayerFit);
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) fitPlayerToVideo();
  })();
</script>
"""
    if next_pair is not None and next_pair.video_path is not None:
        next_url = _watch_url(next_pair.video_path) + "?autoplay=1"
        next_title = next_pair.common_name or "Species pending"
        next_sex = {
            "male": "Male",
            "female": "Female",
            "indeterminate": "Unknown sex",
        }.get(next_pair.sex or "")
        if (next_pair.is_bird or _is_animal_visitor(next_pair)) and next_sex:
            next_title = f"{next_title} · {next_sex}"
        next_subtitle = f"{next_pair.date_label} · {next_pair.time_label}"
        next_video = (
            '<aside class="next-video" data-next-video '
            f'data-next-url="{html.escape(next_url, quote=True)}" hidden>'
            '<div class="next-video-copy">'
            '<span>Up next</span>'
            f'<strong>{html.escape(next_title)}</strong>'
            f'<small>{html.escape(next_subtitle)}</small>'
            '</div>'
            '<p>Playing in <strong data-countdown>5</strong></p>'
            '<div class="next-video-progress" aria-hidden="true">'
            '<span data-countdown-bar></span></div>'
            '<div class="next-video-actions">'
            '<button type="button" data-cancel-next>Cancel</button>'
            '<button type="button" class="play-next-button" data-play-next>'
            'Play now</button>'
            '</div>'
            '</aside>'
        )
        next_script = """
<script>
  (() => {
    const video = document.querySelector("[data-watch-video]");
    if (!video) return;

    const countdownSeconds = 5;
    let countdownRemaining = countdownSeconds;
    let countdownTimer = null;
    let countdownDeadline = null;
    let cancelled = false;
    let advancing = false;
    const currentPanel = () => document.querySelector("[data-next-video]");
    const stopCountdownTimer = () => {
      if (countdownTimer !== null) window.clearInterval(countdownTimer);
      countdownTimer = null;
      countdownDeadline = null;
    };
    const renderCountdown = () => {
      const panel = currentPanel();
      const countdown = panel?.querySelector("[data-countdown]");
      const bar = panel?.querySelector("[data-countdown-bar]");
      if (countdown) countdown.textContent = String(countdownRemaining);
      if (bar) {
        bar.style.transform = `scaleX(${countdownRemaining / countdownSeconds})`;
      }
    };
    const startCountdownTimer = () => {
      if (countdownTimer !== null || cancelled || advancing) return;
      countdownDeadline = Date.now() + countdownRemaining * 1000;
      countdownTimer = window.setInterval(() => {
        countdownRemaining = Math.max(
          0,
          Math.ceil((countdownDeadline - Date.now()) / 1000)
        );
        renderCountdown();
        if (countdownRemaining === 0) {
          stopCountdownTimer();
          advance();
        }
      }, 250);
    };
    const showCountdown = () => {
      if (cancelled || advancing) return;
      const panel = currentPanel();
      if (!panel) return;
      if (panel.hidden) {
        countdownRemaining = countdownSeconds;
        renderCountdown();
        panel.hidden = false;
      }
      startCountdownTimer();
    };
    const cancelCountdown = () => {
      cancelled = true;
      stopCountdownTimer();
      const panel = currentPanel();
      if (panel) panel.hidden = true;
    };
    const updatePage = (nextDocument, nextUrl) => {
      const nextVideo = nextDocument.querySelector("[data-watch-video]");
      const nextSource = nextVideo?.querySelector("source");
      const source = video.querySelector("source");
      const currentHeader = document.querySelector(".watch-header");
      const nextHeader = nextDocument.querySelector(".watch-header");
      const currentInfo = document.querySelector(".watch-info");
      const nextInfo = nextDocument.querySelector(".watch-info");
      if (!nextVideo || !nextSource || !source || !currentHeader || !nextHeader ||
          !currentInfo || !nextInfo) {
        throw new Error("Next video page was incomplete");
      }

      document.title = nextDocument.title;
      currentHeader.replaceWith(document.importNode(nextHeader, true));
      currentInfo.replaceWith(document.importNode(nextInfo, true));
      document.querySelectorAll(".gallery-nav").forEach((link) => link.remove());
      const main = document.querySelector(".watch-shell");
      nextDocument.querySelectorAll(".gallery-nav").forEach((link) => {
        main.before(document.importNode(link, true));
      });

      const player = document.querySelector(".player");
      currentPanel()?.remove();
      const nextPanel = nextDocument.querySelector("[data-next-video]");
      if (nextPanel) player.append(document.importNode(nextPanel, true));

      const poster = nextVideo.getAttribute("poster");
      if (poster) video.setAttribute("poster", poster);
      else video.removeAttribute("poster");
      source.setAttribute("src", nextSource.getAttribute("src"));

      const historyUrl = new URL(nextUrl, window.location.href);
      historyUrl.searchParams.delete("autoplay");
      window.history.pushState({}, "", historyUrl);
      video.load();
    };
    const advance = async () => {
      if (cancelled || advancing) return;
      const panel = currentPanel();
      const nextUrl = panel?.dataset.nextUrl;
      if (!panel || !nextUrl) return;
      advancing = true;
      const countdown = panel.querySelector("[data-countdown]");
      if (countdown) countdown.textContent = "0";
      const bar = panel.querySelector("[data-countdown-bar]");
      if (bar) bar.style.transform = "scaleX(0)";
      stopCountdownTimer();
      try {
        const response = await fetch(nextUrl);
        if (!response.ok) throw new Error("Could not load next video");
        const nextDocument = new DOMParser().parseFromString(
          await response.text(),
          "text/html"
        );
        updatePage(nextDocument, nextUrl);
        cancelled = false;
        countdownRemaining = countdownSeconds;
        await video.play();
        advancing = false;
      } catch (error) {
        window.location.assign(nextUrl);
      }
    };
    const updateCountdown = () => {
      if (cancelled || !Number.isFinite(video.duration)) return;
      const panel = currentPanel();
      if (!panel) return;
      const playbackRemaining = Math.max(0, video.duration - video.currentTime);
      if (playbackRemaining > countdownSeconds) {
        panel.hidden = true;
        stopCountdownTimer();
        countdownRemaining = countdownSeconds;
        renderCountdown();
        return;
      }
      showCountdown();
    };
    const resetCountdown = () => {
      if (Number.isFinite(video.duration) &&
          video.currentTime < Math.max(0, video.duration - countdownSeconds)) {
        cancelled = false;
        advancing = false;
        const panel = currentPanel();
        if (panel) panel.hidden = true;
        stopCountdownTimer();
        countdownRemaining = countdownSeconds;
        renderCountdown();
      }
    };

    video.addEventListener("timeupdate", updateCountdown);
    video.addEventListener("ended", showCountdown);
    video.addEventListener("seeked", () => {
      resetCountdown();
      updateCountdown();
    });
    video.addEventListener("play", () => {
      resetCountdown();
      const panel = currentPanel();
      if (panel && !panel.hidden) startCountdownTimer();
    });
    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-cancel-next]")) cancelCountdown();
      if (event.target.closest("[data-play-next]")) {
        cancelled = false;
        advance();
      }
    });
    window.addEventListener("pagehide", stopCountdownTimer);
    window.addEventListener("popstate", () => window.location.reload());
  })();
</script>
"""
    document = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>{date} — Backyard Birds</title>
  <style>
{shared_styles}
    .watch-shell {{ width: min(1640px, 100%); margin: 0 auto; padding: 18px 30px 48px; }}
    .watch-header {{ display: flex; min-height: 44px; gap: 18px; align-items: center; justify-content: space-between; margin-bottom: 12px; }}
    .watch-capture-label {{ margin: 0; color: var(--leaf); font-size: .9rem; font-weight: 750; }}
    .watch-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(300px, 330px); gap: 18px; align-items: stretch; }}
    .watch-scientific {{ margin: 5px 0 0; color: var(--muted); font-size: .93rem; }}
    .player {{ position: relative; width: 100%; align-self: start; justify-self: center; overflow: hidden; border: 1px solid rgb(255 255 255 / 10%); border-radius: 24px; background: #09140f; box-shadow: 0 24px 60px rgb(15 43 32 / 22%); }}
    video {{ display: block; width: 100%; height: 100%; object-fit: cover; background: #09140f; }}
    .next-video {{ position: absolute; right: 16px; bottom: 16px; width: min(290px, calc(100% - 32px)); padding: 14px; border: 1px solid rgb(255 255 255 / 28%); border-radius: 14px; color: #fff; background: rgb(9 20 15 / 78%); box-shadow: 0 14px 34px rgb(0 0 0 / 30%); backdrop-filter: blur(22px) saturate(130%); }}
    .next-video[hidden] {{ display: none; }}
    .next-video-copy {{ display: grid; gap: 2px; }}
    .next-video-copy span {{ color: #b9d7c9; font-size: .72rem; font-weight: 800; text-transform: uppercase; }}
    .next-video-copy strong {{ font-size: 1.02rem; }}
    .next-video-copy small {{ color: #d7e6de; }}
    .next-video > p {{ margin: 10px 0 6px; font-size: .92rem; }}
    .next-video-progress {{ height: 3px; margin-bottom: 10px; overflow: hidden; border-radius: 999px; background: rgb(255 255 255 / 20%); }}
    .next-video-progress span {{ display: block; width: 100%; height: 100%; border-radius: inherit; background: #b9d7c9; transform: scaleX(1); transform-origin: left center; transition: transform 1s linear; }}
    .next-video-actions {{ display: flex; gap: 8px; }}
    .next-video-actions button {{ flex: 1; min-height: 36px; display: inline-flex; align-items: center; justify-content: center; padding: 7px 10px; border: 1px solid rgb(255 255 255 / 24%); border-radius: 999px; color: #fff; background: transparent; font: inherit; font-size: .92rem; font-weight: 800; cursor: pointer; }}
    .next-video-actions .play-next-button {{ color: #10281e; background: #fff; }}
    .gallery-nav {{ position: fixed; z-index: 4; top: 50%; display: grid; width: 48px; height: 64px; place-items: center; border: 1px solid rgb(23 54 43 / 14%); border-radius: 999px; color: var(--ink); background: rgb(255 254 249 / 88%); box-shadow: 0 12px 30px rgb(15 43 32 / 18%); font-size: 2.5rem; line-height: 1; text-decoration: none; transform: translateY(-50%); backdrop-filter: blur(10px); }}
    .gallery-nav:hover, .gallery-nav:focus {{ color: #fff; border-color: var(--leaf); background: var(--leaf); }}
    .gallery-nav-previous {{ left: 18px; }}
    .gallery-nav-next {{ right: 18px; }}
    .watch-info {{ display: flex; min-width: 0; flex-direction: column; padding: 24px; border: 1px solid rgb(23 54 43 / 9%); border-radius: 22px; background: rgb(255 254 249 / 78%); box-shadow: var(--shadow); }}
    .watch-info-kicker {{ margin: 0 0 5px; color: var(--leaf); font-size: .72rem; font-weight: 800; text-transform: uppercase; }}
    .watch-info h1 {{ margin: 0; color: var(--leaf-dark); font-size: clamp(1.8rem, 2.5vw, 2.55rem); font-weight: 800; line-height: 1.02; }}
    .watch-info .classification {{ margin-top: 4px; }}
    .watch-info .classification-stats {{ margin-top: 18px; }}
    .watch-info .classification-notes {{ grid-template-columns: 1fr; }}
    .watch-info .classification-fact {{ padding: 14px; }}
    .watch-info .classification-fact p {{ font-size: 1rem; line-height: 1.48; }}
    .watch-info .observation {{ padding-inline: 2px; }}
    .watch-actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 9px; margin-top: auto; padding-top: 20px; }}
    .watch-actions > * {{ width: 100%; }}
    .star-button-large, .watch-actions .secondary-action, .delete-button-large {{ min-height: 46px; padding-inline: 14px; }}
    @media (min-width: 1301px) {{
      .watch-shell {{ padding-inline: 64px; }}
      .gallery-nav {{ width: 44px; height: 60px; font-size: 2.25rem; }}
      .gallery-nav-previous {{ left: 10px; }}
      .gallery-nav-next {{ right: 10px; }}
    }}
    @media (min-width: 561px) and (max-width: 1300px) {{
      .gallery-nav {{ top: auto; bottom: 18px; width: 44px; height: 44px; font-size: 2rem; transform: none; }}
      .gallery-nav-previous {{ left: 14px; }}
      .gallery-nav-next {{ right: 14px; }}
    }}
    @media (max-width: 980px) {{
      .watch-shell {{ width: min(1160px, 100%); }}
      .watch-layout {{ grid-template-columns: 1fr; }}
      .watch-info {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px 28px; padding: 20px; }}
      .watch-identity, .watch-info .classification {{ grid-column: 1; }}
      .watch-actions {{ grid-column: 2; grid-row: 1 / span 2; align-self: end; min-width: 300px; padding-top: 0; }}
    }}
    @media (max-width: 560px) {{
      .watch-shell {{ padding: 18px 14px 42px; }}
      .watch-header {{ align-items: center; }}
      .watch-capture-label {{ font-size: .8rem; }}
      .watch-header .back-button {{ min-height: 38px; padding: 7px 11px; font-size: .88rem; }}
      .player {{ border-radius: 16px; }}
      .next-video {{ right: 10px; bottom: 10px; width: min(290px, calc(100% - 20px)); }}
      .watch-info {{ display: flex; padding: 18px 15px; border-radius: 16px; }}
      .watch-info .classification-notes {{ grid-template-columns: 1fr; }}
      .watch-actions {{ width: 100%; padding-top: 18px; }}
      .gallery-nav {{ top: auto; bottom: 18px; width: 44px; height: 44px; font-size: 2rem; transform: none; }}
      .gallery-nav-previous {{ left: 14px; }}
      .gallery-nav-next {{ right: 14px; }}
    }}
  </style>
</head>
<body>
  {navigation}
  <main class="watch-shell">
    <header class="watch-header">
      <p class="watch-capture-label">{date} · {time}</p>
      <a class="back-button" href="/">← Back to gallery</a>
    </header>
    <div class="watch-layout">
      <div class="player" data-video-player>
        <video controls playsinline preload="metadata" data-watch-video{autoplay_attribute}{poster}>
          <source src="{media_url}" type="video/mp4">
          Your browser does not support HTML video.
        </video>
        {next_video}
      </div>
      <aside class="watch-info" aria-label="Capture details">
        <div class="watch-identity">
          <p class="watch-info-kicker">{identity_kicker}</p>
          <h1>{watch_title}</h1>
          {scientific_name}
        </div>
        {classification}
        <div class="watch-actions">
          {star_button}
          {share_button}
        </div>
      </aside>
    </div>
  </main>
{script}
{player_sizing_script}
{next_script}
</body>
</html>
""".format(
        shared_styles=_shared_styles(),
        date=html.escape(pair.date_label),
        time=html.escape(pair.time_label),
        watch_title=html.escape(watch_title),
        scientific_name=scientific_name,
        identity_kicker=identity_kicker,
        navigation=navigation_html,
        autoplay_attribute=autoplay_attribute,
        poster=poster,
        media_url=_media_url(pair.video_path),
        next_video=next_video,
        classification=_classification_label(pair, include_heading=False),
        star_button=_star_button(pair, large=True),
        share_button=_share_button(pair, location=share_location, preload=True),
        script=_shared_script(csrf_token, deletes_enabled=not public_read_only),
        player_sizing_script=player_sizing_script,
        next_script=next_script,
    )
    return document.encode("utf-8")


def _json_pairs(pairs: list[GalleryPair]) -> bytes:
    payload = []
    for pair in pairs:
        payload.append(
            {
                "camera_id": pair.source_id,
                "pair_key": pair.pair_key,
                "captured": pair.captured_label,
                "date": pair.date_label,
                "time": pair.time_label,
                "starred": pair.starred,
                "star_count": pair.star_count,
                "classification": (
                    {
                        "is_bird": pair.is_bird,
                        "is_animal": _is_animal_visitor(pair),
                        "common_name": pair.common_name,
                        "scientific_name": pair.scientific_name,
                        "certainty": pair.certainty,
                        "notes": pair.classification_notes,
                        "sex": pair.sex,
                        "age_class": pair.age_class,
                        "bird_count": pair.bird_count,
                        "behavior": pair.behavior,
                        "sex_evidence": pair.sex_evidence,
                        "age_evidence": pair.age_evidence,
                        "interesting_fact": pair.interesting_fact,
                    }
                    if pair.common_name is not None
                    else None
                ),
                "snapshot_url": (
                    _media_url(pair.snapshot_path) if pair.snapshot_path else None
                ),
                "video_url": _media_url(pair.video_path) if pair.video_path else None,
                "watch_url": _watch_url(pair.video_path) if pair.video_path else None,
                "download_url": (
                    _download_url(pair.video_path) if pair.video_path else None
                ),
            }
        )
    return json.dumps({"media": payload}, separators=(",", ":")).encode("utf-8")


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if header is None:
        return None
    if not header.startswith("bytes=") or "," in header:
        raise ValueError("unsupported range")
    start_text, separator, end_text = header[6:].partition("-")
    if not separator:
        raise ValueError("invalid range")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("range outside file")
    return start, min(end, size - 1)


def make_handler(
    library_root: Path,
    *,
    allowed_hosts: set[str] | None = None,
    mobile_preparer: MobileVideoPreparer | None = None,
    public_read_only: bool = False,
) -> type[BaseHTTPRequestHandler]:
    root = library_root.expanduser().resolve()
    media_root = root / "media"
    favicon = Path(__file__).with_name("favicon.svg").read_bytes()
    about_photo_path = Path(__file__).with_name("about-feeder.jpg")
    about_pi_photo_path = Path(__file__).with_name("about-raspberry-pi.jpg")
    about_amazon_photo_path = Path(__file__).with_name("about-amazon.png")
    about_subscription_photo_path = Path(__file__).with_name("about-subscription.jpg")
    csrf_token = secrets.token_urlsafe(32)
    phone_videos = mobile_preparer or MobileVideoPreparer(root)
    host_allowlist = {
        host.rstrip(".").lower()
        for host in (allowed_hosts or {"127.0.0.1", "localhost", "::1"})
    }
    if not public_read_only:
        _ensure_gallery_schema(root)

    class GalleryHandler(BaseHTTPRequestHandler):
        server_version = "BirdFeederGallery"
        sys_version = ""
        _head_only = False

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._host_is_allowed():
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send_bytes(
                    HTTPStatus.OK,
                    _render_index(
                        list_pairs(root, ensure_schema=not public_read_only),
                        csrf_token,
                        public_read_only=public_read_only,
                    ),
                    "text/html; charset=utf-8",
                )
            elif path == "/about":
                self._send_bytes(
                    HTTPStatus.OK,
                    _render_about(
                        photo_available=about_photo_path.is_file(),
                        pi_photo_available=about_pi_photo_path.is_file(),
                        amazon_photo_available=about_amazon_photo_path.is_file(),
                        subscription_photo_available=about_subscription_photo_path.is_file(),
                        pairs=list_pairs(root, ensure_schema=not public_read_only),
                    ),
                    "text/html; charset=utf-8",
                )
            elif path == "/about-feeder.jpg" and about_photo_path.is_file():
                self._send_bytes(
                    HTTPStatus.OK,
                    about_photo_path.read_bytes(),
                    "image/jpeg",
                )
            elif path == "/about-raspberry-pi.jpg" and about_pi_photo_path.is_file():
                self._send_bytes(
                    HTTPStatus.OK,
                    about_pi_photo_path.read_bytes(),
                    "image/jpeg",
                )
            elif path == "/about-amazon.png" and about_amazon_photo_path.is_file():
                self._send_bytes(
                    HTTPStatus.OK,
                    about_amazon_photo_path.read_bytes(),
                    "image/png",
                )
            elif path == "/about-subscription.jpg" and about_subscription_photo_path.is_file():
                self._send_bytes(
                    HTTPStatus.OK,
                    about_subscription_photo_path.read_bytes(),
                    "image/jpeg",
                )
            elif path == "/api/media":
                if public_read_only:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    _json_pairs(
                        list_pairs(root, ensure_schema=not public_read_only)
                    ),
                    "application/json",
                )
            elif path == "/healthz":
                if public_read_only:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                pairs = list_pairs(root, limit=1000)
                body = json.dumps(
                    {
                        "status": "ok",
                        "items": len(pairs),
                        "starred": sum(pair.starred for pair in pairs),
                        "stars": sum(pair.star_count for pair in pairs),
                    }
                ).encode("utf-8")
                self._send_bytes(HTTPStatus.OK, body, "application/json")
            elif path in ("/favicon.svg", "/favicon.ico"):
                self._send_bytes(HTTPStatus.OK, favicon, "image/svg+xml")
            elif public_read_only and path == "/robots.txt":
                self._send_bytes(
                    HTTPStatus.OK,
                    b"User-agent: *\nDisallow: /\n",
                    "text/plain; charset=utf-8",
                )
            elif path.startswith("/watch/"):
                video_path = unquote(path[len("/watch/") :])
                pair = _find_video_pair(
                    root,
                    video_path,
                    ensure_schema=not public_read_only,
                )
                if pair is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    previous_pair, next_pair = _video_neighbors(
                        root,
                        video_path,
                        ensure_schema=not public_read_only,
                    )
                    self._send_bytes(
                        HTTPStatus.OK,
                        _render_watch(
                            pair,
                            csrf_token,
                            previous_pair=previous_pair,
                            next_pair=next_pair,
                            autoplay=urlsplit(self.path).query == "autoplay=1",
                            public_read_only=public_read_only,
                        ),
                        "text/html; charset=utf-8",
                    )
            elif path.startswith("/media/"):
                self._send_media(path[len("/media/") :], download=False)
            elif path.startswith("/share/"):
                self._send_media(
                    path[len("/share/") :],
                    download=False,
                    prepare_mobile=True,
                    require_prepared=True,
                )
            elif path.startswith("/download/"):
                self._send_media(
                    path[len("/download/") :], download=True, prepare_mobile=True
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._head_only = True
            try:
                self.do_GET()
            finally:
                self._head_only = False

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._host_is_allowed():
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
                return
            path = urlsplit(self.path).path
            if public_read_only and path != "/api/stars":
                self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
                self.send_header("Allow", "GET, HEAD")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self._send_security_headers()
                self.end_headers()
                return
            if not self._origin_is_same_host():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            public_star_request = public_read_only and path == "/api/stars"
            if not public_star_request and not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), csrf_token
            ):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if path not in ("/api/stars", "/api/delete"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if content_length < 1 or content_length > 4096:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                source_id = payload["camera_id"]
                pair_key = payload["pair_key"]
                if not isinstance(source_id, str) or not isinstance(pair_key, str):
                    raise ValueError("invalid capture identity")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/stars":
                # Tabs opened before the toggle release sent no desired state;
                # retain their original add-one behavior during the transition.
                starred = payload.get("starred", True)
                if not isinstance(starred, bool):
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                try:
                    star_count = _set_star_contribution(
                        root, source_id, pair_key, starred
                    )
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                except FileNotFoundError:
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                except sqlite3.DatabaseError:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                response = {"star_count": star_count}
            else:
                if payload.get("confirmed") is not True:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                try:
                    deleted = _delete_pair(root, source_id, pair_key)
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                except (FileNotFoundError, OSError, sqlite3.DatabaseError, ValueError):
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                response = {"deleted": True, **deleted}

            self._send_bytes(
                HTTPStatus.OK,
                json.dumps(response).encode("utf-8"),
                "application/json",
            )

        def _reject_unsupported_method(self) -> None:
            if not self._host_is_allowed():
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
                return
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()

        do_DELETE = _reject_unsupported_method  # noqa: N815
        do_OPTIONS = _reject_unsupported_method  # noqa: N815
        do_PATCH = _reject_unsupported_method  # noqa: N815
        do_PUT = _reject_unsupported_method  # noqa: N815
        do_TRACE = _reject_unsupported_method  # noqa: N815

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            if not self._head_only:
                self.wfile.write(body)

        def _host_is_allowed(self) -> bool:
            raw_host = self.headers.get("Host", "")
            try:
                parsed = urlsplit("//" + raw_host)
                if parsed.username is not None or parsed.password is not None:
                    return False
                hostname = parsed.hostname
                _ = parsed.port
            except ValueError:
                return False
            return bool(hostname and hostname.rstrip(".").lower() in host_allowlist)

        def _origin_is_same_host(self) -> bool:
            raw_host = self.headers.get("Host", "").lower()
            origin = self.headers.get("Origin", "")
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return False
            return (
                parsed.scheme in {"http", "https"}
                and parsed.netloc.lower() == raw_host
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )

        def _send_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=()",
            )
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            if public_read_only:
                self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'; connect-src 'self'; "
                "font-src 'self'; "
                "img-src 'self' data:; media-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'",
            )

        def _send_media(
            self,
            encoded_path: str,
            *,
            download: bool,
            prepare_mobile: bool = False,
            require_prepared: bool = False,
        ) -> None:
            relative = PurePosixPath(unquote(encoded_path))
            if relative.is_absolute() or ".." in relative.parts:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = media_root.joinpath(*relative.parts)
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if media_root not in resolved.parents or not resolved.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                connection = _connect_catalog(root)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                exists = connection.execute(
                    "SELECT 1 FROM media WHERE relative_path = ?",
                    (relative.as_posix(),),
                ).fetchone()
            finally:
                connection.close()
            if exists is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            if prepare_mobile and relative.suffix.casefold() == ".mp4":
                try:
                    if public_read_only:
                        cached = phone_videos.cached(relative.as_posix())
                        if cached is not None:
                            resolved = cached
                        elif require_prepared:
                            self.send_error(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                "The phone-compatible video is not ready",
                            )
                            return
                    else:
                        resolved = phone_videos.prepare(relative.as_posix())
                except MobileVideoError:
                    self.send_error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "The phone-compatible video could not be prepared",
                    )
                    return

            size = resolved.stat().st_size
            try:
                requested_range = _parse_range(self.headers.get("Range"), size)
            except (ValueError, TypeError):
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                if public_read_only:
                    self.send_header("Cache-Control", "no-store")
                self._send_security_headers()
                self.end_headers()
                return

            content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            if requested_range is None:
                start, end = 0, size - 1
                status = HTTPStatus.OK
            else:
                start, end = requested_range
                status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self._send_security_headers()
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{resolved.name}"',
                )
            if public_read_only and prepare_mobile and not download:
                # The public gallery never generates this derivative and the
                # response is private to the visitor's browser. Allow Safari
                # to reuse the already-prepared attachment when the page is
                # restored after sharing to Messages or another app.
                self.send_header("Cache-Control", "private, max-age=3600")
            elif public_read_only:
                self.send_header("Cache-Control", "no-store")
            elif not download:
                self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()

            if self._head_only:
                return
            with resolved.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def log_message(self, format: str, *args: Any) -> None:
            super().log_message(format, *args)

    return GalleryHandler


def serve(
    library_root: Path,
    host: str,
    port: int,
    *,
    allowed_hosts: set[str] | None = None,
    public_read_only: bool = False,
) -> None:
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(
            library_root,
            allowed_hosts=allowed_hosts,
            public_read_only=public_read_only,
        ),
    )
    server.daemon_threads = True
    server.request_queue_size = 32
    print(f"Bird gallery listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the local bird-media gallery")
    parser.add_argument("--library", type=Path, required=True, help="Pi media library")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", type=int, default=8080, help="listen port")
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="accepted HTTP Host name or address; may be repeated",
    )
    parser.add_argument(
        "--public-read-only",
        action="store_true",
        help="disable admin mutations while allowing stars; add public privacy controls",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    allowed_hosts = set(args.allowed_host) if args.allowed_host else None
    serve(
        args.library,
        args.host,
        args.port,
        allowed_hosts=allowed_hosts,
        public_read_only=args.public_read_only,
    )


if __name__ == "__main__":
    main()
