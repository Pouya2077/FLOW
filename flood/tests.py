import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from pipeline.detect_flood import water_mask

from . import data
from .views import observed_local

FIXTURE_META = {"name": "Test Area", "bbox": [-122.3, 49.0, -122.1, 49.1]}
FIXTURE_SEGMENTS = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[-122.2, 49.05], [-122.19, 49.05]]},
            "properties": {"name": "Test Rd", "status": "flooded", "flooded_fraction": 0.8},
        }
    ],
}
FIXTURE_STREETS = [
    {"name": "Test Rd", "total_m": 800, "flooded_m": 640, "bbox": [-122.2, 49.05, -122.19, 49.05]}
]


class ApiSmokeTest(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        loc = Path(tmp.name) / "test-area"
        loc.mkdir()
        (loc / "meta.json").write_text(json.dumps(FIXTURE_META))
        (loc / "segments.geojson").write_text(json.dumps(FIXTURE_SEGMENTS))
        (loc / "streets.json").write_text(json.dumps(FIXTURE_STREETS))

        override = override_settings(FLOOD_DATA_DIR=Path(tmp.name))
        override.enable()
        self.addCleanup(override.disable)
        for fn in (data.locations, data.segments, data.streets):
            fn.cache_clear()
            self.addCleanup(fn.cache_clear)

    def test_index(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_locations(self):
        body = self.client.get("/api/locations").json()
        self.assertEqual([loc["slug"] for loc in body], ["test-area"])

    def test_flood_in_view(self):
        body = self.client.get("/api/flood", {"bbox": "-122.25,49.0,-122.15,49.1"}).json()
        self.assertEqual(len(body["features"]), 1)

    def test_flood_out_of_view(self):
        body = self.client.get("/api/flood", {"bbox": "0,0,1,1"}).json()
        self.assertEqual(body["features"], [])

    def test_bad_bbox(self):
        self.assertEqual(self.client.get("/api/flood", {"bbox": "nope"}).status_code, 400)

    def test_theme_link_keeps_location(self):
        response = self.client.get("/", {"location": "test-area"})
        self.assertContains(response, 'href="?theme=dark&amp;location=test-area"')

    # A ?q= page load geocodes with Nominatim and downloads radar; stand in for both so tests stay
    # offline and don't need CDSE credentials.
    @patch("flood.views.fetch_sentinel_radar", return_value=None)
    @patch("flood.views.get_10km_range", return_value=[-122.4, 48.9, -122.1, 49.1])
    def test_search_query_prefilled_and_kept(self, geocode, fetch_radar):
        response = self.client.get("/", {"q": "Abbotsford"})
        self.assertContains(response, 'value="Abbotsford"')
        self.assertContains(response, "q=Abbotsford")
        geocode.assert_called_once_with("Abbotsford")
        fetch_radar.assert_not_called()  # not a region in pipeline/regions.py

    @patch("flood.views.fetch_sentinel_radar", return_value=None)
    @patch("flood.views.get_10km_range", return_value=[-122.4, 48.9, -122.1, 49.1])
    def test_search_for_region_runs_pipeline_on_region(self, geocode, fetch_radar):
        self.client.get("/", {"q": "Sumas Prairie"})
        fetch_radar.assert_called_once_with(region_slug="sumas-prairie")


class WaterMaskTest(SimpleTestCase):
    """Raw Sentinel-1 GRD pixels are uncalibrated amplitudes in the hundreds, not 0-1."""

    def test_dark_pixels_are_water_on_raw_scale(self):
        rng = np.random.default_rng(0)
        raw = rng.gamma(4.4, 300 / 4.4, (100, 100))  # bright fields, speckled
        raw[:40, :40] = rng.gamma(4.4, 40 / 4.4, (40, 40))  # dark water patch
        raw[:, -10:] = 0  # GRD fill: outside the scene
        mask, threshold = water_mask(raw.astype("uint16"))
        self.assertGreater((mask[:40, :40] == 1).mean(), 0.9)
        self.assertLess((mask[50:, :80] == 1).mean(), 0.05)
        self.assertTrue((mask[:, -10:] == 255).all())
        self.assertTrue(np.isfinite(threshold))

    def test_empty_scene_is_all_no_data(self):
        mask, _ = water_mask(np.zeros((5, 5), dtype="uint16"))
        self.assertTrue((mask == 255).all())


class RecentSearchTest(SimpleTestCase):
    """Uses the committed Sumas Prairie data, which is the hardcoded recent search."""

    def test_abbotsford_flood_offered_as_recent(self):
        response = self.client.get("/")
        self.assertContains(response, 'role="combobox"')
        self.assertContains(response, 'id="recent-sumas-prairie"')
        self.assertContains(response, "Sumas Prairie, Abbotsford")
        self.assertContains(response, "Nov 16, 2021, 6:25 AM PST")

    def test_window_on_map_is_marked_selected(self):
        response = self.client.get("/", {"location": "sumas-prairie"})
        self.assertContains(response, 'data-slug="sumas-prairie" aria-selected="true"')


class ObservedLocalTest(SimpleTestCase):
    def test_local_time_and_zone(self):
        meta = {"observed_utc": "2021-11-16T14:25:00Z", "timezone": "America/Vancouver"}
        self.assertEqual(observed_local(meta), "Nov 16, 2021, 6:25 AM PST")

    def test_utc_without_timezone(self):
        self.assertEqual(
            observed_local({"observed_utc": "2021-11-16T14:25:00Z"}), "Nov 16, 2021, 2:25 PM UTC"
        )

    def test_empty_without_time(self):
        self.assertEqual(observed_local({}), "")


class ThemeTest(SimpleTestCase):
    def get(self, **params):
        return self.client.get("/", params)

    def assertTheme(self, response, theme, icon):
        self.assertEqual(response.context["theme"], theme)
        self.assertContains(response, f'data-theme="{theme}"')
        self.assertContains(response, f'class="icon-{icon}"')

    def test_default_is_light(self):
        response = self.get()
        self.assertTheme(response, "light", "moon")
        self.assertNotIn("theme", response.cookies)

    def test_param_dark_sets_cookie(self):
        response = self.get(theme="dark")
        self.assertTheme(response, "dark", "sun")
        self.assertEqual(response.cookies["theme"].value, "dark")
        self.assertContains(response, 'href="?theme=light&amp;location=')

    def test_cookie_used_without_param(self):
        self.client.cookies["theme"] = "dark"
        self.assertTheme(self.get(), "dark", "sun")

    def test_param_overrides_cookie(self):
        self.client.cookies["theme"] = "dark"
        response = self.get(theme="light")
        self.assertTheme(response, "light", "moon")
        self.assertEqual(response.cookies["theme"].value, "light")

    def test_invalid_param_falls_back_to_light(self):
        response = self.get(theme="purple")
        self.assertTheme(response, "light", "moon")
        self.assertNotIn("theme", response.cookies)

    def test_invalid_cookie_falls_back_to_light(self):
        self.client.cookies["theme"] = "purple"
        self.assertTheme(self.get(), "light", "moon")
