import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase, override_settings

from pipeline.detect_flood import change_mask, pick_pair

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


class ChangeDetectionTest(SimpleTestCase):
    """Before/after comparison on raw Sentinel-1 scale (uncalibrated amplitudes, squared)."""

    def scenes(self):
        rng = np.random.default_rng(0)

        def speckled(db):  # power with Sentinel-1-like speckle (4.4 looks)
            return 10 ** (db / 10) * rng.gamma(4.4, 1 / 4.4, db.shape)

        pre = np.full((120, 120), 50.0)  # bright farmland, ~50 dB on the raw scale
        pre[:, 100:104] = 38  # asphalt road: dark in both images
        post = pre.copy()
        post[10:60, 10:60] = 38  # flooded field: dark only in the flood image
        return speckled(pre), speckled(post)

    def test_new_water_flagged_dry_road_not(self):
        pre, post = self.scenes()
        mask, threshold = change_mask(pre, post)
        self.assertGreater((mask[15:55, 15:55] == 1).mean(), 0.95)  # flood found
        self.assertFalse((mask[:, 100:104] == 1).any())  # always-dark road is not water
        self.assertLess((mask[70:, :90] == 1).mean(), 0.01)  # dry field stays dry
        self.assertTrue(np.isfinite(threshold))

    def test_no_change_no_water(self):
        # A day without a flood: nothing got darker, so nothing is flagged, roads included.
        _, post = self.scenes()
        self.assertFalse((change_mask(post, post)[0] == 1).any())

    def test_outside_either_image_is_no_data(self):
        pre, post = self.scenes()
        pre[:, :20] = np.nan  # reference image doesn't reach this strip
        post[-20:, :] = np.nan  # flood image doesn't reach this strip
        mask, _ = change_mask(pre, post)
        self.assertTrue((mask[:, :20] == 255).all())
        self.assertTrue((mask[-20:, :] == 255).all())
        self.assertFalse((mask[:-20, 20:] == 255).any())

    def test_empty_scene_is_all_no_data(self):
        empty = np.full((5, 5), np.nan)
        self.assertTrue((change_mask(empty, empty)[0] == 255).all())


class PickPairTest(SimpleTestCase):
    """Reference image: newest pass on the same track before the flood date range."""

    RANGE = "2021-11-14T00:00:00Z/2021-11-30T23:59:59Z"

    def item(self, when, track, orbit="descending"):
        return {
            "assets": {"vv": {}},
            "properties": {
                "datetime": when,
                "sat:relative_orbit": track,
                "sat:orbit_state": orbit,
                "sar:instrument_mode": "IW",
            },
        }

    def test_same_track_reference_before_flood(self):
        during = [self.item("2021-11-16T14:20:31Z", 13), self.item("2021-11-20T01:54:01Z", 64)]
        before = [
            self.item("2021-10-23T14:20:32Z", 13),
            self.item("2021-11-04T14:20:31Z", 13),
            self.item("2021-11-08T01:54:01Z", 64),  # newer, but a different track
            self.item("2021-11-11T14:12:40Z", 115),
        ]
        with patch("pipeline.detect_flood.search_passes", side_effect=[during, before]):
            pre, post = pick_pair((0, 0, 1, 1), self.RANGE)
        self.assertEqual(post["properties"]["datetime"], "2021-11-16T14:20:31Z")
        self.assertEqual(pre["properties"]["datetime"], "2021-11-04T14:20:31Z")

    @patch("builtins.print")
    def test_no_reference_on_track(self, _print):
        during = [self.item("2021-11-16T14:20:31Z", 13)]
        before = [self.item("2021-11-08T01:54:01Z", 64)]
        with patch("pipeline.detect_flood.search_passes", side_effect=[during, before]):
            self.assertIsNone(pick_pair((0, 0, 1, 1), self.RANGE))


class RecentSearchTest(SimpleTestCase):
    """Uses the committed Sumas Prairie data, which is the hardcoded recent search."""

    def test_abbotsford_flood_offered_as_recent(self):
        response = self.client.get("/")
        self.assertContains(response, 'role="combobox"')
        self.assertContains(response, 'id="recent-sumas-prairie"')
        self.assertContains(response, "Sumas Prairie, Abbotsford")
        self.assertContains(response, "Nov 16, 2021, 6:20 AM PST")  # Sentinel-1B pass

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
