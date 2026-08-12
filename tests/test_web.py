from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from camera.sdcard import FilesystemMediaSource, MediaImporter
from web.app import (
    GalleryPair,
    _animal_summaries,
    _json_pairs,
    _render_about,
    _render_index,
    _render_watch,
    _share_filename,
    _species_summaries,
    list_pairs,
    make_handler,
)


class PassthroughMobilePreparer:
    def __init__(self, library: Path) -> None:
        self.media_root = library / "media"
        self.calls: list[str] = []
        self.cached_calls: list[str] = []
        self.cached_path: Path | None = None

    def prepare(self, relative_path: str) -> Path:
        self.calls.append(relative_path)
        return (self.media_root / relative_path).resolve()

    def cached(self, relative_path: str) -> Path | None:
        self.cached_calls.append(relative_path)
        return self.cached_path


class SpeciesSummaryTests(unittest.TestCase):
    def _pair(
        self,
        pair_key: str,
        common_name: str,
        scientific_name: str,
        *,
        video: bool = True,
        is_bird: bool = True,
        sex: str | None = None,
        bird_count: int | None = None,
    ) -> GalleryPair:
        return GalleryPair(
            source_id="yard",
            pair_key=pair_key,
            date_code="260809",
            time_code=pair_key,
            snapshot_path=f"yard/snaps/{pair_key}.jpg",
            video_path=f"yard/video/{pair_key}.mp4" if video else None,
            is_bird=is_bird,
            common_name=common_name,
            scientific_name=scientific_name,
            sex=sex,
            bird_count=bird_count,
        )

    def test_species_cards_count_videos_sort_by_frequency_and_filter_by_key(self) -> None:
        pairs = [
            self._pair("100003", "House Sparrow", "Passer domesticus"),
            self._pair(
                "100002", "Black-capped Chickadee", "Poecile atricapillus", sex="male"
            ),
            self._pair(
                "100001", "Black-capped Chickadee", "Poecile atricapillus", sex="female"
            ),
            self._pair(
                "100000",
                "Black-capped Chickadee",
                "Poecile atricapillus",
                video=False,
            ),
            self._pair("095959", "Squirrel", "", is_bird=False, sex="female"),
        ]

        summaries = _species_summaries(pairs)
        self.assertEqual(
            [(summary.common_name, summary.video_count) for summary in summaries],
            [("Black-capped Chickadee", 2), ("House Sparrow", 1)],
        )
        animal_summaries = _animal_summaries(pairs)
        self.assertEqual(
            [(summary.common_name, summary.video_count) for summary in animal_summaries],
            [("Squirrel", 1)],
        )
        self.assertEqual(animal_summaries[0].female_video_count, 1)
        self.assertEqual(animal_summaries[0].male_video_count, 0)
        self.assertEqual(animal_summaries[0].unknown_sex_video_count, 0)

        page = _render_index(pairs, "test-token").decode("utf-8")
        species_filters = re.findall(r'data-species-filter="([^"]+)"', page)
        self.assertEqual(
            species_filters,
            ["poecile atricapillus", "passer domesticus", "squirrel"],
        )
        self.assertIn("Birds we’ve seen", page)
        self.assertIn("<b data-species-count>2</b> ", page)
        self.assertIn(
            '<div class="species-card-sex" data-species-sex-breakdown>'
            '<button class="sex-key sex-key-male" type="button" '
            'data-sex-filter="male" aria-pressed="false">1 male</button>'
            '<span class="sex-separator" aria-hidden="true"> · </span>'
            '<button class="sex-key sex-key-female" type="button" '
            'data-sex-filter="female" aria-pressed="false">1 female</button></div>',
            page,
        )
        self.assertIn('data-sex="male"', page)
        self.assertIn('data-sex="female"', page)
        self.assertIn('sexCounts.male', page)
        self.assertIn("sex-key-male { color: #4f739d", page)
        self.assertIn("sex-key-female { color: #bd6b87", page)
        self.assertIn('.sex-key-male[aria-pressed="true"]', page)
        self.assertIn("background: rgb(79 115 157 / 10%)", page)
        self.assertIn('key.className = `sex-key sex-key-${part.sex}`', page)
        self.assertIn('key.dataset.sexFilter = part.sex', page)
        self.assertIn('const sexFilterButton = event.target.closest("[data-sex-filter]")', page)
        self.assertIn('activeSex = sameSelection ? "" : sex', page)
        self.assertIn("const sexMatches = !activeSex || cardSex === activeSex", page)
        self.assertIn("grid-column: 1 / -1", page)
        self.assertIn("font-weight: 700; white-space: nowrap", page)
        self.assertNotIn("species-card-sex { overflow: hidden", page)
        self.assertIn('data-species="poecile atricapillus"', page)
        self.assertIn('data-species-label="Black-capped Chickadee"', page)
        self.assertIn('card.dataset.species === activeSpecies', page)
        self.assertIn('card.dataset.hasVideo === "true"', page)
        self.assertEqual(page.count('data-hourly-activity '), 3)
        self.assertIn('data-chart-species="poecile atricapillus"', page)
        self.assertIn('data-chart-species="squirrel"', page)
        self.assertIn(
            'aria-label="Squirrel sightings by hour, sex, and month" hidden', page
        )
        self.assertIn('class="activity-chart animal-activity-chart"', page)
        self.assertIn('class="species-row species-card-shell" data-species-row', page)
        self.assertNotIn("When they visit", page)
        self.assertNotIn("data-hourly-summary", page)
        self.assertNotIn("Hover, tap, or focus", page)
        self.assertIn("renderHourlyActivity(cards);", page)
        self.assertIn("activitySexStyles", page)
        self.assertIn('male: { label: "Male", color: "#4f739d"', page)
        self.assertIn('female: { label: "Female", color: "#bd6b87"', page)
        self.assertIn("item.hourlyBySex[sex][hour] += sightingCount", page)
        self.assertIn('if (!isBird && card.dataset.isAnimal !== "true") return', page)
        self.assertIn(
            'unknown: { label: "Unknown sex", color: "#8b603b"', page
        )
        self.assertIn("animalActivityStyles[sex]", page)
        self.assertIn(
            'Blue solid lines indicate males, pink dashed lines indicate females, and the brown solid line indicates unknown-sex sightings.',
            page,
        )
        self.assertIn('tooltip.style.top = `${yPosition(sightings)}px`', page)
        self.assertNotIn("yPosition(birds)", page)
        self.assertIn('class="species-row species-card-shell animal-card-shell"', page)
        self.assertIn(".animal-grid { grid-template-columns: minmax(0, 1fr)", page)
        self.assertIn("monthlyVideos: Array(12).fill(0)", page)
        self.assertIn("item.monthlyVideos[month] += 1", page)
        self.assertIn('class: "activity-months"', page)
        self.assertIn('class: "activity-month-cell"', page)
        self.assertIn("plotWidth >= 480", page)
        self.assertIn("plotWidth >= 210", page)
        self.assertIn('Array(12).fill("")', page)
        self.assertIn("activity-night", page)
        self.assertIn('createSvgElement("linearGradient"', page)
        self.assertIn('gradientUnits: "userSpaceOnUse"', page)
        self.assertIn("const nightTransitionHours = 1.5", page)
        self.assertIn('"stop-opacity": "0.09"', page)
        self.assertIn('"stop-opacity": "0"', page)
        self.assertIn("fill: `url(#${dawnGradientId})`", page)
        self.assertIn("fill: `url(#${duskGradientId})`", page)
        self.assertIn("activity-daypart-icons", page)
        self.assertIn("moonStarsPath", page)
        self.assertIn("eveningMoonStars", page)
        self.assertIn("sunPath", page)
        self.assertIn("scale(0.078125)", page)
        self.assertIn("Shaded areas indicate nighttime hours", page)
        self.assertIn("grid-template-columns: 300px minmax(0, 1fr)", page)
        self.assertIn(".species-card-shell { overflow: hidden; border:", page)
        self.assertIn("container: archive / inline-size", page)
        self.assertIn(".species-row .species-card { width: 100%; grid-template-columns:", page)
        self.assertIn("padding-inline: 0", page)
        self.assertIn("padding: 0 11px", page)
        self.assertIn("@container archive (min-width: 578px)", page)
        self.assertIn("calc((100cqw - 18px) / 2)", page)
        self.assertIn("@container archive (min-width: 876px)", page)
        self.assertIn("calc((100cqw - 36px) / 3)", page)
        self.assertIn("@container archive (min-width: 1174px)", page)
        self.assertIn("calc((100cqw - 54px) / 4)", page)
        self.assertIn(
            '@media (max-width: 760px) and (orientation: portrait), (max-width: 520px)',
            page,
        )
        self.assertIn("grid-template-rows: auto 154px", page)
        self.assertNotIn('chart.style.height = `${Math.round(speciesCard', page)
        self.assertIn("max-width: 100%; min-width: 0", page)
        self.assertIn('window.addEventListener("resize"', page)
        self.assertIn("Other feeder visitors", page)
        self.assertEqual(page.count('<button class="section-toggle"'), 2)
        self.assertIn(
            'data-section-label="Birds we’ve seen" aria-expanded="false"', page
        )
        self.assertIn(
            'data-section-label="Other feeder visitors" aria-expanded="false"', page
        )
        self.assertIn('aria-controls="species-section-content"', page)
        self.assertIn('aria-controls="animal-section-content"', page)
        self.assertIn('aria-label="Expand Birds we’ve seen"', page)
        self.assertIn('aria-label="Expand Other feeder visitors"', page)
        self.assertIn('<div id="species-section-content" data-section-content hidden>', page)
        self.assertIn('<div id="animal-section-content" data-section-content hidden>', page)
        self.assertIn("content.hidden = !nextExpanded", page)
        self.assertIn(
            "window.requestAnimationFrame(() => renderHourlyActivity(hourlyActivityCards))",
            page,
        )
        self.assertIn('data-species-filter="squirrel"', page)
        self.assertIn('data-visitor-kind="animal"', page)
        self.assertIn(
            'speciesButton.dataset.visitorKind === "animal" ? "animals" : "birds"',
            page,
        )
        self.assertIn('classification-stats classification-stats-animal', page)
        self.assertIn(".classification-stats-animal { grid-template-columns:", page)
        self.assertIn('<option value="animals">Other animal detected</option>', page)
        self.assertIn('<option value="empty">No animal detected</option>', page)
        self.assertIn('(filter === "animals" && card.dataset.isAnimal === "true")', page)
        payload = json.loads(_json_pairs([pairs[-1]]))
        self.assertTrue(payload["media"][0]["classification"]["is_animal"])
        gallery = re.search(r'<section class="gallery".*?</section>', page, re.DOTALL)
        self.assertIsNotNone(gallery)
        self.assertNotIn('data-share-video', gallery.group(0))
        self.assertNotIn("Watch clip", page)
        self.assertNotIn('> Download</a>', page)
        self.assertIn('class="card-open-link" href="/watch/', gallery.group(0))
        self.assertIn('cursor: pointer', page)
        self.assertIsNotNone(
            re.search(
                r'class="card" data-card hidden.*?data-is-bird="false"',
                page,
                re.DOTALL,
            )
        )

    def test_multiple_birds_filter_uses_classified_bird_count(self) -> None:
        pairs = [
            self._pair(
                "100002",
                "House Sparrow",
                "Passer domesticus",
                bird_count=2,
            ),
            self._pair(
                "100001",
                "Black-capped Chickadee",
                "Poecile atricapillus",
                bird_count=1,
            ),
            self._pair(
                "100000",
                "American Robin",
                "Turdus migratorius",
            ),
        ]

        page = _render_index(pairs, "test-token").decode("utf-8")

        self.assertIn(
            '<option value="multiple-birds">Multiple birds detected</option>',
            page,
        )
        self.assertIn('data-bird-count="2"', page)
        self.assertIn('data-bird-count="1"', page)
        self.assertIn('data-bird-count=""', page)
        self.assertIn(
            '(filter === "multiple-birds" && Number(card.dataset.birdCount) > 1)',
            page,
        )

    def test_gallery_paginates_and_defers_thumbnails_after_first_24(self) -> None:
        pairs = [
            self._pair(
                f"1000{second:02d}",
                "House Sparrow",
                "Passer domesticus",
            )
            for second in range(25)
        ]

        page = _render_index(pairs, "test-token").decode("utf-8")
        gallery = re.search(
            r'<section class="gallery".*?</section>', page, re.DOTALL
        )
        self.assertIsNotNone(gallery)
        self.assertEqual(len(re.findall(r'<img src="/media/', gallery.group(0))), 24)
        self.assertEqual(
            len(re.findall(r'<img data-src="/media/', gallery.group(0))), 1
        )
        self.assertIn("const archivePageSize = 24;", page)
        self.assertIn("data-page-status>Showing 1–24 of 25</p>", page)
        self.assertIn('data-page-number aria-live="polite">Page 1 of 2</p>', page)
        self.assertIn("data-page-previous disabled", page)
        self.assertIn("data-page-next>Next</button>", page)
        self.assertIn('card.querySelectorAll("img[data-src]")', page)
        self.assertIn("refreshArchive({ resetPage: true });", page)


