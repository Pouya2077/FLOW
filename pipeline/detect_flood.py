import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import rasterio
import requests
from dotenv import load_dotenv
from rasterio.enums import Resampling
from rasterio.env import Env
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from scipy import ndimage

from pipeline.build_location import OUT_DIR, build

# Import the teammate's CRS function
from pipeline.map_mask import MASK_DIR, utm_crs
from pipeline.regions import Region, get_region

# Load environment variables once when the module is imported
load_dotenv()

PIXEL_M = 10  # matches Sentinel-1 GRD resolution
SMOOTH_PX = 5  # speckle smoothing window (50 m); radar is too grainy to compare single pixels
DROP_DB = 3.0  # minimum darkening vs the reference image to count as new water
REFERENCE_DAYS = 60  # how far before the flood to look for a reference image
MIN_PATCH_PX = 20  # water patches smaller than this (0.2 ha) are leftover speckle

# Change detection: To know if a 'dark spot' is actually water, we compare 2 images, before and
# after. Places in the 'after' image that are actually darker than the 'before' image are
# marked as water. This ensures that dry roads are not all just marked as water because they
# are dark.
#
# Raw GRD pixels are uncalibrated amplitudes (whole numbers, typically 50-500). The 'is flooded'
# threshold is on a different scale, so we need to convert the amplitudes to this scale before
# we compare them to the threshold and mark areas as flooded.


# Sentinel-1 passes over the bbox in the date range, oldest first
def search_passes(bbox, date_range: str) -> list[dict]:
    search_url = "https://stac.dataspace.copernicus.eu/v1/search"
    search_payload = {
        "collections": ["sentinel-1-grd"],
        "bbox": list(bbox),
        "datetime": date_range,
        "limit": 100,
    }
    search_response = requests.post(search_url, json=search_payload, timeout=30)
    search_response.raise_for_status()
    passes = [
        f
        for f in search_response.json().get("features", [])
        if "vv" in f["assets"] and f["properties"].get("sar:instrument_mode") == "IW"
    ]
    return sorted(passes, key=lambda f: f["properties"]["datetime"])


# Passes on the same track see the ground from the same angle, so their brightness is comparable
def track(feature: dict) -> tuple:
    p = feature["properties"]
    return p["sat:relative_orbit"], p["sat:orbit_state"]


# Raised when there are no usable satellite images; the message is shown to the user
class NoImagery(Exception):
    pass


# The image to analyse and a reference image (latest pass on the same track in the REFERENCE_DAYS
# before it). For a past flood take the first pass in the range (closest to the peak); for a
# search take the newest pass.
def pick_pair(bbox, date_range: str, newest: bool = False) -> tuple[dict, dict]:
    during = search_passes(bbox, date_range)
    if not during:
        raise NoImagery("No Sentinel-1 pass over this area in the selected period.")
    post = during[-1] if newest else during[0]

    taken = parse_utc(post["properties"]["datetime"])
    before = f"{iso(taken - timedelta(days=REFERENCE_DAYS))}/{iso(taken - timedelta(hours=1))}"
    same_track = [f for f in search_passes(bbox, before) if track(f) == track(post)]
    if not same_track:
        raise NoImagery(
            f"No earlier pass on the same orbit track in the {REFERENCE_DAYS} days before "
            f"{taken:%b %d, %Y} to compare against."
        )
    return same_track[-1], post


# The newest passes: Sentinel-1 revisits every 6-12 days, so look back two weeks
def latest_range(days: int = 14) -> str:
    now = datetime.now(UTC)
    return f"{iso(now - timedelta(days=days))}/{iso(now)}"


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


