"""GPS coordinates -> "City, Region", fully offline.

reverse_geocoder ships a bundled GeoNames dataset and answers with a k-d tree
nearest-neighbour lookup — no internet call, ever. Accuracy is city-level,
which is exactly the granularity the Places screen wants.

mode=1 forces single-process search: the default multiprocess mode can
deadlock when called from a background thread on Windows.
"""

from __future__ import annotations

from functools import lru_cache

import reverse_geocoder as rg


@lru_cache(maxsize=4096)
def place_name(lat: float, lng: float) -> str | None:
    # Round to ~1km so nearby photos share one cache entry
    result = rg.search([(round(lat, 2), round(lng, 2))], mode=1)
    if not result:
        return None
    hit = result[0]
    city = hit.get("name") or ""
    region = hit.get("admin1") or ""
    if city and region and city != region:
        return f"{city}, {region}"
    return city or region or None


@lru_cache(maxsize=4096)
def place_detail(lat: float, lng: float) -> str | None:
    """A fuller label for the photo info panel: town, district, region, country
    where available. The Places/timeline surfaces keep the short place_name;
    this is only for "where exactly was this" in the info sidebar."""
    result = rg.search([(round(lat, 3), round(lng, 3))], mode=1)
    if not result:
        return None
    hit = result[0]
    parts = [
        hit.get("name"),      # town/city
        hit.get("admin2"),    # district / county
        hit.get("admin1"),    # state / region
        hit.get("cc"),        # country code
    ]
    seen: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.append(p)
    return ", ".join(seen) or None


def warm_up() -> None:
    """First lookup loads the dataset (~2s) — do it before indexing starts."""
    place_name(9.93, 76.27)
