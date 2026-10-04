"""Download a raw Sentinel-1 VV radar image from the Copernicus Data Space (CDSE).

Needs a free CDSE account in .env (CDSE_USERNAME, CDSE_PASSWORD). The output is the raw
radar backscatter, not a flood mask; turning it into one is the data team's step.
"""

import math
import os
from pathlib import Path

import rasterio
import requests
from dotenv import load_dotenv
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds

# Load environment variables once when the module is imported
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
RADAR_DIR = ROOT / "data" / "radar"  # gitignored; scenes are large


def fetch_sentinel_radar(
    bbox: tuple[float, float, float, float] = (-122.4, 49.199, -122.300, 49.2),
    date_range: str = "2021-11-14T00:00:00Z/2021-11-30T23:59:59Z",
    output_filename: str = "abbotsford_vv_radar.tiff",
) -> str | None:
    """
    Fetches Sentinel-1 VV radar data. Automatically calculates a 10km x 10km area 
    centered on the provided bounding box and streams ONLY that subset from the cloud.
    Returns the file path of the downloaded .tiff file, or None if no data is found.
    """
    username = os.getenv("CDSE_USERNAME")
    password = os.getenv("CDSE_PASSWORD")

    if not username or not password:
        raise ValueError("Missing CDSE_USERNAME or CDSE_PASSWORD in .env file.")

    # Calculate the center of the provided bbox to enforce a strict 10km x 10km size
    min_lon, min_lat, max_lon, max_lat = bbox
    center_lon = (min_lon + max_lon) / 2.0
    center_lat = (min_lat + max_lat) / 2.0

    size_km = 10.0
    km_per_lat_deg = 111.32
    half_size = size_km / 2.0

    # Calculate degree offsets for 10km x 10km based on Earth's curvature
    lat_offset = half_size / km_per_lat_deg
    lon_offset = half_size / (km_per_lat_deg * math.cos(math.radians(center_lat)))

    stream_bbox = (
        center_lon - lon_offset,
        center_lat - lat_offset,
        center_lon + lon_offset,
        center_lat + lat_offset
    )

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

    # 2. Search STAC API using the 10km x 10km bounding box
    print(f"\nSearching for Sentinel-1 data for 10x10km area centered at ({center_lat:.3f}, {center_lon:.3f})...")
    search_url = "https://stac.dataspace.copernicus.eu/v1/search"
    search_payload = {
        "collections": ["sentinel-1-grd"],
        "bbox": stream_bbox,
        "datetime": date_range,
        "limit": 1,
    }

    search_response = requests.post(search_url, json=search_payload)
    search_response.raise_for_status()
    features = search_response.json().get("features", [])

    if not features:
        print("No imagery found for this time and location.")
        return None

    # 3. Extract the HTTPS Download URL
    feature = features[0]
    date_captured = feature["properties"]["datetime"]
    print(f"Found imagery captured on: {date_captured}")

    download_url = feature["assets"]["vv"]["alternate"]["https"]["href"]

    # 4. Stream and clip the .tiff directly from the cloud
    print("Streaming clipped 10km x 10km area from the cloud (Skipping full download)...")

    RADAR_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RADAR_DIR / output_filename

    # Pass your bearer token into GDAL's environment so it can securely access the file
    gdal_env = {
        "GDAL_HTTP_HEADERS": f"Authorization: Bearer {access_token}",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"  # Speeds up remote reading
    }

    with rasterio.Env(**gdal_env):
        with rasterio.open(f"/vsicurl/{download_url}") as src:
            # Wrap the raw GCP-based SAR image in a Virtual Raster (VRT) aligned to standard Lat/Lon
            with WarpedVRT(src, crs="EPSG:4326", resampling=Resampling.bilinear) as vrt:
                
                # Match our geographic bounding box to the VRT's new Lat/Lon pixel grid
                window = from_bounds(*stream_bbox, transform=vrt.transform)
                
                # Download ONLY the data inside that window over the network
                data = vrt.read(1, window=window)
                window_transform = vrt.window_transform(window)
                
                # Copy metadata from the VRT, not the raw source!
                meta = vrt.meta.copy()

    # Update the file's metadata to match our newly shrunken 10km x 10km image
    meta.update({
        "height": window.height,
        "width": window.width,
        "transform": window_transform
    })

    # Save the clipped data locally
    with rasterio.open(output_path, "w", **meta) as dest:
        dest.write(data, 1)

    print(f"\nSuccess! 10km x 10km clipped file saved as {output_path}")

    return str(output_path)


if __name__ == "__main__":
    fetch_sentinel_radar()