# The pixel grid both images are warped onto: the region's bbox in its UTM zone, 10 m pixels.
# Using one grid for both is what lets us compare them pixel by pixel.
def region_grid(bbox):
    crs = utm_crs((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", crs, *bbox)
    minx = math.floor(minx / PIXEL_M) * PIXEL_M
    maxy = math.ceil(maxy / PIXEL_M) * PIXEL_M
    width = math.ceil((maxx - minx) / PIXEL_M)
    height = math.ceil((maxy - miny) / PIXEL_M)
    return crs, from_origin(minx, maxy, PIXEL_M, PIXEL_M), width, height


# Stream only the region's pixels of a remote scene (it is 600 MB+) and warp the sideways radar
# image onto the grid. Returns linear power (amplitude squared), NaN outside the scene.
def stream_power(feature: dict, grid, access_token: str) -> np.ndarray:
    crs, transform, width, height = grid
    bounds = (
        transform.c,
        transform.f - height * PIXEL_M,
        transform.c + width * PIXEL_M,
        transform.f,
    )
    download_url = feature["assets"]["vv"]["alternate"]["https"]["href"]
    # Pass the auth token to GDAL so it can read the remote file securely
    with (
        Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {access_token}"),
        rasterio.open(download_url) as src,
        # Let GDAL pick the warped grid (forcing ours onto a GCP-referenced scene reads all
        # zeros). nodata=0: keep the scene's 0-valued fill out of the interpolation, so the
        # scene edge doesn't blend into dark "water" pixels.
        WarpedVRT(src, crs=crs, resampling=Resampling.bilinear, src_nodata=0, nodata=0) as vrt,
    ):
        # Stream ONLY the pixels around the region (a 2-pixel margin for the resampling below)
        window = from_bounds(*bounds, transform=vrt.transform)
        window = Window(window.col_off - 2, window.row_off - 2, window.width + 4, window.height + 4)
        window = window.round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, vrt.width, vrt.height))  # region may overhang
        raw = vrt.read(1, window=window)
        raw_transform = vrt.window_transform(window)

    # Snap onto the exact region grid, so the reference and flood images line up pixel for pixel
    amplitude = np.zeros((height, width), dtype="float32")
    reproject(
        raw.astype("float32"),
        amplitude,
        src_transform=raw_transform,
        src_crs=crs,
        dst_transform=transform,
        dst_crs=crs,
        src_nodata=0,
        dst_nodata=0,
        resampling=Resampling.bilinear,
    )
    power = amplitude**2
    power[amplitude == 0] = np.nan
    return power


# Average power over a SMOOTH_PX window (multilooking) to even out speckle; NaN pixels are ignored
def smooth(power: np.ndarray) -> np.ndarray:
    valid = np.isfinite(power)
    total = ndimage.uniform_filter(np.where(valid, power, 0).astype("float64"), SMOOTH_PX)
    count = ndimage.uniform_filter(valid.astype("float64"), SMOOTH_PX)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(valid, total / count, np.nan).astype("float32")


# Convert power to db; raw pixels are on a different scale that will not match a fixed threshold
def to_db(power: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10 * np.log10(power)  # dB relative to an unknown constant (uncalibrated)


# Value that best splits values into two groups (maximum between-group variance).
def otsu_threshold(values: np.ndarray, bins: int = 256) -> float:
    lo, hi = np.percentile(values, [0.5, 99.5])  # ignore extreme outliers
    hist, edges = np.histogram(values, bins=bins, range=(lo, hi))
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)  # pixels at or below each candidate threshold
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * centers)
    m0 = s0 / np.maximum(w0, 1)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1)
    return float(centers[np.argmax(w0 * w1 * (m0 - m1) ** 2)])


# Drop connected water patches smaller than min_px (8-connected)
def remove_small(water: np.ndarray, min_px: int = MIN_PATCH_PX) -> np.ndarray:
    labels, _ = ndimage.label(water, structure=np.ones((3, 3)))
    keep = np.bincount(labels.ravel()) >= min_px
    keep[0] = False
    return keep[labels]


# 0 = dry, 1 = water, 255 = no data (outside either image). Returns the dark threshold (dB) too.
def change_mask(pre_power: np.ndarray, post_power: np.ndarray) -> tuple[np.ndarray, float]:
    pre_db, post_db = to_db(smooth(pre_power)), to_db(smooth(post_power))
    seen = np.isfinite(pre_db) & np.isfinite(post_db)
    utm_mask = np.full(seen.shape, 255, dtype=np.uint8)
    if not seen.any():
        return utm_mask, float("nan")

    threshold = otsu_threshold(post_db[seen])
    with np.errstate(invalid="ignore"):
        water = seen & (post_db < threshold) & (post_db - pre_db < -DROP_DB)
    water = remove_small(water)

    utm_mask[seen] = 0
    utm_mask[water] = 1
    return utm_mask, threshold


