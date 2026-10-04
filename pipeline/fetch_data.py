"""Download a raw Sentinel-1 VV radar image from the Copernicus Data Space (CDSE).

Needs a free CDSE account in .env (CDSE_USERNAME, CDSE_PASSWORD). The output is the raw
radar backscatter, not a flood mask; turning it into one is the data team's step.
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables once when the module is imported
load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
RADAR_DIR = ROOT / "data" / "radar"  # gitignored; scenes are large


def fetch_sentinel_radar(
    bbox: tuple[float, float, float, float] = (-122.4, 49.0, -122.1, 49.2),
    date_range: str = "2021-11-14T00:00:00Z/2021-11-30T23:59:59Z",
    output_filename: str = "abbotsford_vv_radar.tiff",
) -> str | None:
    """
    Fetches Sentinel-1 VV radar data for a given bounding box and time range.
    Returns the file path of the downloaded .tiff file, or None if no data is found.
    """
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

    # 2. Search STAC API using the provided parameters
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

    # 3. Extract the HTTPS Download URL
    feature = features[0]
    date_captured = feature["properties"]["datetime"]
    print(f"Found imagery captured on: {date_captured}")

    download_url = feature["assets"]["vv"]["alternate"]["https"]["href"]

    # 4. Download the .tiff File
    headers = {"Authorization": f"Bearer {access_token}"}

    RADAR_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RADAR_DIR / output_filename
    print(f"Starting download: {output_path}")

    with requests.get(download_url, headers=headers, stream=True, timeout=30) as r:
        r.raise_for_status()
        total_size = int(r.headers.get("content-length", 0))

        with (
            open(output_path, "wb") as f,
            tqdm(
                desc="Downloading",
                total=total_size,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
            ) as bar,
        ):
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))

    print(f"\nSuccess! File saved as {output_path}")

    # Return the file path so the API endpoint can pass it to the next processing step
    return str(output_path)


if __name__ == "__main__":
    fetch_sentinel_radar()
