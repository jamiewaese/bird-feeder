"""Classify imported snapshots while enforcing local request and spend limits."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


PROMPT_VERSION = "bird-wildlife-sex-person-privacy-v3"
PROVIDER = "openai"
VISIT_CONTEXT_SECONDS = 120
MAX_VISIT_CONTEXT_IMAGES = 3

FACT_FOCUSES = (
    "seasonal life: migration, winter survival, molt, breeding, or changing food needs at this time of year",
    "local geography: the species' Toronto-area range, abundance, habitat, or whether this is near a range edge",
    "plumage: where its colors come from, molt, camouflage, or a meaningful male/female or age difference",
    "feeding ecology: a bill, foot, tongue, digestive, food-caching, or foraging adaptation",
    "movement: migration distance, route, navigation, flight, dispersal, or year-round residency",
    "communication and social life: song, calls, flocking, territoriality, recognition, or learning",
    "nesting and family life: courtship, nest design, eggs, parental care, or juvenile development",
    "surprising biology: sleep, senses, memory, metabolism, lifespan, predators, or conservation",
)

CLASSIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS classification_attempts (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    response_id TEXT,
    error_message TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    estimated_cost_usd REAL NOT NULL,
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS classification_attempt_month
    ON classification_attempts (attempted_at, provider);
CREATE INDEX IF NOT EXISTS classification_attempt_media
    ON classification_attempts (media_id, provider, model, prompt_version);

CREATE TABLE IF NOT EXISTS classifications (
    media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    response_id TEXT,
    is_bird INTEGER NOT NULL CHECK (is_bird IN (0, 1)),
    common_name TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    certainty TEXT NOT NULL CHECK (certainty IN ('certain', 'likely', 'uncertain')),
    alternatives_json TEXT NOT NULL,
    field_marks_json TEXT NOT NULL,
    notes TEXT NOT NULL,
    sex TEXT NOT NULL CHECK (sex IN ('male', 'female', 'indeterminate', 'not_applicable')),
    age_class TEXT NOT NULL CHECK (age_class IN ('adult', 'juvenile', 'immature', 'indeterminate', 'not_applicable')),
    bird_count INTEGER NOT NULL CHECK (bird_count >= 0),
    behavior TEXT NOT NULL,
    sex_evidence TEXT NOT NULL,
    age_evidence TEXT NOT NULL,
    interesting_fact TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    estimated_cost_usd REAL NOT NULL,
    classified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS classification_species
    ON classifications (common_name, scientific_name);

CREATE TABLE IF NOT EXISTS privacy_deletions (
    source_id TEXT NOT NULL,
    pair_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    response_id TEXT,
    estimated_cost_usd REAL NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (source_id, pair_key)
);
CREATE INDEX IF NOT EXISTS privacy_deletion_month
    ON privacy_deletions (detected_at, provider);
"""

CLASSIFICATION_MIGRATION_COLUMNS = {
    "sex": (
        "TEXT NOT NULL DEFAULT 'indeterminate' "
        "CHECK (sex IN ('male', 'female', 'indeterminate', 'not_applicable'))"
    ),
    "age_class": (
        "TEXT NOT NULL DEFAULT 'indeterminate' "
        "CHECK (age_class IN ('adult', 'juvenile', 'immature', "
        "'indeterminate', 'not_applicable'))"
    ),
    "bird_count": "INTEGER NOT NULL DEFAULT 0 CHECK (bird_count >= 0)",
    "behavior": "TEXT NOT NULL DEFAULT ''",
    "sex_evidence": "TEXT NOT NULL DEFAULT ''",
    "age_evidence": "TEXT NOT NULL DEFAULT ''",
    "interesting_fact": "TEXT NOT NULL DEFAULT ''",
}


@dataclass(frozen=True)
class BirdClassification:
    is_bird: bool
    common_name: str
    scientific_name: str
    certainty: str
    alternatives: list[dict[str, str]]
    field_marks: list[str]
    notes: str
    sex: str
    age_class: str
    bird_count: int
    behavior: str
    sex_evidence: str
    age_evidence: str
    interesting_fact: str
    person_detected: bool = False
    response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ClassificationResult:
    candidates: int = 0
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    estimated_cost_usd: float = 0.0
    stopped_reason: str | None = None


class ClassificationClient(Protocol):
    def classify(
        self,
        image_path: Path,
        *,
        location: str,
        captured_at: str,
        context_images: tuple[Path, ...] = (),
    ) -> BirdClassification: ...


