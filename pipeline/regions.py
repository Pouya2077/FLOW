"""Lists regions that the pipeline is able to process."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    slug: str
    name: str  # name appearing in the UI
    bbox: tuple[float, float, float, float]  # W, S, E, N in lon/lat


REGIONS = {
    r.slug: r
    for r in [
        Region(
            slug="sumas-prairie",
            name="Sumas Prairie, Abbotsford",
            bbox=(-122.28, 49.00, -122.08, 49.10),
        )
    ]
}


def get_region(slug: str) -> Region:
    try:
        return REGIONS[slug]
    except KeyError:
        raise SystemExit(f"unknown region {slug!r}; choose from {','.join(REGIONS)}") from None
