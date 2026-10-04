import os
import requests
import rasterio
import numpy as np
import pandas as pd
from rasterio import Affine
from dotenv import load_dotenv

load_dotenv()
username = os.getenv("CDSE_USERNAME")
password = os.getenv("CDSE_PASSWORD")

# 1. Authenticate
token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
auth_payload = {"client_id": "cdse-public", "grant_type": "password", "username": username, "password": password}
access_token = requests.post(token_url, data=auth_payload).json()["access_token"]

# 2. Stream the Data
download_url = "https://zipper.dataspace.copernicus.eu/odata/v1/Products(3472e43d-fcff-46d2-b54c-abd803df0989)/Nodes(S1B_IW_GRDH_1SDV_20211128T142031_20211128T142056_029789_038E3D_6EED_COG.SAFE)/Nodes(measurement)/Nodes(s1b-iw-grd-vv-20211128t142031-20211128t142056-029789-038e3d-001-cog.tiff)/$value"
env = rasterio.Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {access_token}", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR")

with env:
    with rasterio.open(download_url) as src:
        # Stream a downscaled 2D matrix to save memory
        scale_factor = 10
        out_shape = (int(src.height / scale_factor), int(src.width / scale_factor))
        pixel_data = src.read(1, out_shape=out_shape)
        
        # Scale the coordinate transform to match our shrunken matrix
        scaled_transform = src.transform * Affine.scale(scale_factor, scale_factor)

# 3. Create the Water Mask
water_threshold = 3500 
# Find all pixels that have data (>0) AND fall below our dark water threshold
water_mask = (pixel_data > 0) & (pixel_data < water_threshold)

# 4. Convert Pixels to Real-World Coordinates
# np.where returns the exact row and column indices where water_mask is True
flooded_rows, flooded_cols = np.where(water_mask)

# Vectorized matrix multiplication: Multiplies the spatial transform by our column/row arrays
# This instantly calculates the Longitude (X) and Latitude (Y) for thousands of pixels
longitudes, latitudes = scaled_transform * (flooded_cols, flooded_rows)

# Extract the raw radar backscatter values for these specific flooded pixels
flood_values = pixel_data[flooded_rows, flooded_cols]

# 5. Build the Final Matrix
flood_matrix = pd.DataFrame({
    'Longitude': longitudes,
    'Latitude': latitudes,
    'Radar_Value': flood_values
})

print(f"Extracted {len(flood_matrix)} flooded coordinate points.")
print("\nSample of the flood matrix:")
print(flood_matrix.head())