import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from . import data

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
        self.assertContains(response, 'href="?theme=light"')

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
