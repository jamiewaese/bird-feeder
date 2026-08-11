from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from camera.classification import (
    BirdClassification,
    BirdClassifier,
    ClassificationAPIError,
    OpenAIResponsesClient,
    ensure_classification_schema,
)
from camera.sdcard import FilesystemMediaSource, MediaImporter


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[Path] = []
        self.fail = fail

    def classify(
        self, image_path: Path, *, location: str, captured_at: str
    ) -> BirdClassification:
        self.calls.append(image_path)
        if self.fail:
            raise ClassificationAPIError("fixture failure")
        return BirdClassification(
            is_bird=True,
            common_name="Black-capped Chickadee",
            scientific_name="Poecile atricapillus",
            certainty="likely",
            alternatives=[],
            field_marks=["black cap", "white cheeks"],
            notes="Small songbird at the feeder.",
            sex="indeterminate",
            age_class="adult",
            bird_count=1,
            behavior="Perched at feeder",
            sex_evidence="Sexes have similar plumage.",
            age_evidence="Definitive adult plumage.",
            interesting_fact="Chickadees can remember thousands of food cache sites.",
            response_id="resp_fixture",
            input_tokens=400,
            output_tokens=100,
            total_tokens=500,
        )


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        card = root / "card"
        self.library = root / "library"
        for time_code in ("092443", "092444", "092445"):
            directory = card / "snaps/260809"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{time_code}_150_031_P.jpg").write_bytes(
                b"fixture-jpeg-" + time_code.encode("ascii")
            )
        video_directory = card / "video/260809"
        video_directory.mkdir(parents=True)
        for time_code in ("092443", "092444"):
            (video_directory / f"{time_code}_150_031_P.mp4").write_bytes(b"video")
        MediaImporter(self.library).sync(FilesystemMediaSource(card, "yard"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preview_makes_no_calls_or_catalog_writes(self) -> None:
        client = FakeClient()
        catalog = self.library / "catalog.sqlite3"
        before = catalog.read_bytes()

        result = BirdClassifier(self.library, client).run(max_images=2)

        self.assertEqual(result.candidates, 2)
        self.assertEqual(result.attempted, 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(catalog.read_bytes(), before)

    def test_run_cap_and_success_are_idempotent(self) -> None:
        client = FakeClient()
        classifier = BirdClassifier(self.library, client)
        clock = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)

        first = classifier.run(max_images=2, execute=True, now=clock)
        second = classifier.run(max_images=2, execute=True, now=clock)

        self.assertEqual((first.attempted, first.succeeded), (2, 2))
        self.assertEqual((second.attempted, second.succeeded), (1, 1))
        self.assertEqual(len(client.calls), 3)
        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            classifications = connection.execute(
                "SELECT COUNT(*) FROM classifications"
            ).fetchone()[0]
            attempts = connection.execute(
                "SELECT COUNT(*) FROM classification_attempts"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(classifications, 3)
        self.assertEqual(attempts, 3)

    def test_requests_are_paced_from_their_start_times(self) -> None:
        client = FakeClient()
        classifier = BirdClassifier(self.library, client)

        with (
            patch(
                "camera.classification.classifier.time.monotonic",
                side_effect=[0.0, 0.0, 0.0, 0.0, 6.1],
            ),
            patch("camera.classification.classifier.time.sleep") as sleep,
        ):
            result = classifier.run(
                max_images=2,
                request_interval_seconds=6.1,
                execute=True,
            )

        self.assertEqual(result.succeeded, 2)
        sleep.assert_called_once_with(6.1)

    def test_monthly_image_limit_stops_before_call(self) -> None:
        client = FakeClient()
        classifier = BirdClassifier(self.library, client)
        clock = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)

        result = classifier.run(
            max_images=3,
            monthly_image_limit=1,
            execute=True,
            now=clock,
        )

        self.assertEqual(result.attempted, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.stopped_reason, "monthly image limit reached")

    def test_paired_only_ignores_snapshot_without_video(self) -> None:
        client = FakeClient()
        result = BirdClassifier(self.library, client).run(
            max_images=3, paired_only=True
        )

        self.assertEqual(result.candidates, 2)
        self.assertEqual(client.calls, [])

    def test_v1_catalog_is_migrated_without_losing_classification(self) -> None:
        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            connection.execute("DROP TABLE classifications")
            connection.execute(
                """
                CREATE TABLE classifications (
                    media_id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    response_id TEXT,
                    is_bird INTEGER NOT NULL,
                    common_name TEXT NOT NULL,
                    scientific_name TEXT NOT NULL,
                    certainty TEXT NOT NULL,
                    alternatives_json TEXT NOT NULL,
                    field_marks_json TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    estimated_cost_usd REAL NOT NULL,
                    classified_at TEXT NOT NULL
                )
                """
            )
            media_id = connection.execute(
                "SELECT id FROM media WHERE kind = 'snapshot' LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO classifications VALUES (
                    ?, 'openai', 'gpt-5.4-mini', 'bird-id-v1', NULL, 1,
                    'Northern Cardinal', 'Cardinalis cardinalis', 'likely',
                    '[]', '[]', 'Legacy row', 1, 1, 2, 0.001,
                    '2026-08-09T00:00:00+00:00'
                )
                """,
                (media_id,),
            )
            ensure_classification_schema(connection)
            connection.commit()
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(classifications)")
            }
            row = connection.execute(
                "SELECT common_name, sex, interesting_fact FROM classifications"
            ).fetchone()
        finally:
            connection.close()

        self.assertIn("age_class", columns)
        self.assertIn("interesting_fact", columns)
        self.assertEqual(row, ("Northern Cardinal", "indeterminate", ""))

    def test_monthly_budget_reserve_stops_before_call(self) -> None:
        client = FakeClient()
        classifier = BirdClassifier(self.library, client)

        result = classifier.run(
            max_images=3,
            monthly_budget_usd=0.01,
            request_cost_reserve_usd=0.01,
            execute=True,
            now=datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.stopped_reason, "monthly estimated budget reserve reached")

    def test_failure_is_recorded_once_without_retry(self) -> None:
        client = FakeClient(fail=True)
        classifier = BirdClassifier(self.library, client)
        result = classifier.run(max_images=1, execute=True)
        second = classifier.run(max_images=1, execute=True)

        self.assertEqual((result.attempted, result.failed), (1, 1))
        self.assertEqual(second.attempted, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertNotEqual(client.calls[0], client.calls[1])
        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            row = connection.execute(
                "SELECT status, error_message FROM classification_attempts"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("failed", "fixture failure"))


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ResponsesClientTests(unittest.TestCase):
    def test_request_is_low_detail_bounded_structured_and_not_stored(self) -> None:
        captured: dict[str, object] = {}
        model_result = {
            "is_bird": True,
            "common_name": "Northern Cardinal",
            "scientific_name": "Cardinalis cardinalis",
            "certainty": "certain",
            "alternatives": [],
            "field_marks": ["red plumage"],
            "notes": "Adult male.",
            "sex": "male",
            "age_class": "adult",
            "bird_count": 1,
            "behavior": "Feeding",
            "sex_evidence": "Uniform bright red plumage.",
            "age_evidence": "Adult male plumage.",
            "interesting_fact": "Northern Cardinals sing throughout the year.",
        }

        def opener(request: object, *, timeout: float) -> FakeHTTPResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHTTPResponse(
                {
                    "id": "resp_test",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": json.dumps(model_result)}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 80,
                        "total_tokens": 380,
                    },
                }
            )

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "bird.jpg"
            image.write_bytes(b"jpeg fixture")
            result = OpenAIResponsesClient(
                "secret-test-key", opener=opener
            ).classify(image, location="Toronto", captured_at="2026-08-09T09:24:43")

        request = captured["request"]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gpt-5.4-mini")
        self.assertEqual(payload["max_output_tokens"], 512)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][0]["content"][1]["detail"], "high")
        self.assertTrue(payload["text"]["format"]["strict"])
        prompt = payload["input"][0]["content"][0]["text"]
        self.assertIn("The interesting_fact is the main editorial note", prompt)
        self.assertIn("capture date or location", prompt)
        self.assertIn("preferred fact angle", prompt)
        self.assertIn("female or juvenile Northern Cardinal", prompt)
        self.assertIn("Identify the bird or other animal", prompt)
        self.assertIn("squirrel, rat, mouse, raccoon", prompt)
        self.assertIn("common_name 'No animal detected'", prompt)
        self.assertIn("Toronto", prompt)
        self.assertIn("2026-08-09T09:24:43", prompt)
        self.assertEqual(result.common_name, "Northern Cardinal")
        self.assertEqual(result.sex, "male")
        self.assertEqual(result.bird_count, 1)
        self.assertNotIn("secret-test-key", request.data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
