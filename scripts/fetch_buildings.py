#!/usr/bin/env python3
"""Download building footprints for the Bay Area and assign each to its census block.

Sources (both public, no key):
  * DataSF "Building Footprints" (dataset ynuv-fyni) for San Francisco: LiDAR-derived,
    with heights.
  * Overture Maps buildings (theme=buildings) for the other counties, fetched with the
    `overturemaps` client for the region's bounding box.  Overture merges OpenStreetMap,
    Microsoft and Google footprints and carries a building type where known.

Each building's representative point is matched to a 2020 census block from
data/blocks/06<county>.geojson, so the map can paint every building with its
block's fiber status when zoomed in.

Output: site/data/buildings/14_<x>_<y>.json  one file per zoom-14 web-mercator
        tile, loaded on demand by the map, plus buildings/index.json.

Chunk format (compact, delta-encoded):
  {"g": [geoid, ...],                             # block ids referenced by index
   "b": [[gi, height_m, x0, y0, [dx, dy, ...], kind], ...]}
  x0/y0 are the first vertex in 1e-6 degrees (lng, lat); the remaining vertices
  are deltas from the previous vertex in the same units.  Only the exterior ring
  of the largest polygon is kept.  kind: 0 unknown, 1 residential, 2 commercial,
  3 industrial, 4 other (Overture subtype, where known).

Needs shapely; the Overture step needs the `overturemaps` package.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
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
BAY_AREA_BBOX = "-123.55,36.90,-121.20,38.90"
SF = "06075"
KIND = {"residential": 1, "commercial": 2, "industrial": 3}


def tile_xy(lng: float, lat: float, z: int = Z) -> tuple[int, int]:
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


# ---- sources -----------------------------------------------------------------

def fetch_datasf(session: requests.Session, cache: Path):
    """Yield (geometry, height_m, kind) for San Francisco from DataSF."""
    if not cache.exists():
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
                    print(f"  DataSF page {offset} failed ({exc}); retry in {wait}s", file=sys.stderr)
                    time.sleep(wait)
            else:
                raise SystemExit("giving up on DataSF")
            feats.extend(page)
            print(f"  DataSF: fetched {len(page):,} buildings (total {len(feats):,})", file=sys.stderr)
            if len(page) < PAGE:
                break
            offset += PAGE
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    else:
        print(f"  using cached {cache}", file=sys.stderr)
    for f in json.loads(cache.read_text())["features"]:
        try:
            h = float(f["properties"].get("hgt_median_m") or 0)
        except (TypeError, ValueError):
            h = 0.0
        yield f["geometry"], h, 0


def fetch_overture(cache: Path, bbox: str):
    """Yield (geometry, height_m, kind) from an Overture buildings download (GeoJSON lines)."""
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading Overture buildings for bbox {bbox} (this takes a while) ...", file=sys.stderr)
        cmd = ["overturemaps", "download", f"--bbox={bbox}", "-f", "geojsonseq", "--type=building", "-o", str(cache)]
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            raise SystemExit("the `overturemaps` command is missing: pip install overturemaps")
    else:
        print(f"  using cached {cache}", file=sys.stderr)
    with cache.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            f = json.loads(line)
            p = f.get("properties") or {}
            h = p.get("height")
            yield f["geometry"], float(h) if isinstance(h, (int, float)) else 0.0, KIND.get(p.get("subtype"), 4 if p.get("subtype") else 0)


# ---- encoding ------------------------------------------------------------------

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
    ap.add_argument("--blocks-dir", default="data/blocks", help="directory of 06<county>.geojson block files")
    ap.add_argument("--sources", default="datasf,overture", help="comma-separated: datasf (SF), overture (everything else)")
    ap.add_argument("--bbox", default=BAY_AREA_BBOX, help="Overture download bbox: west,south,east,north")
    ap.add_argument("--datasf-cache", default="data/raw/sf_building_footprints.geojson")
    ap.add_argument("--overture-cache", default="data/raw/overture_buildings_bayarea.geojsonseq")
    ap.add_argument("--out", default="site/data/buildings")
    args = ap.parse_args()
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    block_files = sorted(Path(args.blocks_dir).glob("06*.geojson"))
    if not block_files:
        raise SystemExit(f"no block files in {args.blocks_dir}/ (run scripts/fetch_blocks.py)")
    geoms, geoids = [], []
    for bf in block_files:
        for f in json.loads(bf.read_text())["features"]:
            geoms.append(shape(f["geometry"]))
            geoids.append(f["properties"]["geoid"])
    tree = STRtree(geoms)
    print(f"{len(geoids):,} blocks from {len(block_files)} counties", file=sys.stderr)

    session = requests.Session()
    session.headers["User-Agent"] = "fcc-fiber-map/1.0 (building footprints)"
    streams = []
    if "datasf" in sources:
        streams.append(("DataSF", fetch_datasf(session, Path(args.datasf_cache)), None))
    if "overture" in sources:
        # DataSF covers San Francisco; skip Overture's buildings there to avoid doubles
        streams.append(("Overture", fetch_overture(Path(args.overture_cache), args.bbox), SF if "datasf" in sources else None))

    chunks: dict[tuple[int, int], dict] = defaultdict(lambda: {"g": [], "gi": {}, "b": []})
    stats = defaultdict(lambda: {"kept": 0, "small": 0, "unassigned": 0, "skipped": 0})
    for name, stream, skip_prefix in streams:
        st = stats[name]
        for i, (geometry, height, kind) in enumerate(stream):
            if i and i % 100000 == 0:
                print(f"  {name}: ... {i:,}", file=sys.stderr)
            try:
                g = shape(geometry)
            except Exception:  # noqa: BLE001
                continue
            if g.geom_type == "MultiPolygon":
                g = max(g.geoms, key=lambda p: p.area)
            if g.geom_type != "Polygon" or g.is_empty:
                continue
            lat = g.centroid.y
            area_m2 = g.area * (111_320 ** 2) * math.cos(math.radians(lat))
            if area_m2 < MIN_AREA_M2:
                st["small"] += 1
                continue
            pt = g.representative_point()
            geoid = None
            for j in tree.query(pt, predicate="within"):
                geoid = geoids[j]
                break
            if geoid is None:
                st["unassigned"] += 1
                continue
            if skip_prefix and geoid.startswith(skip_prefix):
                st["skipped"] += 1
                continue
            gs = g.simplify(SIMPLIFY_DEG, preserve_topology=True)
            ring = list(gs.exterior.coords) if not gs.is_empty and gs.geom_type == "Polygon" else list(g.exterior.coords)
            if len(ring) < 4:
                continue
            x0, y0, deltas = encode_ring(ring)
            c = chunks[tile_xy(pt.x, pt.y)]
            gi = c["gi"].setdefault(geoid, len(c["g"]))
            if gi == len(c["g"]):
                c["g"].append(geoid)
            rec = [gi, int(round(height)), x0, y0, deltas]
            if kind:
                rec.append(kind)
            c["b"].append(rec)
            st["kept"] += 1
        print(f"  {name}: kept {st['kept']:,}; dropped {st['small']:,} under {MIN_AREA_M2} m2, "
              f"{st['unassigned']:,} outside any block, {st['skipped']:,} in a county covered by another source", file=sys.stderr)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("14_*.json"):
        old.unlink()
    total_bytes = 0
    for (x, y), c in chunks.items():
        p = out / f"{Z}_{x}_{y}.json"
        p.write_text(json.dumps({"g": c["g"], "b": c["b"]}, separators=(",", ":")))
        total_bytes += p.stat().st_size
    kept = sum(st["kept"] for st in stats.values())
    index = {"z": Z, "tiles": sorted(f"{x}_{y}" for x, y in chunks), "buildings": kept,
             "sources": {name: st["kept"] for name, st in stats.items()},
             "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    print(f"kept {kept:,} buildings in {len(chunks)} tiles ({total_bytes/1e6:.1f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
