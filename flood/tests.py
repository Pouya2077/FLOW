import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
from django.test import SimpleTestCase, override_settings
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

from pipeline.build_facilities import address, dedupe, facilities_in_window, is_public, site
from pipeline.build_location import score_areas
from pipeline.detect_flood import NoImagery, change_mask, pick_pair
from pipeline.facility_kinds import KINDS_BY_SLUG, classify, query_tags

from . import data, jobs
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
FIXTURE_FACILITIES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.2, 49.05]},
            "properties": {"osm_id": "node/1", "kind": "hospital", "flood_status": "flooded"},
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
        (loc / "facilities.geojson").write_text(json.dumps(FIXTURE_FACILITIES))
        self.loc = loc

        searches = Path(tmp.name) / "searches"  # empty: no searches yet
        override = override_settings(FLOOD_DATA_DIR=Path(tmp.name), SEARCH_DATA_DIR=searches)
        override.enable()
        self.addCleanup(override.disable)
        data.reload()
        self.addCleanup(data.reload)

    def test_index(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_locations(self):
        body = self.client.get("/api/locations").json()
        self.assertEqual([loc["slug"] for loc in body], ["test-area"])

    def test_flood_in_view(self):
        body = self.client.get("/api/flood", {"bbox": "-122.25,49.0,-122.15,49.1"}).json()
        self.assertEqual(len(body["features"]), 1)

    def test_facilities_in_view(self):
        body = self.client.get("/api/facilities", {"bbox": "-122.25,49.0,-122.15,49.1"}).json()
        self.assertEqual([f["properties"]["kind"] for f in body["features"]], ["hospital"])

    def test_facilities_only_for_location(self):
        params = {"bbox": "-122.25,49.0,-122.15,49.1", "location": "elsewhere"}
        self.assertEqual(self.client.get("/api/facilities", params).json()["features"], [])

    def test_facilities_out_of_view(self):
        body = self.client.get("/api/facilities", {"bbox": "0,0,1,1"}).json()
        self.assertEqual(body["features"], [])

    def test_facilities_missing_file_is_empty(self):
        (self.loc / "facilities.geojson").unlink()
        body = self.client.get("/api/facilities", {"bbox": "-122.25,49.0,-122.15,49.1"}).json()
        self.assertEqual(body["features"], [])

    def test_facilities_bad_bbox(self):
        self.assertEqual(self.client.get("/api/facilities", {"bbox": "1,2"}).status_code, 400)

    def test_flood_out_of_view(self):
        body = self.client.get("/api/flood", {"bbox": "0,0,1,1"}).json()
        self.assertEqual(body["features"], [])

    def test_flood_only_for_current_location(self):
        view = {"bbox": "-122.25,49.0,-122.15,49.1"}
        mine = self.client.get("/api/flood", {**view, "location": "test-area"}).json()
        other = self.client.get("/api/flood", {**view, "location": "elsewhere"}).json()
        self.assertEqual(len(mine["features"]), 1)
        self.assertEqual(other["features"], [])
        streets = self.client.get("/api/streets", {**view, "location": "elsewhere"}).json()
        self.assertEqual(streets, [])

    def test_bad_bbox(self):
        self.assertEqual(self.client.get("/api/flood", {"bbox": "nope"}).status_code, 400)

    def test_theme_link_keeps_location(self):
        response = self.client.get("/", {"location": "test-area"})
        self.assertContains(response, 'href="?theme=dark&amp;location=test-area"')

    def test_search_query_prefilled_and_kept(self):
        response = self.client.get("/", {"q": "Abbotsford"})
        self.assertContains(response, 'value="Abbotsford"')
        self.assertContains(response, "q=Abbotsford")

    # Searches are analysed in the background (/api/analyze); the page load never waits on it.
    @patch("pipeline.detect_flood.fetch_sentinel_radar")
    @patch("flood.jobs.fetch_sentinel_radar")
    def test_search_page_load_does_not_run_pipeline(self, job_run, direct_run):
        self.client.get("/", {"q": "Sumas Prairie"})
        job_run.assert_not_called()
        direct_run.assert_not_called()


class DataFoldersTest(SimpleTestCase):
    """Committed demo locations and searched areas are read side by side."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.demo, self.searches = Path(tmp.name, "locations"), Path(tmp.name, "searches")
        override = override_settings(FLOOD_DATA_DIR=self.demo, SEARCH_DATA_DIR=self.searches)
        override.enable()
        self.addCleanup(override.disable)
        data.reload()
        self.addCleanup(data.reload)

    def write(self, root, slug, name):
        folder = root / slug
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(json.dumps({**FIXTURE_META, "name": name}))
        (folder / "segments.geojson").write_text(json.dumps(FIXTURE_SEGMENTS))
        (folder / "streets.json").write_text(json.dumps(FIXTURE_STREETS))

    def test_searches_listed_with_demo(self):
        self.write(self.demo, "sumas-prairie", "Demo")
        self.write(self.searches, "chilliwack", "Searched")
        self.assertEqual(list(data.locations()), ["chilliwack", "sumas-prairie"])
        self.assertEqual(data.streets("chilliwack")[0]["name"], "Test Rd")

    def test_demo_wins_on_same_slug(self):
        self.write(self.demo, "sumas-prairie", "Demo")
        self.write(self.searches, "sumas-prairie", "Searched")
        self.assertEqual(data.locations()["sumas-prairie"]["name"], "Demo")

    def test_reload_picks_up_new_search(self):
        self.assertEqual(data.locations(), {})
        self.write(self.searches, "chilliwack", "Searched")
        self.assertEqual(data.locations(), {})  # still cached
        data.reload()
        self.assertIn("chilliwack", data.locations())


def run_inline(fn, *args):  # stands in for the background thread
    fn(*args)


@patch("flood.jobs._submit", side_effect=run_inline)
class AnalysisJobTest(SimpleTestCase):
    """A search starts a background pipeline run; the page polls its status."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.searches = Path(tmp.name, "searches")
        override = override_settings(
            FLOOD_DATA_DIR=Path(tmp.name, "locations"), SEARCH_DATA_DIR=self.searches
        )
        override.enable()
        self.addCleanup(override.disable)
        data.reload()
        self.addCleanup(data.reload)
        jobs._jobs.clear()
        self.addCleanup(jobs._jobs.clear)

    def fake_pipeline(self, region, date_range, out_dir):  # writes what build() would
        self.assertIsNone(date_range)  # searches analyse the newest pass
        folder = out_dir / region.slug
        folder.mkdir(parents=True)
        meta = {"name": region.name, "bbox": list(region.bbox), "timezone": region.timezone}
        (folder / "meta.json").write_text(json.dumps(meta))
        (folder / "segments.geojson").write_text(json.dumps(FIXTURE_SEGMENTS))
        (folder / "streets.json").write_text(json.dumps(FIXTURE_STREETS))
        return folder

    def analyze(self, name="Chilliwack, BC"):
        return self.client.post(f"/api/analyze?name={name}&lon=-121.95&lat=49.16")

    def test_search_runs_pipeline_and_shows_up(self, _submit):
        with patch("flood.jobs.fetch_sentinel_radar", side_effect=self.fake_pipeline):
            body = self.analyze().json()
        self.assertEqual(body["slug"], "chilliwack-bc")
        status = self.client.get("/api/analyze/chilliwack-bc").json()
        self.assertEqual(status["status"], "done")
        slugs = [loc["slug"] for loc in self.client.get("/api/locations").json()]
        self.assertEqual(slugs, ["chilliwack-bc"])  # data reloaded, no restart needed

    def test_no_imagery_reason_shown(self, _submit):
        reason = "No Sentinel-1 pass over this area in the selected period."
        with patch("flood.jobs.fetch_sentinel_radar", side_effect=NoImagery(reason)):
            self.analyze()
        status = self.client.get("/api/analyze/chilliwack-bc").json()
        self.assertEqual((status["status"], status["message"]), ("failed", reason))

    def test_unexpected_error_logged_with_generic_message(self, _submit):
        with (
            patch("flood.jobs.fetch_sentinel_radar", side_effect=RuntimeError("boom")),
            self.assertLogs("flood.jobs", "ERROR"),
        ):
            self.analyze()
        status = self.client.get("/api/analyze/chilliwack-bc").json()
        self.assertEqual((status["status"], status["message"]), ("failed", jobs.FAILED_MESSAGE))

    def test_already_analysed_area_not_rerun(self, _submit):
        with patch("flood.jobs.fetch_sentinel_radar", side_effect=self.fake_pipeline) as run:
            self.analyze()
            data.reload()
            jobs._jobs.clear()  # e.g. after a runserver restart
            self.assertEqual(self.analyze().json()["status"], "done")
        run.assert_called_once()

    def test_finished_search_not_rerun_in_same_process(self, _submit):
        with patch("flood.jobs.fetch_sentinel_radar", side_effect=self.fake_pipeline) as run:
            self.analyze()
            self.assertEqual(self.analyze().json()["status"], "done")
        run.assert_called_once()

    def test_running_job_not_started_twice(self, _submit):
        _submit.side_effect = None  # leave the job queued
        with patch("flood.jobs.fetch_sentinel_radar") as run:
            self.assertEqual(self.analyze().json()["status"], "queued")
            self.assertEqual(self.analyze().json()["status"], "queued")
        _submit.assert_called_once()
        run.assert_not_called()

    def test_failed_search_can_be_retried(self, _submit):
        with patch("flood.jobs.fetch_sentinel_radar", side_effect=NoImagery("none yet")):
            self.analyze()
        with patch("flood.jobs.fetch_sentinel_radar", side_effect=self.fake_pipeline):
            self.analyze()
        self.assertEqual(self.client.get("/api/analyze/chilliwack-bc").json()["status"], "done")

    def test_unknown_analysis_404(self, _submit):
        self.assertEqual(self.client.get("/api/analyze/nowhere").status_code, 404)

    def test_bad_coordinates_400(self, _submit):
        response = self.client.post("/api/analyze?name=X&lon=-121.95&lat=95")
        self.assertEqual(response.status_code, 400)


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

    def test_no_reference_on_track(self):
        during = [self.item("2021-11-16T14:20:31Z", 13)]
        before = [self.item("2021-11-08T01:54:01Z", 64)]
        with patch("pipeline.detect_flood.search_passes", side_effect=[during, before]):
            with self.assertRaisesMessage(NoImagery, "same orbit track"):
                pick_pair((0, 0, 1, 1), self.RANGE)


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

    def assertTheme(self, response, theme):
        """One theme circle: its icon shows the current mode and it links to the other mode."""
        self.assertEqual(response.context["theme"], theme)
        self.assertContains(response, f'data-theme="{theme}"')
        other = "dark" if theme == "light" else "light"
        label = f"Switch to {other} mode"
        self.assertContains(
            response, f'data-theme-option="{other}" aria-label="{label}" title="{label}"'
        )
        self.assertContains(response, 'class="theme-option', count=1)
        self.assertContains(response, f'class="icon-{"sun" if theme == "light" else "moon"}"')

    def test_default_is_light(self):
        response = self.get()
        self.assertTheme(response, "light")
        self.assertNotIn("theme", response.cookies)

    def test_param_dark_sets_cookie(self):
        response = self.get(theme="dark")
        self.assertTheme(response, "dark")
        self.assertEqual(response.cookies["theme"].value, "dark")
        self.assertContains(response, 'href="?theme=light&amp;location=')

    def test_cookie_used_without_param(self):
        self.client.cookies["theme"] = "dark"
        self.assertTheme(self.get(), "dark")

    def test_param_overrides_cookie(self):
        self.client.cookies["theme"] = "dark"
        response = self.get(theme="light")
        self.assertTheme(response, "light")
        self.assertEqual(response.cookies["theme"].value, "light")

    def test_invalid_param_falls_back_to_light(self):
        response = self.get(theme="purple")
        self.assertTheme(response, "light")
        self.assertNotIn("theme", response.cookies)

    def test_invalid_cookie_falls_back_to_light(self):
        self.client.cookies["theme"] = "purple"
        self.assertTheme(self.get(), "light")


class FacilityTest(SimpleTestCase):
    """Critical buildings: OSM tags -> kind, public filter, window clipping and flood scoring."""

    def test_first_matching_kind_wins(self):
        self.assertEqual(classify({"amenity": "hospital", "healthcare": "clinic"}).slug, "hospital")
        self.assertEqual(classify({"emergency": "assembly_point"}).slug, "shelter")
        self.assertIsNone(classify({"amenity": "shelter"}))  # bus and picnic shelters

    def test_query_has_every_kinds_tags(self):
        tags = query_tags()
        self.assertIn("fire_station", tags["amenity"])
        self.assertIn("dyke", tags["man_made"])

    def test_private_dropped(self):
        self.assertTrue(is_public({"amenity": "school"}))
        self.assertFalse(is_public({"amenity": "school", "operator:type": "private"}))
        self.assertFalse(is_public({"amenity": "fuel", "access": "private"}))

    def test_address(self):
        tags = {"addr:housenumber": "1", "addr:street": "Vye Rd", "addr:city": "Abbotsford"}
        self.assertEqual(address(tags), "1 Vye Rd, Abbotsford")
        self.assertIsNone(address({"addr:city": "Abbotsford"}))  # a city alone isn't an address

    def osm(self, rows):
        frame = gpd.GeoDataFrame(
            [tags for _, tags, _ in rows], geometry=[g for _, _, g in rows], crs=32610
        )
        frame.index = [(element, i) for i, (element, _, _) in enumerate(rows)]
        return frame

    def test_window_keeps_inside_and_clips_lines(self):
        rows = facilities_in_window(
            self.osm(
                [
                    ("node", {"amenity": "police"}, Point(50, 50)),
                    ("node", {"amenity": "police"}, Point(150, 50)),  # outside
                    ("way", {"man_made": "dyke"}, LineString([(-50, 10), (50, 10)])),
                ]
            ),
            box(0, 0, 100, 100),
            32610,
        )
        self.assertEqual([r["kind"].slug for r in rows], ["police", "dyke"])
        self.assertAlmostEqual(rows[1]["geometry"].length, 50)

    def test_node_and_building_merged(self):
        police = KINDS_BY_SLUG["police"]
        node = {"osm_id": "node/1", "tags": {"phone": "1"}, "geometry": Point(5, 5)}
        way = {"osm_id": "way/2", "tags": {"name": "HQ"}, "geometry": box(0, 0, 9, 9)}
        rows = dedupe([node | {"kind": police}, way | {"kind": police}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["osm_id"], "way/2")  # the outline is kept
        self.assertEqual(rows[0]["tags"], {"name": "HQ", "phone": "1"})

    def test_site_scored_against_mask(self):
        mask = np.zeros((20, 20), dtype="uint8")
        mask[:, :10] = 1  # west half water
        mask[:, 18:] = 255  # east edge unobserved
        transform = from_origin(0, 200, 10, 10)  # 10 m pixels
        water = gpd.GeoDataFrame(geometry=[], crs=32610)
        status, fraction, _ = score_areas(
            [site(Point(40, 100)), site(Point(140, 100)), box(185, 50, 200, 150)],
            mask,
            transform,
            water,
        )
        self.assertEqual(status, ["flooded", "clear", "no_data"])
        self.assertEqual(fraction[1], 0)
