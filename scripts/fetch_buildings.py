#!/usr/bin/env python3
"""Download San Francisco building footprints and assign each to its census block.

Source: DataSF "Building Footprints" (dataset ynuv-fyni, public, no key).
Each building's representative point is matched to a 2020 census block from
data/blocks_sf.geojson, so the map can paint every building with its block's
fiber status when zoomed in.

Output: site/data/buildings/14_<x>_<y>.json  one file per zoom-14 web-mercator
        tile, loaded on demand by the map, plus buildings/index.json.

Chunk format (compact, delta-encoded):
  {"g": [geoid, ...],                       # block ids referenced by index
   "b": [[gi, height_m, x0, y0, [dx, dy, ...]], ...]}
  x0/y0 are the first vertex in 1e-6 degrees (lng, lat); the remaining
  vertices are deltas from the previous vertex in the same units.  Only the
  exterior ring of the largest polygon is kept.

Needs shapely (pip install shapely).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from shapely.geometry import shape
from shapely.strtree import STRtree

SODA = "https://data.sf.gov/resource/ynuv-fyni.geojson"
PAGE = 50000
Z = 14
SCALE = 1_000_000            # coordinate units: 1e-6 degree (~0.1 m)
MIN_AREA_M2 = 15             # drop sheds and slivers
SIMPLIFY_DEG = 0.000004      # ~0.4 m


def tile_xy(lng: float, lat: float, z: int = Z) -> tuple[int, int]:
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def fetch_all(session: requests.Session, cache: Path) -> list[dict]:
    if cache.exists():
        print(f"using cached {cache}", file=sys.stderr)
        return json.loads(cache.read_text())["features"]
    feats: list[dict] = []
    offset = 0
    while True:
        params = {"$select": "sf16_bldgid,hgt_median_m,shape", "$order": "sf16_bldgid",
                  "$limit": str(PAGE), "$offset": str(offset)}
        for attempt in range(5):
            try:
                r = session.get(SODA, params=params, timeout=600)
                r.raise_for_status()
                page = r.json()["features"]
                break
            except Exception as exc:  # noqa: BLE001
                wait = 5 * 2 ** attempt
                print(f"  page {offset} failed ({exc}); retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
        else:
            raise SystemExit("giving up")
        feats.extend(page)
        print(f"  fetched {len(page):,} buildings (total {len(feats):,})", file=sys.stderr)
        if len(page) < PAGE:
            break
        offset += PAGE
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return feats


def encode_ring(coords) -> tuple[int, int, list[int]]:
    pts = [(round(x * SCALE), round(y * SCALE)) for x, y in coords]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    x0, y0 = pts[0]
    deltas: list[int] = []
    px, py = x0, y0
    for x, y in pts[1:]:
        deltas += [x - px, y - py]
        px, py = x, y
    return x0, y0, deltas


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blocks", default="data/blocks_sf.geojson")
    ap.add_argument("--cache", default="data/raw/sf_building_footprints.geojson", help="raw download cache")
    ap.add_argument("--out", default="site/data/buildings")
    args = ap.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = "fcc-fiber-map/1.0 (building footprints)"
    feats = fetch_all(session, Path(args.cache))

    blocks = json.loads(Path(args.blocks).read_text())["features"]
    geoms = [shape(f["geometry"]) for f in blocks]
    geoids = [f["properties"]["geoid"] for f in blocks]
    tree = STRtree(geoms)
    print(f"{len(feats):,} buildings, {len(blocks):,} blocks; assigning ...", file=sys.stderr)

    chunks: dict[tuple[int, int], dict] = defaultdict(lambda: {"g": [], "gi": {}, "b": []})
    kept = dropped_small = unassigned = 0
    for i, f in enumerate(feats):
        if i and i % 25000 == 0:
            print(f"  ... {i:,}", file=sys.stderr)
        try:
            g = shape(f["geometry"])
        except Exception:  # noqa: BLE001
            continue
        if g.geom_type == "MultiPolygon":
            g = max(g.geoms, key=lambda p: p.area)
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        lat = g.centroid.y
        area_m2 = g.area * (111_320 ** 2) * math.cos(math.radians(lat))
        if area_m2 < MIN_AREA_M2:
            dropped_small += 1
            continue
        pt = g.representative_point()
        geoid = None
        for j in tree.query(pt, predicate="within"):
            geoid = geoids[j]
            break
        if geoid is None:
            unassigned += 1
            continue
        gs = g.simplify(SIMPLIFY_DEG, preserve_topology=True)
        ring = list(gs.exterior.coords) if not gs.is_empty and gs.geom_type == "Polygon" else list(g.exterior.coords)
        if len(ring) < 4:
            continue
        x0, y0, deltas = encode_ring(ring)
        try:
            h = int(round(float(f["properties"].get("hgt_median_m") or 0)))
        except (TypeError, ValueError):
            h = 0
        c = chunks[tile_xy(pt.x, pt.y)]
        gi = c["gi"].setdefault(geoid, len(c["g"]))
        if gi == len(c["g"]):
            c["g"].append(geoid)
        c["b"].append([gi, h, x0, y0, deltas])
        kept += 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("14_*.json"):
        old.unlink()
    total_bytes = 0
    for (x, y), c in chunks.items():
        p = out / f"{Z}_{x}_{y}.json"
        p.write_text(json.dumps({"g": c["g"], "b": c["b"]}, separators=(",", ":")))
        total_bytes += p.stat().st_size
    index = {"z": Z, "tiles": sorted(f"{x}_{y}" for x, y in chunks), "buildings": kept,
             "source": "DataSF Building Footprints (ynuv-fyni)",
             "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    print(f"kept {kept:,} buildings in {len(chunks)} tiles ({total_bytes/1e6:.1f} MB); "
          f"dropped {dropped_small:,} under {MIN_AREA_M2} m2, {unassigned:,} outside any block", file=sys.stderr)


if __name__ == "__main__":
    main()