class SharedFilenameTests(unittest.TestCase):
    def test_uses_species_location_and_capture_datetime(self) -> None:
        pair = GalleryPair(
            source_id="yard",
            pair_key="260809/092443_150_031_P",
            date_code="260809",
            time_code="092443",
            common_name="Black-capped Chickadee",
            video_path="yard/video/260809/092443_150_031_P.mp4",
        )

        self.assertEqual(
            _share_filename(pair, location="Toronto"),
            "Black-capped-Chickadee_Toronto_2026-08-09_09-24-43.mp4",
        )


class AboutPageTests(unittest.TestCase):
    def test_about_page_includes_story_resources_diagram_and_photo_placeholder(self) -> None:
        page = _render_about().decode("utf-8")

        self.assertIn("<h1>What is this?</h1>", page)
        self.assertIn("TL;DR", page)
        self.assertIn("runs this gallery from my basement", page)
        self.assertNotIn("class=\"about-intro\"", page)
        self.assertNotIn("This is the site I ended up building for it", page)
        self.assertIn("Why I built it", page)
        self.assertIn("How it works", page)
        self.assertIn('class="process-grid"', page)
        self.assertNotIn('class="system-diagram"', page)
        self.assertIn("Save the visit", page)
        self.assertIn("Raspberry Pi", page)
        self.assertIn("Cloudflare Tunnel", page)
        self.assertIn("bought the domain through Cloudflare for about $10 a year", page)
        self.assertIn("without opening a port on my router", page)
        self.assertIn("smart+bird+feeder+with+camera", page)
        self.assertIn("This isn’t an affiliate link", page)
        self.assertIn("github.com/jamiewaese/bird-feeder", page)
        self.assertIn("data-future-feeder-photo", page)
        self.assertIn("data-future-pi-photo", page)
        self.assertIn("subscription of $59.99 a year", page)
        self.assertIn("the box, the manual and the Amazon listing don’t mention", page)
        self.assertIn("comes with an iOS app that can identify the species", page)
        self.assertIn("but that’s so old school", page)
        self.assertIn("but he thinks everything is a great idea", page)
        self.assertIn("waits 30 seconds before the next one", page)
        self.assertIn("downloads the previous day’s worth of files", page)
        self.assertIn("when I used the iOS app", page)
        self.assertIn("manufacturer’s transport library", page)
        self.assertIn("Chrome and Android phones could open the MP4s", page)
        self.assertIn("couldn’t be sent through iMessage", page)
        self.assertIn("runs each video through FFmpeg", page)
        self.assertIn("The original from the camera is left untouched", page)
        self.assertIn("Reading the species charts", page)
        self.assertIn(".story-grid > div, .story-copy { min-width: 0; }", page)
        self.assertIn(".species-card-example-scroll { width: 100%; overflow-x: auto;", page)
        self.assertIn(".about-species-card { width: 100%; grid-template-columns: 1fr; }", page)
        self.assertIn(".about-species-card .activity-chart { min-height: 170px;", page)
        self.assertIn(".story-photo { width: 100%; max-width: 100%;", page)
        self.assertIn(".about-shell figure { max-width: 100%; box-sizing: border-box; }", page)
        self.assertIn("horizontal axis runs from midnight to 11 p.m.", page)
        self.assertIn(
            "quick overview of the counts at each time of day for each species", page
        )
        self.assertNotIn("vertical scales adjust independently", page)
        self.assertIn("This has been a labour of love", page)
        self.assertIn("None of this appeared from nowhere", page)
        self.assertIn("putting the code online for free", page)
        self.assertNotIn("fair amount of stubbornness", page)
        self.assertNotIn("Slow is reliable", page)
        self.assertNotIn("same itch", page)
        self.assertIn('href="/"', page)

    def test_about_page_uses_supplied_photo_when_available(self) -> None:
        page = _render_about(
            photo_available=True,
            pi_photo_available=True,
            amazon_photo_available=True,
            subscription_photo_available=True,
        ).decode("utf-8")

        self.assertIn('src="/about-feeder.jpg"', page)
        self.assertIn('src="/about-raspberry-pi.jpg"', page)
        self.assertIn('src="/about-amazon.png"', page)
        self.assertIn('src="/about-subscription.jpg"', page)
        self.assertIn('class="species-row species-card-shell about-species-card"', page)
        self.assertIn("Northern Cardinal sightings by hour, sex, and month", page)
        self.assertIn(
            "The female Northern Cardinal is more active late in the day, at least this one is.",
            page,
        )
        self.assertNotIn("Swipe across on a phone", page)
        self.assertNotIn('src="/about-species-card.png"', page)
        self.assertNotIn("data-future-feeder-photo", page)
        self.assertNotIn("data-future-pi-photo", page)
        self.assertLess(page.index("The camera"), page.index('src="/about-feeder.jpg"'))
        self.assertLess(page.index('src="/about-subscription.jpg"'), page.index('src="/about-feeder.jpg"'))
        self.assertLess(page.index('src="/about-feeder.jpg"'), page.index("Using an old Raspberry Pi"))
        self.assertLess(
            page.index("quick overview of the counts"),
            page.index('class="species-row species-card-shell about-species-card"'),
        )
        self.assertLess(
            page.index('class="species-row species-card-shell about-species-card"'),
            page.index("Why I’m sharing it"),
        )

    def test_about_species_card_uses_current_cardinal_observations(self) -> None:
        pairs = [
            GalleryPair(
                source_id="yard",
                pair_key="260811/070000_100_001_P",
                date_code="260811",
                time_code="070000",
                snapshot_path="yard/snaps/cardinal-male.jpg",
                video_path="yard/video/cardinal-male.mp4",
                is_bird=True,
                common_name="Northern Cardinal",
                scientific_name="Cardinalis cardinalis",
                sex="male",
            ),
            GalleryPair(
                source_id="yard",
                pair_key="260811/183000_100_002_P",
                date_code="260811",
                time_code="183000",
                video_path="yard/video/cardinal-female.mp4",
                is_bird=True,
                common_name="Northern Cardinal",
                scientific_name="Cardinalis cardinalis",
                sex="female",
            ),
        ]

        page = _render_about(pairs=pairs).decode("utf-8")

        self.assertIn('src="/media/yard/snaps/cardinal-male.jpg"', page)
        self.assertIn('<b>2</b> <span>videos</span>', page)
        self.assertIn('class="sex-key sex-key-male">1 male</span>', page)
        self.assertIn('class="sex-key sex-key-female">1 female</span>', page)
        self.assertIn("Northern Cardinal, female, 6 PM–7 PM: 1 bird", page)


