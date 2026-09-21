#!/usr/bin/env python3
"""Download 2020 Census block boundaries for San Francisco County as GeoJSON.

Source: Census Bureau TIGERweb REST API (public, no key needed).
Layer 12 of the Tracts_Blocks service is "Census Blocks" (2020 vintage).

Output: data/blocks_sf.geojson  (one Polygon/MultiPolygon per block, with
GEOID, housing units, population, land and water area).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

TIGERWEB = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/Tracts_Blocks/MapServer/12/query"
)
STATE_FIPS = "06"      # California
COUNTY_FIPS = "075"    # San Francisco
PAGE = 1000            # records per request (server max is 100000; keep responses small)
OUT_FIELDS = "GEOID,HU100,POP100,AREALAND,AREAWATER"


def fetch_page(session: requests.Session, offset: int, where: str) -> dict:
    params = {
        "where": where,
        "outFields": OUT_FIELDS,
        "outSR": "4326",
        "geometryPrecision": "6",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE),
        "orderByFields": "GEOID",
        "f": "geojson",
    }
    for attempt in range(5):
        try:
            r = session.get(TIGERWEB, params=params, timeout=120)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            wait = 2 ** attempt
            print(f"  page offset={offset} failed ({exc}); retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"giving up on offset {offset}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/blocks_sf.geojson")
    ap.add_argument("--state", default=STATE_FIPS)
    ap.add_argument("--county", default=COUNTY_FIPS)
    args = ap.parse_args()

    where = f"STATE='{args.state}' AND COUNTY='{args.county}'"
    session = requests.Session()
    session.headers["User-Agent"] = "fcc-fiber-map/1.0 (census block fetch)"

    features: list[dict] = []
    offset = 0
    while True:
        page = fetch_page(session, offset, where)
        feats = page.get("features", [])
        features.extend(feats)
        print(f"  fetched {len(feats)} blocks (total {len(features)})")
        if not page.get("exceededTransferLimit") and len(feats) < PAGE:
            break
        if not feats:
            break
        offset += len(feats)

    # Normalise property names to something friendlier for the map.
    for f in features:
        p = f["properties"]
        f["properties"] = {
            "geoid": p["GEOID"],
            "housing_units": p.get("HU100") or 0,
            "population": p.get("POP100") or 0,
            "area_land_m2": p.get("AREALAND") or 0,
            "area_water_m2": p.get("AREAWATER") or 0,
        }
        f.pop("id", None)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "features": features}
    out.write_text(json.dumps(fc, separators=(",", ":")))
    print(f"wrote {len(features)} blocks -> {out} ({out.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
