"""Make a mask overlay to indicate where flood visuals will be displayed"""

import argparse
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import shape
from shapely.ops import transform as reproject

from pipeline.regions import get_region

PIXEL_M = 10  # matches Sentinel-1 GRD resolution
DRY, WATER, NO_DATA = 0, 1, 255
NO_DATA_STRIP = 0.1  # eastern fraction of the mask marked as "not observed"

ROOT = Path(__file__).resolve().parent.parent
WATER_DIR = Path(__file__).resolve().parent / "test_floods"
MASK_DIR = ROOT / "data" / "masks"


def utm_crs(lon: float, lat: float) -> CRS:
    """UTM zone containing the point, so distances are in metres."""
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


def build_mask(slug: str, speckle: float = 0.0) -> tuple[np.ndarray, dict]:
    region = get_region(slug)
    west, south, east, north = region.bbox
    crs = utm_crs((west + east) / 2, (south + north) / 2)
    to_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True)

    # Region bbox in metres, snapped outward to whole pixels.
    minx, miny, maxx, maxy = to_utm.transform_bounds(west, south, east, north)
    minx = math.floor(minx / PIXEL_M) * PIXEL_M
    maxy = math.ceil(maxy / PIXEL_M) * PIXEL_M
    width = math.ceil((maxx - minx) / PIXEL_M)
    height = math.ceil((maxy - miny) / PIXEL_M)
    grid = from_origin(minx, maxy, PIXEL_M, PIXEL_M)

    water = json.loads((WATER_DIR / f"{slug}.geojson").read_text())
    shapes = [(reproject(to_utm.transform, shape(f["geometry"])), WATER) for f in water["features"]]

    mask = rasterize(shapes, out_shape=(height, width), transform=grid, fill=DRY, dtype="uint8")
    mask[:, int(width * (1 - NO_DATA_STRIP)) :] = NO_DATA

    if speckle:  # imitate radar noise: flip random observed pixels (seeded, so runs repeat)
        noisy = (np.random.default_rng(0).random(mask.shape) < speckle) & (mask != NO_DATA)
        mask[noisy] = 1 - mask[noisy]

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "uint8",
        "crs": crs,
        "transform": grid,
        "nodata": NO_DATA,
        "compress": "deflate",
    }
    return mask, profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("region", help="region slug from pipeline/regions.py")
    parser.add_argument("--observed-utc", default="2021-11-16T14:25:00Z")
    parser.add_argument(
        "--speckle", type=float, default=0.0, help="share of pixels flipped at random, e.g. 0.3"
    )
    args = parser.parse_args()

    mask, profile = build_mask(args.region, args.speckle)
    MASK_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "_speckle" if args.speckle else ""
    out = MASK_DIR / f"{args.region}_synthetic{suffix}.tif"
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(mask, 1)
        dst.update_tags(observed_utc=args.observed_utc, sensor="synthetic", synthetic="true")

    water_pct = 100 * (mask == WATER).sum() / mask.size
    print(f"wrote {out} ({profile['width']}x{profile['height']} px, {water_pct:.1f}% water)")


if __name__ == "__main__":
    main()
