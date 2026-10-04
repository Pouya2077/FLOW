import os
import subprocess
import sys

import numpy as np
import rasterio
import requests
from dotenv import load_dotenv
from rasterio.enums import Resampling
from rasterio.env import Env
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

# Explicit rasterio imports for streaming and orthorectification
from rasterio.windows import from_bounds

# Import the teammate's CRS function
from pipeline.map_mask import MASK_DIR, utm_crs
from pipeline.regions import get_region

# Load environment variables once when the module is imported
load_dotenv()


# Convert amplitude to db; they are on different scales that will not match the flood threshold
def amplitude_to_db(amplitude: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20 * np.log10(amplitude.astype("float32"))  # dB relative to an unknown constant


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


# 0 = dry, 1 = water, 255 = no data (0 is the GRD fill value). Returns the dB threshold too.
def water_mask(raw_radar: np.ndarray) -> tuple[np.ndarray, float]:
    seen = raw_radar > 0
    utm_mask = np.full(raw_radar.shape, 255, dtype=np.uint8)
    if not seen.any():
        return utm_mask, float("nan")
    db = amplitude_to_db(raw_radar[seen])
    threshold = otsu_threshold(db)
    utm_mask[seen] = np.where(db < threshold, 1, 0)
    return utm_mask, threshold


"""
    Fetches a subset of Sentinel-1 VV radar data for a region from pipeline/regions.py.
    Converts the raw data into a map-aligned, UTM-projected uint8 flood mask.
    Automatically triggers build_location.py upon completion.
"""


def fetch_sentinel_radar(
    region_slug: str = "sumas-prairie",
    date_range: str = "2021-11-14T00:00:00Z/2021-11-30T23:59:59Z",
) -> str | None:
    bbox = get_region(region_slug).bbox

    username = os.getenv("CDSE_USERNAME")
    password = os.getenv("CDSE_PASSWORD")

    if not username or not password:
        raise ValueError("Missing CDSE_USERNAME or CDSE_PASSWORD in .env file.")

    # 1. Authenticate and Get Bearer Token
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

    auth_response = requests.post(token_url, data=auth_payload)
    auth_response.raise_for_status()
    access_token = auth_response.json()["access_token"]

    # Search STAC API and Extract URL
    print(f"\nSearching for Sentinel-1 data for bbox {bbox}...")
    search_url = "https://stac.dataspace.copernicus.eu/v1/search"
    search_payload = {
        "collections": ["sentinel-1-grd"],
        "bbox": bbox,
        "datetime": date_range,
        "limit": 1,
    }

    search_response = requests.post(search_url, json=search_payload)
    search_response.raise_for_status()
    features = search_response.json().get("features", [])

    if not features:
        print("No imagery found for this time and location.")
        return None

    feature = features[0]
    date_captured = feature["properties"]["datetime"]
    print(f"Found imagery captured on: {date_captured}")

    # This URL points to the remote 600MB+ file
    download_url = feature["assets"]["vv"]["alternate"]["https"]["href"]

    # 4 & 5. Stream, Orthorectify, and Threshold in One Step
    print(f"Streaming and orthorectifying radar subset for bbox {bbox} (Skipping full download)...")

    # Get the correct target map projection from the teammate's function
    target_crs = utm_crs((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

    # Pass the auth token to GDAL so it can read the remote file securely
    with Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {access_token}"):
        with rasterio.open(download_url) as src:
            # Wrap the raw GCP-based SAR image in a Virtual Raster (VRT).
            # This automatically calculates how to stretch the sideways radar image
            # into our flat, top-down UTM map projection!
            # nodata=0: keep the scene's 0-valued fill out of the interpolation, so the scene
            # edge doesn't blend into dark "water" pixels.
            with WarpedVRT(
                src, crs=target_crs, resampling=Resampling.bilinear, src_nodata=0, nodata=0
            ) as vrt:
                # Transform our lat/lon bbox into the VRT's new UTM coordinates
                bounds = transform_bounds("EPSG:4326", vrt.crs, *bbox)

                # Calculate the pixel window that matches our UTM bounding box
                window = from_bounds(*bounds, transform=vrt.transform)

                # Stream ONLY those pixels over the network.
                # GDAL automatically fetches and warps just the chunks we need!
                raw_radar = vrt.read(1, window=window)

                # Get the metadata for our newly cropped, UTM-aligned array
                window_transform = vrt.window_transform(window)
                h, w = raw_radar.shape

    print("Subset streamed and orthorectified. Applying flood thresholds...")

    # Apply the flood threshold (0 = Dry, 1 = Water, 255 = No Data)
    utm_mask, threshold_db = water_mask(raw_radar)
    seen = utm_mask != 255
    print(
        f"Water threshold {threshold_db:.1f} dB: "
        f"{100 * (utm_mask == 1).sum() / max(seen.sum(), 1):.1f}% of observed pixels are water"
    )

    # Format the output filename and ensure the directory exists
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    handoff_filename = str(MASK_DIR / f"{region_slug}_s1.tif")

    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": "uint8",
        "crs": target_crs,
        "transform": window_transform,
        "nodata": 255,
        "compress": "deflate",
    }

    print(f"Writing engine-compatible mask to {handoff_filename}...")
    with rasterio.open(handoff_filename, "w", **profile) as dst:
        dst.write(utm_mask, 1)
        dst.update_tags(
            observed_utc=date_captured,
            sensor="Sentinel-1A",
            synthetic="false",
            orbit="descending",
            reference_date="2021-11-04",
            threshold_db=f"{threshold_db:.2f}",
        )

    # 6. Automatic Pipeline Handoff
    print(f"\nPipeline complete! Triggering build_location.py for {region_slug}...")

    cmd = [sys.executable, "-m", "pipeline.build_location", region_slug, "--mask", handoff_filename]

    try:
        subprocess.run(cmd, check=True)
        print("\nSuccess: Location data successfully built!")
    except subprocess.CalledProcessError as e:
        print(f"\nError: build_location.py failed with exit code {e.returncode}")

    return handoff_filename


if __name__ == "__main__":
    fetch_sentinel_radar()