class ClassificationAPIError(RuntimeError):
    """An API request failed or returned an unusable response."""


class OpenAIResponsesClient:
    """Minimal dependency-free client for the OpenAI Responses API."""

    API_URL = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-5.4-mini",
        max_output_tokens: int = 512,
        timeout_seconds: float = 60,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if not 64 <= max_output_tokens <= 1024:
            raise ValueError("max_output_tokens must be between 64 and 1024")
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def classify(
        self,
        image_path: Path,
        *,
        location: str,
        captured_at: str,
        context_images: tuple[Path, ...] = (),
    ) -> BirdClassification:
        mime_type = _image_mime_type(image_path)
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        content: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": _prompt(location, captured_at),
            },
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
                "detail": "high",
            },
        ]
        if context_images:
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        "The following images are nearby captures from the same "
                        "feeder. Use them only to judge whether the same visitor "
                        "continues across a burst; do not copy an identification "
                        "when the visible animal changes."
                    ),
                }
            )
            for context_image in context_images:
                context_mime_type = _image_mime_type(context_image)
                context_encoded = base64.b64encode(
                    context_image.read_bytes()
                ).decode("ascii")
                content.append(
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{context_mime_type};base64,{context_encoded}"
                        ),
                        "detail": "low",
                    }
                )
        payload = {
            "model": self.model,
            "store": False,
            "reasoning": {"effort": "none"},
            "max_output_tokens": self.max_output_tokens,
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "feeder_wildlife_identification",
                    "strict": True,
                    "schema": _response_schema(),
                }
            },
        }
        request = Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read()
        except HTTPError as error:
            detail = error.read(2048).decode("utf-8", errors="replace")
            raise ClassificationAPIError(
                f"OpenAI returned HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise ClassificationAPIError(f"OpenAI request failed: {error}") from error

        try:
            response_payload = json.loads(raw_response)
            output_text = _extract_output_text(response_payload)
            result = json.loads(output_text)
            _validate_result(result)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ClassificationAPIError(
                "OpenAI returned a response that did not match the wildlife schema"
            ) from error

        usage = response_payload.get("usage") or {}
        return BirdClassification(
            person_detected=result["person_detected"],
            is_bird=result["is_bird"],
            common_name=result["common_name"],
            scientific_name=result["scientific_name"],
            certainty=result["certainty"],
            alternatives=result["alternatives"],
            field_marks=result["field_marks"],
            notes=result["notes"],
            sex=result["sex"],
            age_class=result["age_class"],
            bird_count=result["bird_count"],
            behavior=result["behavior"],
            sex_evidence=result["sex_evidence"],
            age_evidence=result["age_evidence"],
            interesting_fact=result["interesting_fact"],
            response_id=response_payload.get("id"),
            input_tokens=_optional_nonnegative_int(usage.get("input_tokens")),
            output_tokens=_optional_nonnegative_int(usage.get("output_tokens")),
            total_tokens=_optional_nonnegative_int(usage.get("total_tokens")),
        )


class BirdClassifier:
    """Run single-threaded classification batches against a media library."""

    def __init__(
        self,
        library_root: Path,
        client: ClassificationClient,
        *,
        model: str = "gpt-5.4-mini",
        prompt_version: str = PROMPT_VERSION,
        input_usd_per_million: float = 0.75,
        output_usd_per_million: float = 4.50,
    ) -> None:
        self.library_root = library_root.expanduser().resolve()
        self.catalog_path = self.library_root / "catalog.sqlite3"
        self.media_root = self.library_root / "media"
        self.client = client
        self.model = model
        self.prompt_version = prompt_version
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million

    def run(
        self,
        *,
        max_images: int = 5,
        monthly_image_limit: int = 100,
        monthly_budget_usd: float = 10.00,
        request_cost_reserve_usd: float = 0.01,
        request_interval_seconds: float = 0.0,
        max_image_bytes: int = 4 * 1024 * 1024,
        location: str = "Toronto, Ontario, Canada",
        newest_first: bool = True,
        paired_only: bool = False,
        execute: bool = False,
        now: datetime | None = None,
    ) -> ClassificationResult:
        _validate_limits(
            max_images,
            monthly_image_limit,
            monthly_budget_usd,
            request_cost_reserve_usd,
            request_interval_seconds,
            max_image_bytes,
        )
        if not self.catalog_path.is_file():
            raise FileNotFoundError(self.catalog_path)

        connection = sqlite3.connect(self.catalog_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            if execute:
                ensure_classification_schema(connection)
                connection.commit()
            candidates = self._candidates(
                connection,
                max_images=max_images,
                max_image_bytes=max_image_bytes,
                newest_first=newest_first,
                paired_only=paired_only,
            )
            if not execute:
                return ClassificationResult(candidates=len(candidates))

            clock = now or datetime.now(timezone.utc)
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=timezone.utc)
            month = clock.astimezone(timezone.utc).strftime("%Y-%m")
            attempted_this_month, spent_this_month = self._month_usage(
                connection, month
            )
            attempted = succeeded = failed = 0
            batch_cost = 0.0
            stopped_reason: str | None = None
            next_request_at = time.monotonic()

            for row in candidates:
                if attempted_this_month >= monthly_image_limit:
                    stopped_reason = "monthly image limit reached"
                    break
                if spent_this_month + request_cost_reserve_usd > monthly_budget_usd:
                    stopped_reason = "monthly estimated budget reserve reached"
                    break

                delay = next_request_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                request_started_at = time.monotonic()

                image_path = self.media_root.joinpath(*Path(row["relative_path"]).parts)
                try:
                    if not image_path.is_file():
                        raise OSError(f"catalogued snapshot is missing: {image_path}")
                    classification = self.client.classify(
                        image_path,
                        location=location,
                        captured_at=_captured_at(row),
                        context_images=self._visit_context_images(
                            connection,
                            row,
                            max_image_bytes=max_image_bytes,
                        ),
                    )
                    cost = self._estimated_cost(classification)
                    if classification.person_detected:
                        self._delete_person_capture(
                            connection,
                            row,
                            classification,
                            cost,
                            clock,
                        )
                    else:
                        self._record_success(
                            connection,
                            row["id"],
                            classification,
                            cost,
                            clock,
                        )
                    succeeded += 1
                    batch_cost += cost
                    spent_this_month += cost
                except ClassificationAPIError as error:
                    self._record_failure(
                        connection,
                        row["id"],
                        str(error),
                        request_cost_reserve_usd,
                        clock,
                    )
                    failed += 1
                    spent_this_month += request_cost_reserve_usd
                except OSError:
                    failed += 1
                    continue

                connection.commit()
                attempted += 1
                attempted_this_month += 1
                next_request_at = request_started_at + request_interval_seconds

            return ClassificationResult(
                candidates=len(candidates),
                attempted=attempted,
                succeeded=succeeded,
                failed=failed,
                estimated_cost_usd=batch_cost,
                stopped_reason=stopped_reason,
            )
        finally:
            connection.close()

    def _candidates(
        self,
        connection: sqlite3.Connection,
        *,
        max_images: int,
        max_image_bytes: int,
        newest_first: bool,
        paired_only: bool,
    ) -> list[sqlite3.Row]:
        has_attempts = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'classification_attempts'
            """
        ).fetchone() is not None
        already_done = (
            """
            AND NOT EXISTS (
                SELECT 1 FROM classification_attempts
                WHERE classification_attempts.media_id = media.id
                  AND classification_attempts.provider = ?
                  AND classification_attempts.model = ?
                  AND classification_attempts.prompt_version = ?
            )
            """
            if has_attempts
            else ""
        )
        direction = "DESC" if newest_first else "ASC"
        paired_filter = (
            """
            AND EXISTS (
                SELECT 1 FROM media AS paired_video
                WHERE paired_video.source_id = media.source_id
                  AND paired_video.pair_key = media.pair_key
                  AND paired_video.kind = 'video'
            )
            """
            if paired_only
            else ""
        )
        parameters: list[object] = [max_image_bytes]
        if has_attempts:
            parameters.extend((PROVIDER, self.model, self.prompt_version))
        parameters.append(max_images)
        return connection.execute(
            f"""
            SELECT id, source_id, pair_key, relative_path, date_code, time_code
            FROM media
            WHERE kind = 'snapshot' AND size_bytes <= ?
            AND NOT EXISTS (
                SELECT 1 FROM classifications AS current_classification
                WHERE current_classification.media_id = media.id
                  AND current_classification.provider = 'manual'
            )
            {paired_filter}
            {already_done}
            ORDER BY date_code {direction}, time_code {direction}, subsecond_code {direction}
            LIMIT ?
            """,
            parameters,
        ).fetchall()

    def _visit_context_images(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        max_image_bytes: int,
    ) -> tuple[Path, ...]:
        hours, minutes, seconds = (
            int(row["time_code"][0:2]),
            int(row["time_code"][2:4]),
            int(row["time_code"][4:6]),
        )
        target_second = hours * 3600 + minutes * 60 + seconds
        second_expression = (
            "CAST(substr(time_code, 1, 2) AS INTEGER) * 3600 + "
            "CAST(substr(time_code, 3, 2) AS INTEGER) * 60 + "
            "CAST(substr(time_code, 5, 2) AS INTEGER)"
        )
        nearby = connection.execute(
            f"""
            SELECT relative_path
            FROM media
            WHERE kind = 'snapshot'
              AND date_code = ?
              AND id != ?
              AND size_bytes <= ?
              AND ABS(({second_expression}) - ?) <= ?
            ORDER BY ABS(({second_expression}) - ?), time_code DESC,
                     subsecond_code DESC
            LIMIT ?
            """,
            (
                row["date_code"],
                row["id"],
                max_image_bytes,
                target_second,
                VISIT_CONTEXT_SECONDS,
                target_second,
                MAX_VISIT_CONTEXT_IMAGES,
            ),
        ).fetchall()
        paths: list[Path] = []
        for nearby_row in nearby:
            path = self.media_root.joinpath(
                *Path(nearby_row["relative_path"]).parts
            )
            if path.is_file():
                paths.append(path)
        return tuple(paths)

    def _delete_person_capture(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        result: BirdClassification,
        cost: float,
        clock: datetime,
    ) -> None:
        """Tombstone and remove a capture pair when a person is detected."""
        source_id = str(row["source_id"])
        pair_key = str(row["pair_key"])
        rows = connection.execute(
            """
            SELECT id, relative_path
            FROM media
            WHERE source_id = ? AND pair_key = ?
            """,
            (source_id, pair_key),
        ).fetchall()
        if not rows:
            raise OSError("privacy capture pair disappeared before deletion")

        staged: list[tuple[Path, Path]] = []
        trash_root = self.library_root / ".deleting" / uuid4().hex
        try:
            connection.execute("BEGIN IMMEDIATE")
            for index, media_row in enumerate(rows):
                relative = PurePosixPath(media_row["relative_path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("invalid catalogued media path")
                candidate = self.media_root.joinpath(*relative.parts)
                try:
                    resolved = candidate.resolve(strict=True)
                except FileNotFoundError:
                    continue
                if self.media_root not in resolved.parents or not resolved.is_file():
                    raise ValueError("catalogued media escaped the library")
                trash_root.mkdir(parents=True, exist_ok=True)
                staged_path = trash_root / f"{index}-{resolved.name}"
                resolved.replace(staged_path)
                staged.append((staged_path, resolved))

            media_ids = [int(media_row["id"]) for media_row in rows]
            placeholders = ",".join("?" for _ in media_ids)
            connection.execute(
                f"DELETE FROM classification_attempts WHERE media_id IN ({placeholders})",
                media_ids,
            )
            connection.execute(
                f"DELETE FROM classifications WHERE media_id IN ({placeholders})",
                media_ids,
            )
            for table in ("stars", "classification_overrides"):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if exists is None:
                    continue
                if table == "stars":
                    connection.execute(
                        "DELETE FROM stars WHERE source_id = ? AND pair_key = ?",
                        (source_id, pair_key),
                    )
                else:
                    connection.execute(
                        f"DELETE FROM classification_overrides WHERE media_id IN ({placeholders})",
                        media_ids,
                    )
            detected_at = clock.astimezone(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO deleted_pairs (source_id, pair_key, deleted_at)
                VALUES (?, ?, ?)
                ON CONFLICT (source_id, pair_key) DO UPDATE SET
                    deleted_at = excluded.deleted_at
                """,
                (source_id, pair_key, detected_at),
            )
            connection.execute(
                """
                INSERT INTO privacy_deletions (
                    source_id, pair_key, provider, model, prompt_version,
                    response_id, estimated_cost_usd, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_id, pair_key) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    response_id = excluded.response_id,
                    estimated_cost_usd = excluded.estimated_cost_usd,
                    detected_at = excluded.detected_at
                """,
                (
                    source_id,
                    pair_key,
                    PROVIDER,
                    self.model,
                    self.prompt_version,
                    result.response_id,
                    cost,
                    detected_at,
                ),
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

        for staged_path, _ in staged:
            staged_path.unlink(missing_ok=True)
        for cache_directory in ("mobile-v1", "mobile-v2"):
            mobile_root = self.library_root / cache_directory
            for media_row in rows:
                relative = PurePosixPath(media_row["relative_path"])
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

    @staticmethod
    def _month_usage(connection: sqlite3.Connection, month: str) -> tuple[int, float]:
        row = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(estimated_cost_usd), 0)
            FROM (
                SELECT estimated_cost_usd
                FROM classification_attempts
                WHERE provider = ? AND substr(attempted_at, 1, 7) = ?
                UNION ALL
                SELECT estimated_cost_usd
                FROM privacy_deletions
                WHERE provider = ? AND substr(detected_at, 1, 7) = ?
            )
            """,
            (PROVIDER, month, PROVIDER, month),
        ).fetchone()
        return int(row[0]), float(row[1])

    def _estimated_cost(self, result: BirdClassification) -> float:
        input_tokens = result.input_tokens or 0
        output_tokens = result.output_tokens or 0
        return (
            input_tokens * self.input_usd_per_million
            + output_tokens * self.output_usd_per_million
        ) / 1_000_000

    def _record_success(
        self,
        connection: sqlite3.Connection,
        media_id: int,
        result: BirdClassification,
        cost: float,
        clock: datetime,
    ) -> None:
        timestamp = clock.astimezone(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO classification_attempts (
                media_id, provider, model, prompt_version, status, response_id,
                input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                attempted_at
            ) VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                PROVIDER,
                self.model,
                self.prompt_version,
                result.response_id,
                result.input_tokens,
                result.output_tokens,
                result.total_tokens,
                cost,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO classifications (
                media_id, provider, model, prompt_version, response_id, is_bird,
                common_name, scientific_name, certainty, alternatives_json,
                field_marks_json, notes, sex, age_class, bird_count, behavior,
                sex_evidence, age_evidence, interesting_fact, input_tokens,
                output_tokens, total_tokens, estimated_cost_usd, classified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (media_id) DO UPDATE SET
                provider = excluded.provider,
                model = excluded.model,
                prompt_version = excluded.prompt_version,
                response_id = excluded.response_id,
                is_bird = excluded.is_bird,
                common_name = excluded.common_name,
                scientific_name = excluded.scientific_name,
                certainty = excluded.certainty,
                alternatives_json = excluded.alternatives_json,
                field_marks_json = excluded.field_marks_json,
                notes = excluded.notes,
                sex = excluded.sex,
                age_class = excluded.age_class,
                bird_count = excluded.bird_count,
                behavior = excluded.behavior,
                sex_evidence = excluded.sex_evidence,
                age_evidence = excluded.age_evidence,
                interesting_fact = excluded.interesting_fact,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                total_tokens = excluded.total_tokens,
                estimated_cost_usd = excluded.estimated_cost_usd,
                classified_at = excluded.classified_at
            """,
            (
                media_id,
                PROVIDER,
                self.model,
                self.prompt_version,
                result.response_id,
                int(result.is_bird),
                result.common_name,
                result.scientific_name,
                result.certainty,
                json.dumps(result.alternatives, separators=(",", ":")),
                json.dumps(result.field_marks, separators=(",", ":")),
                result.notes,
                result.sex,
                result.age_class,
                result.bird_count,
                result.behavior,
                result.sex_evidence,
                result.age_evidence,
                result.interesting_fact,
                result.input_tokens,
                result.output_tokens,
                result.total_tokens,
                cost,
                timestamp,
            ),
        )

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        media_id: int,
        message: str,
        reserved_cost: float,
        clock: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO classification_attempts (
                media_id, provider, model, prompt_version, status, error_message,
                estimated_cost_usd, attempted_at
            ) VALUES (?, ?, ?, ?, 'failed', ?, ?, ?)
            """,
            (
                media_id,
                PROVIDER,
                self.model,
                self.prompt_version,
                message[:1000],
                reserved_cost,
                clock.astimezone(timezone.utc).isoformat(),
            ),
        )


def _prompt(location: str, captured_at: str) -> str:
    fact_focus = FACT_FOCUSES[
        sum((index + 1) * ord(character) for index, character in enumerate(captured_at))
        % len(FACT_FOCUSES)
    ]
    return (
        "This feeder can be visited by birds and other wildlife. Identify any "
        "visible animal in this image; do not assume the visitor must be a bird. "
        "The first image is the primary capture to classify. If nearby feeder "
        "images follow, use them to keep a visually continuous visit consistent, "
        "but treat a visibly different visitor as a new identification. "
        "Use visible field marks and geographic plausibility; do not invent details. "
        f"The camera is near {location}, and the capture time is {captured_at}. "
        "Set is_bird true only for a bird. For another visible animal—such as a "
        "squirrel, rat, mouse, raccoon, rabbit, or cat—set is_bird false but identify "
        "the animal in common_name, include its scientific_name when the species is "
        "reasonably supported, and describe its observed behavior. If the image is "
        "empty, obstructed, or has no identifiable animal, set is_bird false, use "
        "common_name 'No animal detected', and use an empty scientific_name. Common "
        "and scientific names must otherwise be plain names without commentary. Count "
        "visible birds in bird_count; use bird_count 0 for other animals and empty "
        "frames. Infer sex for birds and other animals only when plumage, anatomy, "
        "or another visible sex-specific field mark supports it; do not infer sex "
        "from size, behavior, or the species name alone. Otherwise use indeterminate "
        "and briefly say why in sex_evidence. Infer bird age only when a visible "
        "field mark supports it; otherwise use indeterminate and briefly say why. "
        "For non-bird animals use not_applicable for age and leave age_evidence empty. "
        "For empty frames use not_applicable for sex and age, behavior 'No animal visible', "
        "and empty evidence and fact fields. The interesting_fact is the main editorial note, "
        "not a caption of the obvious feeder activity. Make it vivid, specific to "
        "the identified bird or animal species, and useful to a curious reader in one or two short "
        "sentences. Whenever it is genuinely relevant, connect it to the supplied "
        "capture date or location. For variety, this capture's preferred fact angle "
        f"is {fact_focus}. If that angle would require a doubtful claim, choose a "
        "different well-supported angle instead. Do not claim that the bird is rare, "
        "migrating, breeding, or at a range edge unless that is accurate for the "
        "species, place, and season. Before finalizing a brown feeder bird, explicitly "
        "check for a thick orange-red bill, a subtle crest, and red in the wings or "
        "tail: those marks favor a female or juvenile Northern Cardinal over a House "
        "Finch, House Sparrow, or Mourning Dove. Use the complete combination of "
        "visible marks rather than overall brown color or camera perspective. "
        "People can also appear accidentally. Set person_detected true whenever a "
        "real person is visible, even partially or in the background; otherwise set "
        "it false. This privacy flag is independent of the animal identification. "
        "Avoid generic facts that could describe most "
        "birds, and do not merely repeat the observed behavior. Keep all prose brief "
        "and do not invent visible details."
    )


def _response_schema() -> dict[str, object]:
    named_species = {
        "type": "object",
        "properties": {
            "common_name": {"type": "string", "maxLength": 100},
            "scientific_name": {"type": "string", "maxLength": 120},
        },
        "required": ["common_name", "scientific_name"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "person_detected": {"type": "boolean"},
            "is_bird": {"type": "boolean"},
            "common_name": {"type": "string", "maxLength": 100},
            "scientific_name": {"type": "string", "maxLength": 120},
            "certainty": {
                "type": "string",
                "enum": ["certain", "likely", "uncertain"],
            },
            "alternatives": {
                "type": "array",
                "items": named_species,
                "maxItems": 3,
            },
            "field_marks": {
                "type": "array",
                "items": {"type": "string", "maxLength": 160},
                "maxItems": 5,
            },
            "notes": {"type": "string", "maxLength": 300},
            "sex": {
                "type": "string",
                "enum": ["male", "female", "indeterminate", "not_applicable"],
            },
            "age_class": {
                "type": "string",
                "enum": [
                    "adult",
                    "juvenile",
                    "immature",
                    "indeterminate",
                    "not_applicable",
                ],
            },
            "bird_count": {"type": "integer", "minimum": 0, "maximum": 20},
            "behavior": {"type": "string", "maxLength": 160},
            "sex_evidence": {"type": "string", "maxLength": 200},
            "age_evidence": {"type": "string", "maxLength": 200},
            "interesting_fact": {"type": "string", "maxLength": 300},
        },
        "required": [
            "person_detected",
            "is_bird",
            "common_name",
            "scientific_name",
            "certainty",
            "alternatives",
            "field_marks",
            "notes",
            "sex",
            "age_class",
            "bird_count",
            "behavior",
            "sex_evidence",
            "age_evidence",
            "interesting_fact",
        ],
        "additionalProperties": False,
    }


def _extract_output_text(payload: dict[str, object]) -> str:
    for output in payload.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise ValueError("model refused classification")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise ValueError("response contains no output text")


def _validate_result(result: object) -> None:
    if not isinstance(result, dict):
        raise TypeError("result must be an object")
    required = {
        "person_detected",
        "is_bird",
        "common_name",
        "scientific_name",
        "certainty",
        "alternatives",
        "field_marks",
        "notes",
        "sex",
        "age_class",
        "bird_count",
        "behavior",
        "sex_evidence",
        "age_evidence",
        "interesting_fact",
    }
    if set(result) != required:
        raise ValueError("unexpected result fields")
    if not isinstance(result["is_bird"], bool):
        raise TypeError("is_bird must be boolean")
    if not isinstance(result["person_detected"], bool):
        raise TypeError("person_detected must be boolean")
    if result["certainty"] not in {"certain", "likely", "uncertain"}:
        raise ValueError("invalid certainty")
    if result["sex"] not in {"male", "female", "indeterminate", "not_applicable"}:
        raise ValueError("invalid sex")
    if result["age_class"] not in {
        "adult",
        "juvenile",
        "immature",
        "indeterminate",
        "not_applicable",
    }:
        raise ValueError("invalid age class")
    if (
        not isinstance(result["bird_count"], int)
        or isinstance(result["bird_count"], bool)
        or not 0 <= result["bird_count"] <= 20
    ):
        raise TypeError("bird_count must be an integer between 0 and 20")
    string_fields = (
        "common_name",
        "scientific_name",
        "notes",
        "behavior",
        "sex_evidence",
        "age_evidence",
        "interesting_fact",
    )
    if not all(isinstance(result[name], str) for name in string_fields):
        raise TypeError("classification prose fields must be strings")
    if not isinstance(result["field_marks"], list) or not all(
        isinstance(mark, str) for mark in result["field_marks"]
    ):
        raise TypeError("field_marks must be strings")
    alternatives = result["alternatives"]
    if not isinstance(alternatives, list) or not all(
        isinstance(item, dict)
        and set(item) == {"common_name", "scientific_name"}
        and all(isinstance(value, str) for value in item.values())
        for item in alternatives
    ):
        raise TypeError("alternatives must be named species")


def ensure_classification_schema(connection: sqlite3.Connection) -> None:
    """Create the schema and add v2 columns to an existing v1 catalog."""
    connection.executescript(CLASSIFICATION_SCHEMA)
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(classifications)")
    }
    for name, definition in CLASSIFICATION_MIGRATION_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE classifications ADD COLUMN {name} {definition}"
            )


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError("token usage must be a non-negative integer")
    return value


def _image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    raise ValueError(f"unsupported snapshot type: {suffix}")


def _captured_at(row: sqlite3.Row) -> str:
    try:
        return datetime.strptime(
            row["date_code"] + row["time_code"], "%y%m%d%H%M%S"
        ).isoformat()
    except ValueError:
        return f"{row['date_code']} {row['time_code']}"


def _validate_limits(
    max_images: int,
    monthly_image_limit: int,
    monthly_budget_usd: float,
    request_cost_reserve_usd: float,
    request_interval_seconds: float,
    max_image_bytes: int,
) -> None:
    if not 1 <= max_images <= 100:
        raise ValueError("max_images must be between 1 and 100")
    if not 1 <= monthly_image_limit <= 10_000:
        raise ValueError("monthly_image_limit must be between 1 and 10000")
    if not 0 < monthly_budget_usd <= 100:
        raise ValueError("monthly_budget_usd must be greater than 0 and at most 100")
    if not 0 < request_cost_reserve_usd <= monthly_budget_usd:
        raise ValueError("request_cost_reserve_usd must fit within the monthly budget")
    if not 0 <= request_interval_seconds <= 60:
        raise ValueError("request_interval_seconds must be between 0 and 60")
    if not 1 <= max_image_bytes <= 20 * 1024 * 1024:
        raise ValueError("max_image_bytes must be between 1 byte and 20 MiB")
