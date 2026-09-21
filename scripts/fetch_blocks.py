#!/usr/bin/env python3
"""Download 2020 Census block boundaries for the Bay Area counties as GeoJSON.

Source: Census Bureau TIGERweb REST API (public, no key needed).
Layer 12 of the Tracts_Blocks service is "Census Blocks" (2020 vintage).

Output: data/blocks/06<county>.geojson, one file per county (one Polygon or
MultiPolygon per block, with GEOID, housing units, population, land and
water area).  Existing files are kept unless --force is given.
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
BAY_AREA = {           # county FIPS -> name
    "001": "Alameda", "013": "Contra Costa", "041": "Marin", "055": "Napa", "075": "San Francisco",
    "081": "San Mateo", "085": "Santa Clara", "095": "Solano", "097": "Sonoma",
}
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


def fetch_county(session: requests.Session, state: str, county: str, out: Path) -> int:
    where = f"STATE='{state}' AND COUNTY='{county}'"
    features: list[dict] = []
    offset = 0
    while True:
        page = fetch_page(session, offset, where)
        feats = page.get("features", [])
        features.extend(feats)
        print(f"  {state}{county}: fetched {len(feats)} blocks (total {len(features)})")
        if not page.get("exceededTransferLimit") and len(feats) < PAGE:
            break
        if not feats:
            break
        offset += len(feats)
    for f in features:   # friendlier property names for the map
        p = f["properties"]
        f["properties"] = {
            "geoid": p["GEOID"],
            "housing_units": p.get("HU100") or 0,
            "population": p.get("POP100") or 0,
            "area_land_m2": p.get("AREALAND") or 0,
            "area_water_m2": p.get("AREAWATER") or 0,
        }
        f.pop("id", None)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")))
    print(f"wrote {len(features)} blocks -> {out} ({out.stat().st_size/1e6:.1f} MB)")
    return len(features)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="data/blocks")
    ap.add_argument("--state", default=STATE_FIPS)
    ap.add_argument("--county", default=",".join(sorted(BAY_AREA)),
                    help="comma-separated 3-digit county FIPS codes (default: the nine Bay Area counties)")
    ap.add_argument("--force", action="store_true", help="re-download counties that already exist")
    args = ap.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = "fcc-fiber-map/1.0 (census block fetch)"
    total = 0
    for county in [c.strip()[-3:].zfill(3) for c in args.county.split(",") if c.strip()]:
        out = Path(args.out_dir) / f"{args.state}{county}.geojson"
        if out.exists() and not args.force:
            print(f"keeping existing {out}")
            continue
        total += fetch_county(session, args.state, county, out)
    print(f"done: {total} blocks fetched")


if __name__ == "__main__":
    main()
