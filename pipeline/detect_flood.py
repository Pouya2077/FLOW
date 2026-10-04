import os
import sys
import requests
import subprocess
import rasterio
import numpy as np
from dotenv import load_dotenv

# Explicit rasterio imports for streaming and orthorectification
from rasterio.windows import from_bounds
from rasterio.warp import transform_bounds
from rasterio.vrt import WarpedVRT
from rasterio.enums import Resampling
from rasterio.env import Env

# Import the teammate's CRS function
from pipeline.map_mask import utm_crs

# Load environment variables once when the module is imported
load_dotenv()

def fetch_sentinel_radar(
    # 10km x 10km bounding box around Sumas Prairie
    bbox: tuple[float, float, float, float] = (-122.319, 49.055, -122.181, 49.145),
    date_range: str = "2021-11-14T00:00:00Z/2021-11-30T23:59:59Z",
    output_filename: str = "abbotsford_vv_radar.tiff",
    region_slug: str = "abbostford_area",  # Required for the handoff to build_location.py
) -> str | None:
    """
    Fetches a subset of Sentinel-1 VV radar data for a given bounding box.
    Converts the raw data into a map-aligned, UTM-projected uint8 flood mask.
    Automatically triggers build_location.py upon completion.
    """
    username = os.getenv("CDSE_USERNAME")
    password = os.getenv("CDSE_PASSWORD")

    if not username or not password:
        raise ValueError("Missing CDSE_USERNAME or CDSE_PASSWORD in .env file.")

    # =========================================================
    # 1. Authenticate and Get Bearer Token
    # =========================================================
    print("Authenticating with Copernicus...")
    token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    auth_payload = {
        "client_id": "cdse-public",
        "grant_type": "password",
        "username": username,
        "password": password,
    }

    auth_response = requests.post(token_url, data=auth_payload)
    auth_response.raise_for_status()
    access_token = auth_response.json()["access_token"]

    # =========================================================
    # 2 & 3. Search STAC API and Extract URL
    # =========================================================
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

    # =========================================================
    # 4 & 5. Stream, Orthorectify, and Threshold in One Step
    # =========================================================
    print(f"Streaming and orthorectifying radar subset for bbox {bbox} (Skipping full download)...")

    # Get the correct target map projection from the teammate's function
    target_crs = utm_crs((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

    # Pass the auth token to GDAL so it can read the remote file securely
    with Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {access_token}"):
        with rasterio.open(download_url) as src:
            
            # Wrap the raw GCP-based SAR image in a Virtual Raster (VRT).
            # This automatically calculates how to stretch the sideways radar image
            # into our flat, top-down UTM map projection!
            with WarpedVRT(src, crs=target_crs, resampling=Resampling.bilinear) as vrt:
                
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
    WATER_THRESHOLD = 0.025 
    utm_mask = np.zeros((h, w), dtype=np.uint8) 
    utm_mask[raw_radar < WATER_THRESHOLD] = 1   
    utm_mask[raw_radar == 0] = 255              

    # Format the output filename and ensure the directory exists
    handoff_filename = output_filename.replace(".tiff", "_engine_ready.tif")
    os.makedirs(os.path.dirname(handoff_filename) or ".", exist_ok=True)
    
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
        )

    # =========================================================
    # 6. Automatic Pipeline Handoff
    # =========================================================
    print(f"\nPipeline complete! Triggering build_location.py for {region_slug}...")
    
    cmd = [
        sys.executable,
        "-m", "pipeline.build_location",
        region_slug,
        "--mask", handoff_filename
    ]
    
    try:
        subprocess.run(cmd, check=True)
        print("\nSuccess: Location data successfully built!")
    except subprocess.CalledProcessError as e:
        print(f"\nError: build_location.py failed with exit code {e.returncode}")
    
    return handoff_filename

if __name__ == "__main__":
    fetch_sentinel_radar()