class GallerySchemaTests(unittest.TestCase):
    def test_existing_boolean_star_row_migrates_to_a_count_of_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            card = root / "card"
            library = root / "library"
            (card / "video/260809").mkdir(parents=True)
            video = card / "video/260809/092443_150_031_P.mp4"
            video.write_bytes(b"video")
            MediaImporter(library).sync(FilesystemMediaSource(card, "yard"))

            connection = sqlite3.connect(library / "catalog.sqlite3")
            try:
                connection.execute(
                    """
                    CREATE TABLE stars (
                        source_id TEXT NOT NULL,
                        pair_key TEXT NOT NULL,
                        starred_at TEXT NOT NULL,
                        PRIMARY KEY (source_id, pair_key)
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO stars VALUES (?, ?, ?)",
                    ("yard", "260809/092443_150_031_P", "2026-08-09T12:00:00Z"),
                )
                connection.commit()
            finally:
                connection.close()

            pairs = list_pairs(library)
            self.assertEqual(pairs[0].star_count, 1)


class GalleryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        card = root / "card"
        self.card = card
        self.library = root / "library"
        (card / "video/260809").mkdir(parents=True)
        (card / "snaps/260809").mkdir(parents=True)
        self.video_relative = "yard/video/260809/092443_150_031_P.mp4"
        self.snapshot_relative = "yard/snaps/260809/092443_150_031_P.jpg"
        self.pair_key = "260809/092443_150_031_P"
        (card / "video/260809/092443_150_031_P.mp4").write_bytes(b"0123456789")
        (card / "snaps/260809/092443_150_031_P.jpg").write_bytes(b"jpeg-data")
        MediaImporter(self.library).sync(FilesystemMediaSource(card, "yard"))
        self.mobile_preparer = PassthroughMobilePreparer(self.library)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.library,
                mobile_preparer=self.mobile_preparer,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _post_star(self, starred: bool = True) -> dict[str, object]:
        return self._post_json(
            "/api/stars",
            {
                "camera_id": "yard",
                "pair_key": self.pair_key,
                "starred": starred,
            },
        )

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
        match = re.search(r'const csrfToken = "([^"]+)";', page)
        self.assertIsNotNone(match)
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "X-CSRF-Token": match.group(1),
            },
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_index_and_api_pair_snapshot_with_video(self) -> None:
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn(
                "default-src 'self'", response.headers["Content-Security-Policy"]
            )
        self.assertIn("Backyard Birds", page)
        self.assertIn("Toronto", page)
        self.assertIn('<a href="/about">About</a>', page)
        self.assertNotIn("Your local bird feeder archive", page)
        self.assertNotIn("Quiet moments, curious visitors", page)
        self.assertNotIn("The archive", page)
        self.assertNotIn("Showing <strong", page)
        self.assertNotIn('aria-label="Archive summary"', page)
        self.assertNotIn("data-total-count", page)
        self.assertNotIn("data-identified-count", page)
        self.assertNotIn("Watch clip", page)
        self.assertIn('<span class="time-chip">9:24 AM</span>', page)
        self.assertIn('<p class="capture-date">August 9, 2026</p>', page)
        self.assertNotIn("fonts.googleapis.com", page)
        self.assertIn(
            '<link rel="icon" href="/favicon.svg" type="image/svg+xml">', page
        )
        self.assertNotIn("letter-spacing", page)
        self.assertNotIn('<p class="camera">', page)
        self.assertIn('data-date="2026-08-09"', page)
        self.assertIn('data-sort', page)
        self.assertIn('data-filter', page)
        self.assertIn('data-date-from', page)
        self.assertIn("Identified species", page)
        self.assertIn("max-width: 1480px", page)
        self.assertIn("minmax(min(100%, 280px), 1fr)", page)
        self.assertIn('<label for="date-from">From</label>', page)
        self.assertIn('class="date-range" role="group"', page)
        self.assertEqual(page.count('class="date-empty-state"'), 2)
        self.assertIn("min-inline-size: 0; max-inline-size: 100%", page)
        self.assertIn("-webkit-appearance: none; appearance: none;", page)
        self.assertIn("padding-right: 38px; -webkit-appearance: none", page)
        self.assertIn("top: 50%; right: 15px; width: 7px; height: 7px;", page)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;", page)
        self.assertIn("syncDateControlStates();", page)
        self.assertIn('<label for="archive-filter">Filter</label>', page)
        self.assertIn('<option value="birds" selected>', page)
        self.assertIn(
            'document.querySelector("[data-filter]").value = "birds";', page
        )
        self.assertIn('class="card" data-card hidden', page)
        self.assertIn("Delete", page)
        self.assertIn("Permanently delete this capture?", page)

        with urlopen(self.base_url + "/api/media") as response:
            payload = json.load(response)
        self.assertEqual(len(payload["media"]), 1)
        self.assertEqual(payload["media"][0]["camera_id"], "yard")
        self.assertTrue(payload["media"][0]["snapshot_url"].endswith(".jpg"))
        self.assertTrue(payload["media"][0]["video_url"].endswith(".mp4"))
        self.assertTrue(payload["media"][0]["watch_url"].startswith("/watch/"))
        self.assertTrue(payload["media"][0]["download_url"].startswith("/download/"))
        self.assertFalse(payload["media"][0]["starred"])

        with urlopen(self.base_url + "/favicon.svg") as response:
            favicon = response.read().decode("utf-8")
            self.assertEqual(response.headers["Content-Type"], "image/svg+xml")
        self.assertIn('aria-label="Bird head"', favicon)
        self.assertIn('fill="#17362b"', favicon)

        with urlopen(self.base_url + "/about") as response:
            about = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        self.assertIn("<h1>What is this?</h1>", about)
        self.assertIn("How it works", about)
        self.assertIn('<img src="/about-feeder.jpg"', about)
        self.assertIn('<img src="/about-raspberry-pi.jpg"', about)
        self.assertIn('<img src="/about-amazon.png"', about)
        self.assertIn('<img src="/about-subscription.jpg"', about)
        self.assertIn('class="species-row species-card-shell about-species-card"', about)

        with urlopen(self.base_url + "/about-feeder.jpg") as response:
            feeder_photo = response.read()
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertTrue(feeder_photo.startswith(b"\xff\xd8"))

        with urlopen(self.base_url + "/about-raspberry-pi.jpg") as response:
            pi_photo = response.read()
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertTrue(pi_photo.startswith(b"\xff\xd8"))

        with urlopen(self.base_url + "/about-amazon.png") as response:
            amazon_photo = response.read()
            self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertTrue(amazon_photo.startswith(b"\x89PNG"))

        with urlopen(self.base_url + "/about-subscription.jpg") as response:
            subscription_photo = response.read()
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
        self.assertTrue(subscription_photo.startswith(b"\xff\xd8"))

    def test_watch_page_has_back_share_and_player_without_download(self) -> None:
        with urlopen(self.base_url + "/watch/" + self.video_relative) as response:
            page = response.read().decode("utf-8")
        self.assertIn("← Back to gallery", page)
        self.assertIn("<video controls", page)
        watch_actions = re.search(
            r'<div class="watch-actions">(.*?)</div>', page, re.DOTALL
        )
        self.assertIsNotNone(watch_actions)
        self.assertIn("☆ Star · 0", watch_actions.group(1))
        self.assertIn("<span data-share-label>Share</span>", page)
        self.assertIn('data-share-video', page)
        self.assertIn('data-preload-share', page)
        self.assertIn('data-video-url="/share/' + self.video_relative + '"', page)
        self.assertIn(
            'data-video-name="Bird_Toronto_2026-08-09_09-24-43.mp4"',
            page,
        )
        self.assertIn('files: [file]', page)
        self.assertIn('type: "video/mp4"', page)
        self.assertIn('await navigator.share(shareData)', page)
        self.assertIn('prepareShareFile(button).catch(() => {})', page)
        self.assertIn('Ready — tap Share', page)
        self.assertNotIn('url: window.location.href', page)
        self.assertNotIn('aria-label="Download video"', page)
        self.assertNotIn('> Download</a>', page)
        self.assertNotIn("Delete capture", watch_actions.group(1))
        self.assertIn("/media/" + self.video_relative, page)
        self.assertIn("padding-inline: 64px", page)
        self.assertIn("minmax(300px, 330px)", page)
        self.assertIn(".gallery-nav-previous { left: 10px; }", page)

    def test_watch_player_tracks_video_aspect_ratio_without_letterboxing(self) -> None:
        with urlopen(self.base_url + "/watch/" + self.video_relative) as response:
            page = response.read().decode("utf-8")

        self.assertIn('class="player" data-video-player', page)
        self.assertIn('video.addEventListener("loadedmetadata", fitPlayerToVideo)', page)
        self.assertIn("video.videoWidth / video.videoHeight", page)
        self.assertIn("player.style.aspectRatio", page)
        self.assertIn('player.style.removeProperty("width")', page)
        self.assertIn("Math.max(1, maximumHeight) * aspectRatio", page)
        self.assertIn("window.visualViewport?.addEventListener", page)
        self.assertIn("height: 100%; object-fit: cover", page)
        self.assertNotIn("max-height: calc(100vh - 104px)", page)
        self.assertNotIn("video { max-height: 68vh; }", page)

    def test_watch_page_counts_down_to_next_video_in_gallery_order(self) -> None:
        next_relative = "yard/video/260809/092000_100_030_P.mp4"
        previous_relative = "yard/video/260809/093000_100_030_P.mp4"
        (self.card / "video/260809/092000_100_030_P.mp4").write_bytes(b"next")
        (self.card / "snaps/260809/092000_100_030_P.jpg").write_bytes(b"next-jpeg")
        (self.card / "video/260809/093000_100_030_P.mp4").write_bytes(b"previous")
        (self.card / "snaps/260809/093000_100_030_P.jpg").write_bytes(
            b"previous-jpeg"
        )
        MediaImporter(self.library).sync(FilesystemMediaSource(self.card, "yard"))

        with urlopen(self.base_url + "/watch/" + self.video_relative) as response:
            page = response.read().decode("utf-8")
        self.assertIn('data-next-video', page)
        self.assertIn('data-countdown>5</strong>', page)
        self.assertIn('data-countdown-bar', page)
        self.assertIn('class="next-video-progress"', page)
        self.assertIn("Playing in", page)
        self.assertIn("Cancel", page)
        self.assertIn("Play now", page)
        self.assertIn("/watch/" + next_relative + "?autoplay=1", page)
        self.assertIn('video.addEventListener("timeupdate", updateCountdown)', page)
        self.assertIn('const countdownSeconds = 5', page)
        self.assertIn('countdownDeadline = Date.now()', page)
        self.assertIn('countdownDeadline - Date.now()', page)
        self.assertIn('countdownTimer = window.setInterval', page)
        self.assertNotIn('video.addEventListener("pause"', page)
        self.assertIn('video.addEventListener("ended", showCountdown)', page)
        self.assertIn('await video.play()', page)
        self.assertIn('source.setAttribute("src", nextSource.getAttribute("src"))', page)
        self.assertIn('window.history.pushState({}, "", historyUrl)', page)
        self.assertIn('data-play-next', page)
        self.assertIn("width: min(290px, calc(100% - 32px))", page)
        self.assertIn("min-height: 36px", page)
        self.assertIn('document.querySelector(".watch-info")', page)
        self.assertIn('class="gallery-nav gallery-nav-previous"', page)
        self.assertIn('href="/watch/' + previous_relative + '?autoplay=1"', page)
        self.assertIn('class="gallery-nav gallery-nav-next"', page)
        self.assertIn('href="/watch/' + next_relative + '?autoplay=1"', page)

        with urlopen(
            self.base_url + "/watch/" + next_relative + "?autoplay=1"
        ) as response:
            next_page = response.read().decode("utf-8")
        self.assertIn("data-watch-video autoplay", next_page)
        self.assertNotIn('data-next-video', next_page)

    def test_up_next_panel_adds_visitor_sex_to_species_name(self) -> None:
        current = GalleryPair(
            source_id="yard",
            pair_key="260809/092443_150_031_P",
            date_code="260809",
            time_code="092443",
            video_path=self.video_relative,
        )
        upcoming = GalleryPair(
            source_id="yard",
            pair_key="260809/092000_100_030_P",
            date_code="260809",
            time_code="092000",
            video_path="yard/video/260809/092000_100_030_P.mp4",
            is_bird=True,
            common_name="House Sparrow",
            scientific_name="Passer domesticus",
            sex="male",
        )

        page = _render_watch(current, "test-token", next_pair=upcoming).decode("utf-8")

        self.assertIn("<strong>House Sparrow · Male</strong>", page)

        animal = GalleryPair(
            source_id="yard",
            pair_key="260809/091500_100_029_P",
            date_code="260809",
            time_code="091500",
            video_path="yard/video/260809/091500_100_029_P.mp4",
            is_bird=False,
            common_name="Eastern gray squirrel",
            scientific_name="Sciurus carolinensis",
            sex="female",
        )
        animal_page = _render_watch(
            current, "test-token", next_pair=animal
        ).decode("utf-8")

        self.assertIn("<strong>Eastern gray squirrel · Female</strong>", animal_page)

    def test_species_metadata_appears_in_gallery_and_api(self) -> None:
        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            media_id = connection.execute(
                "SELECT id FROM media WHERE kind = 'snapshot'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO classifications (
                    media_id, provider, model, prompt_version, is_bird,
                    common_name, scientific_name, certainty, alternatives_json,
                    field_marks_json, notes, sex, age_class, bird_count, behavior,
                    sex_evidence, age_evidence, interesting_fact,
                    estimated_cost_usd, classified_at
                ) VALUES (?, 'openai', 'gpt-5.4-mini', 'bird-id-v2', 1,
                    'Black-capped Chickadee', 'Poecile atricapillus', 'likely',
                    '[]', '["black cap"]', 'Small songbird.', 'indeterminate',
                    'adult', 1, 'Feeding', 'Sexes look alike.',
                    'Adult plumage.', 'Chickadees cache food for later.', 0.001,
                    '2026-08-09T15:00:00+00:00')
                """,
                (media_id,),
            )
            connection.commit()
        finally:
            connection.close()

        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
        self.assertIn("Black-capped Chickadee", page)
        self.assertIn("Poecile atricapillus", page)
        self.assertNotIn("Likely identification", page)
        self.assertNotIn("Uncertain identification", page)
        self.assertIn('<dl class="classification-stats">', page)
        self.assertIn("<dt>Sex</dt><dd>Unknown</dd>", page)
        self.assertIn("<dt>Age</dt><dd>Adult</dd>", page)
        self.assertLess(page.index("<dt>Sex</dt>"), page.index("<dt>Age</dt>"))
        self.assertLess(page.index("<dt>Age</dt>"), page.index("<dt>Count</dt>"))
        self.assertNotIn("<span>Observed</span>", page)
        self.assertNotIn("<span>Species note</span>", page)
        self.assertNotIn("Chickadees cache food for later.", page)

        with urlopen(self.base_url + "/watch/" + self.video_relative) as response:
            watch_page = response.read().decode("utf-8")
        self.assertNotIn("Likely identification", watch_page)
        self.assertIn('<aside class="watch-info" aria-label="Capture details">', watch_page)
        self.assertIn("<h1>Black-capped Chickadee</h1>", watch_page)
        self.assertLess(
            watch_page.index('<div class="player" data-video-player>'),
            watch_page.index('<aside class="watch-info"'),
        )
        self.assertIn("<span>Observed</span><p>Feeding</p>", watch_page)
        self.assertIn("<span>Species note</span>", watch_page)
        self.assertIn("Chickadees cache food for later.", watch_page)
        self.assertLess(
            watch_page.index("<span>Species note</span>"),
            watch_page.index("<span>Observed</span>"),
        )
        self.assertIn('<div class="classification-notes">', watch_page)
        self.assertNotIn('class="classification-fact" style=', watch_page)

        with urlopen(self.base_url + "/api/media") as response:
            payload = json.load(response)
        classification = payload["media"][0]["classification"]
        self.assertEqual(classification["common_name"], "Black-capped Chickadee")
        self.assertEqual(classification["certainty"], "likely")
        self.assertEqual(classification["age_class"], "adult")
        self.assertEqual(classification["bird_count"], 1)
        self.assertEqual(
            classification["interesting_fact"], "Chickadees cache food for later."
        )

        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            connection.execute(
                """
                INSERT INTO classification_overrides (
                    media_id, common_name, scientific_name, certainty, notes,
                    sex, sex_evidence, interesting_fact, reason, corrected_at
                ) VALUES (?, 'Northern Cardinal', 'Cardinalis cardinalis',
                    'certain', 'Human-reviewed identification.', 'female',
                    'Orange-red bill and red in the wings and tail.',
                    'Female cardinals share the same warm red accents as males.',
                    'Confirmed from adjacent captures',
                    '2026-08-10T18:00:00+00:00')
                """,
                (media_id,),
            )
            connection.commit()
        finally:
            connection.close()

        with urlopen(self.base_url + "/") as response:
            corrected_page = response.read().decode("utf-8")
        self.assertIn("Northern Cardinal", corrected_page)
        self.assertIn("Cardinalis cardinalis", corrected_page)
        self.assertIn("<dt>Sex</dt><dd>Female</dd>", corrected_page)
        with urlopen(self.base_url + "/api/media") as response:
            corrected_payload = json.load(response)
        corrected = corrected_payload["media"][0]["classification"]
        self.assertEqual(corrected["common_name"], "Northern Cardinal")
        self.assertEqual(corrected["sex"], "female")

        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE classifications SET certainty = 'certain' WHERE media_id = ?",
                (media_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with urlopen(self.base_url + "/") as response:
            certain_page = response.read().decode("utf-8")
        self.assertNotIn("Certain identification", certain_page)
        self.assertNotIn("Likely identification", certain_page)

    def test_browser_contributions_increment_decrement_and_persist(self) -> None:
        self.assertEqual(self._post_star(), {"star_count": 1})
        self.assertEqual(self._post_star(), {"star_count": 2})
        self.assertEqual(self._post_star(False), {"star_count": 1})
        with urlopen(self.base_url + "/api/media") as response:
            payload = json.load(response)
        self.assertTrue(payload["media"][0]["starred"])
        self.assertEqual(payload["media"][0]["star_count"], 1)

        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
        self.assertIn("☆ Star · 1", page)
        self.assertIn('data-star-count="1"', page)
        self.assertNotIn('<strong data-star-count>1</strong> stars', page)
        self.assertIn('const starStorageKey = "bird-feeder-starred-v1";', page)
        self.assertIn("window.localStorage.setItem", page)
        self.assertIn("★ Starred · ${starCount}", page)
        self.assertIn("starred: starred", page)
        self.assertIn('option value="starred">Most starred', page)
        self.assertIn('option value="starred">Has stars', page)

        self.assertEqual(self._post_star(False), {"star_count": 0})
        self.assertEqual(self._post_star(False), {"star_count": 0})

    def test_download_uses_attachment_disposition(self) -> None:
        with urlopen(self.base_url + "/download/" + self.video_relative) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers["Content-Disposition"],
                'attachment; filename="092443_150_031_P.mp4"',
            )
            self.assertEqual(response.read(), b"0123456789")
        self.assertEqual(self.mobile_preparer.calls, [self.video_relative])

    def test_share_uses_prepared_inline_mp4(self) -> None:
        with urlopen(self.base_url + "/share/" + self.video_relative) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertIsNone(response.headers["Content-Disposition"])
            self.assertEqual(response.read(), b"0123456789")
        self.assertEqual(self.mobile_preparer.calls, [self.video_relative])

    def test_share_source_is_the_full_inline_mp4(self) -> None:
        with urlopen(self.base_url + "/media/" + self.video_relative) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertEqual(response.headers["Content-Length"], "10")
            self.assertIsNone(response.headers["Content-Disposition"])
            self.assertEqual(response.read(), b"0123456789")

    def test_delete_requires_confirmation_and_removes_pair(self) -> None:
        cached_mobile = self.library / "mobile-v2" / self.video_relative
        cached_mobile.parent.mkdir(parents=True)
        cached_mobile.write_bytes(b"phone-video")
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
        match = re.search(r'const csrfToken = "([^"]+)";', page)
        self.assertIsNotNone(match)
        request = Request(
            self.base_url + "/api/delete",
            data=json.dumps(
                {
                    "camera_id": "yard",
                    "pair_key": self.pair_key,
                    "confirmed": False,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "X-CSRF-Token": match.group(1),
            },
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        self.assertTrue((self.library / "media" / self.video_relative).is_file())

        self._post_star()
        payload = self._post_json(
            "/api/delete",
            {
                "camera_id": "yard",
                "pair_key": self.pair_key,
                "confirmed": True,
            },
        )
        self.assertEqual(payload["deleted"], True)
        self.assertEqual(payload["files"], 2)
        self.assertEqual(payload["bytes"], 19)
        self.assertFalse((self.library / "media" / self.video_relative).exists())
        self.assertFalse((self.library / "media" / self.snapshot_relative).exists())
        self.assertFalse(cached_mobile.exists())

        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            tombstone = connection.execute(
                """
                SELECT 1 FROM deleted_pairs
                WHERE source_id = ? AND pair_key = ?
                """,
                ("yard", self.pair_key),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(tombstone)

        reimported = MediaImporter(self.library).sync(
            FilesystemMediaSource(self.card, "yard")
        )
        self.assertEqual(reimported.suppressed, 2)
        self.assertEqual(reimported.imported, 0)
        self.assertFalse((self.library / "media" / self.video_relative).exists())
        self.assertFalse((self.library / "media" / self.snapshot_relative).exists())

        with urlopen(self.base_url + "/api/media") as response:
            self.assertEqual(json.load(response)["media"], [])
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/watch/" + self.video_relative)
        self.assertEqual(raised.exception.code, 404)

    def test_posts_require_same_origin_and_csrf_token(self) -> None:
        payload = json.dumps(
            {
                "camera_id": "yard",
                "pair_key": self.pair_key,
                "starred": True,
            }
        ).encode("utf-8")
        for headers in (
            {"Content-Type": "application/json"},
            {"Content-Type": "application/json", "Origin": self.base_url},
        ):
            with self.subTest(headers=headers):
                request = Request(self.base_url + "/api/stars", data=payload, headers=headers)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(raised.exception.code, 403)

    def test_rejects_unapproved_host_header(self) -> None:
        request = Request(self.base_url + "/", headers={"Host": "attacker.example"})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 421)

    def test_video_supports_single_byte_range(self) -> None:
        request = Request(
            self.base_url + "/media/" + self.video_relative,
            headers={"Range": "bytes=2-5"},
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.read(), b"2345")

    def test_does_not_serve_uncatalogued_or_traversal_paths(self) -> None:
        secret = self.library / "secret.txt"
        secret.write_text("not media")
        for path in (
            "/media/secret.txt",
            "/media/%2E%2E/secret.txt",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(self.base_url + path)
                self.assertEqual(raised.exception.code, 404)


class PublicGalleryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        card = root / "card"
        self.library = root / "library"
        (card / "video/260809").mkdir(parents=True)
        (card / "snaps/260809").mkdir(parents=True)
        self.video_relative = "yard/video/260809/092443_150_031_P.mp4"
        self.snapshot_relative = "yard/snaps/260809/092443_150_031_P.jpg"
        self.pair_key = "260809/092443_150_031_P"
        (card / "video/260809/092443_150_031_P.mp4").write_bytes(b"0123456789")
        (card / "snaps/260809/092443_150_031_P.jpg").write_bytes(b"jpeg-data")
        MediaImporter(self.library).sync(FilesystemMediaSource(card, "yard"))
        # Production runs the schema-owning LAN service before the public
        # service. Establish the same one-time schema boundary here.
        make_handler(self.library)
        self.catalog_mtime_ns = (self.library / "catalog.sqlite3").stat().st_mtime_ns

        self.mobile_preparer = PassthroughMobilePreparer(self.library)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.library,
                mobile_preparer=self.mobile_preparer,
                public_read_only=True,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _post_public_star(self, starred: bool = True) -> dict[str, object]:
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
        match = re.search(r'const csrfToken = "([^"]+)";', page)
        self.assertIsNotNone(match)
        request = Request(
            self.base_url + "/api/stars",
            data=json.dumps(
                {
                    "camera_id": "yard",
                    "pair_key": self.pair_key,
                    "starred": starred,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.base_url,
                "X-CSRF-Token": match.group(1),
            },
        )
        with urlopen(request) as response:
            return json.load(response)

    def test_public_pages_allow_stars_but_hide_admin_controls(self) -> None:
        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
            self.assertEqual(
                response.headers["X-Robots-Tag"],
                "noindex, nofollow, noarchive",
            )
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn('<p class="eyebrow">Toronto</p>', page)
        self.assertIn("data-star-button", page)
        self.assertNotIn("data-delete-button", page)
        self.assertIn("data-source-id", page)
        self.assertIn("data-pair-key", page)
        self.assertIn("csrfToken", page)
        self.assertNotIn("Watch clip", page)
        gallery = re.search(r'<section class="gallery".*?</section>', page, re.DOTALL)
        self.assertIsNotNone(gallery)
        self.assertNotIn("Share", gallery.group(0))
        self.assertIn('class="card-open-link" href="/watch/', gallery.group(0))
        self.assertNotIn("> Download</a>", page)

        with urlopen(self.base_url + "/watch/" + self.video_relative) as response:
            watch = response.read().decode("utf-8")
        self.assertIn("data-star-button", watch)
        self.assertNotIn("data-delete-button", watch)
        self.assertIn("data-share-video", watch)

    def test_public_star_clicks_toggle_a_browser_contribution(self) -> None:
        self.assertEqual(self._post_public_star(), {"star_count": 1})
        self.assertEqual(self._post_public_star(), {"star_count": 2})
        self.assertEqual(self._post_public_star(False), {"star_count": 1})

        with urlopen(self.base_url + "/") as response:
            page = response.read().decode("utf-8")
        self.assertIn("☆ Star · 1", page)
        self.assertNotIn('<strong data-star-count>1</strong> stars', page)
        self.assertIn("window.localStorage.setItem", page)

    def test_public_star_accepts_a_stale_token_and_previous_request_shape(self) -> None:
        payload = json.dumps(
            {"camera_id": "yard", "pair_key": self.pair_key}
        ).encode("utf-8")
        request = Request(
            self.base_url + "/api/stars",
            data=payload,
            headers={"Content-Type": "application/json", "Origin": self.base_url},
        )
        with urlopen(request) as response:
            self.assertEqual(json.load(response), {"star_count": 1})

        request = Request(
            self.base_url + "/api/stars",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 403)

    def test_public_robots_and_health_boundary(self) -> None:
        with urlopen(self.base_url + "/robots.txt") as response:
            self.assertEqual(response.read(), b"User-agent: *\nDisallow: /\n")
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/healthz")
        self.assertEqual(raised.exception.code, 404)
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/api/media")
        self.assertEqual(raised.exception.code, 404)

    def test_public_post_is_rejected_before_reading_or_mutating(self) -> None:
        request = Request(
            self.base_url + "/api/delete",
            data=json.dumps(
                {
                    "camera_id": "yard",
                    "pair_key": self.pair_key,
                    "confirmed": True,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 405)
        self.assertEqual(raised.exception.headers["Allow"], "GET, HEAD")
        self.assertTrue((self.library / "media" / self.video_relative).is_file())

        connection = sqlite3.connect(self.library / "catalog.sqlite3")
        try:
            count = connection.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 2)
        self.assertEqual(
            (self.library / "catalog.sqlite3").stat().st_mtime_ns,
            self.catalog_mtime_ns,
        )

        for method in ("DELETE", "OPTIONS", "PATCH", "PUT", "TRACE"):
            with self.subTest(method=method):
                request = Request(self.base_url + "/", method=method)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request)
                self.assertEqual(raised.exception.code, 405)
                self.assertEqual(raised.exception.headers["Allow"], "GET, HEAD")

    def test_public_head_and_download_do_not_write(self) -> None:
        request = Request(self.base_url + "/", method="HEAD")
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(int(response.headers["Content-Length"]), 0)
            self.assertEqual(response.read(), b"")

        request = Request(
            self.base_url + "/media/" + self.video_relative,
            headers={"Range": "bytes=2-5"},
            method="HEAD",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.read(), b"")

        with urlopen(self.base_url + "/download/" + self.video_relative) as response:
            self.assertEqual(response.read(), b"0123456789")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.mobile_preparer.cached_calls, [self.video_relative])
        self.assertEqual(self.mobile_preparer.calls, [])

    def test_public_share_never_falls_back_to_camera_original(self) -> None:
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/share/" + self.video_relative)

        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(self.mobile_preparer.cached_calls, [self.video_relative])
        self.assertEqual(self.mobile_preparer.calls, [])

    def test_public_prepared_share_is_cached_only_in_the_visitors_browser(self) -> None:
        self.mobile_preparer.cached_path = (
            self.library / "media" / self.video_relative
        ).resolve()

        with urlopen(self.base_url + "/share/" + self.video_relative) as response:
            self.assertEqual(response.read(), b"0123456789")
            self.assertEqual(
                response.headers["Cache-Control"],
                "private, max-age=3600",
            )

        self.assertEqual(self.mobile_preparer.cached_calls, [self.video_relative])
        self.assertEqual(self.mobile_preparer.calls, [])


if __name__ == "__main__":
    unittest.main()