def satellite_name(feature: dict) -> str:
    platform = feature["properties"].get("platform", "sentinel-1")  # e.g. "sentinel-1b"
    return "Sentinel-" + platform.removeprefix("sentinel-").upper()


"""
    Fetches an image and a pre-flood reference image of a region (a slug from
    pipeline/regions.py, or a Region built from a search).
    Turns them into a map-aligned, flood mask using change detection.
    Automatically runs build_location upon completion, writing <out_dir>/<slug>/.
    date_range=None analyses the newest pass. Raises NoImagery if there is nothing to compare.
"""


def fetch_sentinel_radar(
    region: Region | str = "sumas-prairie",
    date_range: str | None = "2021-11-14T00:00:00Z/2021-11-30T23:59:59Z",
    out_dir: Path = OUT_DIR,
) -> Path:
    if isinstance(region, str):
        region = get_region(region)
    bbox = region.bbox

    username = os.getenv("CDSE_USERNAME")
    password = os.getenv("CDSE_PASSWORD")

    if not username or not password:
        raise ValueError("Missing CDSE_USERNAME or CDSE_PASSWORD in .env file.")

    # 1. Find the flood image and a reference image on the same track (search needs no login)
    print(f"Searching for Sentinel-1 data for bbox {bbox}...")
    if date_range is None:
        pre, post = pick_pair(bbox, latest_range(), newest=True)
    else:
        pre, post = pick_pair(bbox, date_range)
    pre_time, post_time = pre["properties"]["datetime"], post["properties"]["datetime"]
    relative_orbit, orbit = track(post)
    print(f"Flood image:     {post_time} ({satellite_name(post)}, {orbit}, track {relative_orbit})")
    print(f"Reference image: {pre_time} ({satellite_name(pre)})")

    # 2. Authenticate and Get Bearer Token (needed to read the image files)
    print("Authenticating with Copernicus...")
    token_url = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )
    auth_payload = {
        "client_id": "cdse-public",
        "grant_type": "password",
        "username": username,
        "password": password,
    }

    auth_response = requests.post(token_url, data=auth_payload, timeout=30)
    auth_response.raise_for_status()
    access_token = auth_response.json()["access_token"]

    # 3. Stream both images onto the same grid (skipping the full downloads)
    grid = region_grid(bbox)
    print("Streaming and orthorectifying the reference image...")
    pre_power = stream_power(pre, grid, access_token)
    print("Streaming and orthorectifying the flood image...")
    post_power = stream_power(post, grid, access_token)

    # 4. Change detection (0 = Dry, 1 = Water, 255 = No Data)
    utm_mask, threshold_db = change_mask(pre_power, post_power)
    seen = utm_mask != 255
    print(
        f"Dark threshold {threshold_db:.1f} dB, drop >= {DROP_DB} dB: "
        f"{100 * (utm_mask == 1).sum() / max(seen.sum(), 1):.1f}% of observed pixels are water"
    )

    # Format the output filename and ensure the directory exists
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    handoff_filename = MASK_DIR / f"{region.slug}_s1.tif"

    crs, transform, width, height = grid
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "uint8",
        "crs": crs,
        "transform": transform,
        "nodata": 255,
        "compress": "deflate",
    }

    print(f"Writing engine-compatible mask to {handoff_filename}...")
    with rasterio.open(handoff_filename, "w", **profile) as dst:
        dst.write(utm_mask, 1)
        dst.update_tags(
            observed_utc=post_time,
            sensor=satellite_name(post),
            synthetic="false",
            orbit=orbit,
            relative_orbit=str(relative_orbit),
            reference_utc=pre_time,
            method="change detection",
            threshold_db=f"{threshold_db:.2f}",
            drop_db=str(DROP_DB),
        )

    # 5. Automatic Pipeline Handoff (in-process: a searched Region isn't in regions.py)
    print(f"\nMask complete. Overlaying roads for {region.slug}...")
    return build(region, handoff_filename, out_dir)


if __name__ == "__main__":
    try:
        fetch_sentinel_radar()
    except NoImagery as e:
        raise SystemExit(str(e)) from None
