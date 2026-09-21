#!/usr/bin/env python3
"""Join FCC BDC fiber availability records to census blocks and H3 hexagons
and emit the map's data files.

Inputs
  data/raw/*.csv | *.zip      FCC BDC "availability" CSVs (as downloaded from
                               broadbandmap.fcc.gov, optionally still zipped).
  data/raw/*summary_by_geography*  optional: the FCC "Fixed Broadband Summary by
                               Geography" file.  Its County rows give the share
                               of residential units that can get each speed by
                               technology, computed by the FCC against the full
                               (licensed) location Fabric, i.e. the honest
                               "% of homes" figure the availability file alone
                               cannot give.  Exposed as meta.fcc_summary.
  data/blocks/06<county>.geojson  Census blocks from scripts/fetch_blocks.py.

Outputs
  site/data/overview.js        window.FIBER_DATA = {meta, hexes, hexes7}
                               One feature per H3 res-8 hexagon (~0.74 km2)
                               that has any serviceable location, plus the same
                               aggregated to res-7 parents (~5.2 km2) for the
                               zoomed-out view.  Small enough to load whole, so
                               the page computes exact region-wide statistics.
  site/data/blocks/12_x_y.json Census blocks, one file per zoom-12 web-mercator
                               tile (a block appears in every tile its bounding
                               box touches), loaded by the page when zoomed in.
  site/data/blocks/index.json  The list of tiles.

Every fiber provider in the files is kept.  For each hexagon and block the
output stores, instead of plain totals, a small list of *location
combinations*:

    c: [[res, [[provider_idx, down_mbps, up_mbps], ...], count], ...]

i.e. "`count` locations here (residential=1 / business-only=0) are served by
exactly this set of providers at these advertised speeds".  The map page can
then compute exact distinct-location counts for any combination of ISP /
minimum-speed / residential filters without double counting a home that
several ISPs serve.

Each feature also carries  t: [residential_total, business_only_total]  - the
number of distinct FCC serviceable locations of ANY technology in the files.
Satellite ISPs file nearly every location, so dropping a satellite (or cable)
file for the same state into data/raw/ turns this into the true number of
locations, and the map can then say whether an ISP reaches every location in
a block or only some.  With only the fiber file present the totals equal the
fiber-served counts (meta.denominator = "fiber").

The FCC public files identify each serviceable location only by an opaque
location_id plus its census block (block_geoid) and H3 cell; coordinates for
individual homes are part of the licensed CostQuest Fabric and are not in the
public download.  Census block is therefore the finest public geography.

BDC availability CSV columns (Dec 2025 vintage):
  frn, provider_id, brand_name, location_id, technology,
  max_advertised_download_speed, max_advertised_upload_speed, low_latency,
  business_residential_code, state_usps, block_geoid, h3_res8_id
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import h3
from shapely.geometry import shape, mapping

FIBER_TECH = "50"          # BDC technology code for Fiber to the Premises
BAY_AREA = ["06001", "06013", "06041", "06055", "06075", "06081", "06085", "06095", "06097"]
COUNTY_NAMES = {"06001": "Alameda", "06013": "Contra Costa", "06041": "Marin", "06055": "Napa",
                "06075": "San Francisco", "06081": "San Mateo", "06085": "Santa Clara",
                "06095": "Solano", "06097": "Sonoma"}
BLOCK_TILE_ZOOM = 12
SIMPLIFY_DEG = 0.00002     # ~2 m: TIGER block outlines carry far more vertices than the map needs
COORD_DECIMALS = 5         # ~1.1 m

# Friendlier names than the brand_name column where it is a legal entity name.
DISPLAY_NAMES = {
    "190425": "Sonic",
    "130982": "Zayo",
    "200014": "Cogent",
}
_SUFFIXES = re.compile(r"\s*(,|\s)\s*(LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|Co\.?|Ltd\.?)\s*$", re.I)


def display_name(provider_id: str, brand: str) -> str:
    if provider_id in DISPLAY_NAMES:
        return DISPLAY_NAMES[provider_id]
    name = _SUFFIXES.sub("", brand).strip()
    return name or brand


def iter_csv_tables(path: Path):
    """Yield (header, row_iterator) for a CSV or for every CSV inside a zip.
    Rows are plain lists: much faster than DictReader on multi-million-row files."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    with zf.open(name) as fh:
                        text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                        rd = csv.reader(text)
                        header = [h.strip().lower() for h in next(rd)]
                        yield header, rd
    else:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rd = csv.reader(fh)
            header = [h.strip().lower() for h in next(rd)]
            yield header, rd


def as_of_from_name(name: str) -> str | None:
    """FCC file names carry the filing period as J24 / D25 (June / December)."""
    m = re.search(r"_([JD])(\d{2})_", name)
    if not m:
        return None
    month = "June" if m.group(1) == "J" else "December"
    return f"{month} 20{m.group(2)}"


SUMMARY_SPEEDS = ["speed_25_3", "speed_100_20", "speed_250_25", "speed_1000_100"]


def is_availability_file(path: Path) -> bool:
    """The download page also offers summary and provider-list files; skip those."""
    n = path.name.lower()
    return path.suffix.lower() in (".csv", ".zip") and "summary" not in n and "provider_list" not in n


def load_fcc_summary(paths, counties) -> dict | None:
    """Unit-weighted availability shares for the region from the FCC summary-by-geography
    file (County rows, residential, all areas): {units, per_tech: {tech: {speed: share}}}."""
    tech_labels = {"Fiber": "fiber", "Fiber to the Premises": "fiber", "Any Technology": "any"}
    units: dict[str, int] = {}
    acc: dict[str, dict[str, float]] = {t: {k: 0.0 for k in SUMMARY_SPEEDS} for t in tech_labels.values()}
    found = False
    for path in paths:
        for header, rows in iter_csv_tables(path):
            col = {name: i for i, name in enumerate(header)}
            need = ("geography_type", "geography_id", "area_data_type", "biz_res", "technology", "total_units")
            if any(k not in col for k in need) or any(k not in col for k in SUMMARY_SPEEDS):
                continue
            for row in rows:
                if row[col["geography_type"]] != "County" or row[col["area_data_type"]] != "Total" or row[col["biz_res"]] != "R":
                    continue
                cid = row[col["geography_id"]].zfill(5)
                if cid not in counties:
                    continue
                tech = tech_labels.get(row[col["technology"]])
                if not tech:
                    continue
                found = True
                u = int(float(row[col["total_units"]] or 0))
                units[cid] = max(units.get(cid, 0), u)
                for k in SUMMARY_SPEEDS:
                    acc[tech][k] += u * float(row[col[k]] or 0)
    if not found:
        return None
    total = sum(units.values())
    return {
        "source": "FCC Fixed Broadband Summary by Geography (county rows, residential units, all areas)",
        "units_res": total,
        "counties": len(units),
        "share": {t: {k: round(v / total, 4) for k, v in acc[t].items()} for t in acc} if total else {},
    }


def _speed(v: str) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def tile_xy(lng: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lng + 180.0) / 360.0 * n)
    lat_r = math.radians(max(-85.05, min(85.05, lat)))
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def load_locations(paths, counties, tech, provider_ids=None):
    """Scan the FCC files once.

    counties  tuple of 5-digit state+county prefixes to keep
    Returns
      locs    {location_id: {"geoid", "h3", "res", "prov": {provider_id: (down, up)}}}
              for locations with a matching (fiber) row.  res is True when any
              provider reports the location as residential (code R or X);
              business-only locations (all rows B) have res False.
      brands  {provider_id: Counter(brand_name)}
      every   {location_id: [geoid, res, h3]} for locations of ANY technology
              in the files (the per-block / per-hex denominator)
      techs   set of technology codes seen
    """
    counties = tuple(counties)
    pids = {str(p).strip() for p in (provider_ids or []) if str(p).strip()}
    locs: dict[str, dict] = {}
    brands: dict[str, Counter] = defaultdict(Counter)
    every: dict[str, list] = {}
    techs: set[str] = set()
    seen = kept = 0
    for path in paths:
        print(f"reading {path} ...", file=sys.stderr)
        for header, rows in iter_csv_tables(path):
            col = {name: i for i, name in enumerate(header)}
            try:
                i_geoid, i_loc, i_tech = col["block_geoid"], col["location_id"], col["technology"]
                i_down, i_up = col["max_advertised_download_speed"], col["max_advertised_upload_speed"]
                i_pid, i_brand, i_br = col["provider_id"], col["brand_name"], col["business_residential_code"]
                i_h3 = col["h3_res8_id"]
            except KeyError as exc:
                raise SystemExit(f"{path}: missing expected column {exc}; is this an FCC availability file?")
            for row in rows:
                seen += 1
                if seen % 2_000_000 == 0:
                    print(f"  ... {seen:,} rows", file=sys.stderr)
                geoid = row[i_geoid]
                if not geoid.startswith(counties):
                    continue
                row_tech = row[i_tech].strip()
                techs.add(row_tech)
                br = (row[i_br] or "X").strip().upper()
                ev = every.get(row[i_loc])
                if ev is None:
                    every[row[i_loc]] = [geoid, br != "B", row[i_h3].strip()]
                elif br != "B":
                    ev[1] = True
                if tech and row_tech != tech:
                    continue
                pid = row[i_pid].strip()
                if pids and pid not in pids:
                    continue
                kept += 1
                brands[pid][row[i_brand].strip()] += 1
                down, up = _speed(row[i_down]), _speed(row[i_up])
                loc = locs.get(row[i_loc])
                if loc is None:
                    loc = locs[row[i_loc]] = {"geoid": geoid, "h3": row[i_h3].strip(), "res": False, "prov": {}}
                if br != "B":
                    loc["res"] = True
                prev = loc["prov"].get(pid)
                if prev is None or down > prev[0]:
                    loc["prov"][pid] = (down, up)
    print(f"  rows read: {seen:,}  rows kept: {kept:,}  distinct locations: {len(locs):,}  "
          f"providers: {len(brands)}  locations of any technology: {len(every):,}  techs: {sorted(techs)}",
          file=sys.stderr)
    return locs, brands, every, techs


def build_providers(locs, brands):
    """Providers ordered by residential location count; index = position."""
    n_res: Counter = Counter()
    n_all: Counter = Counter()
    blocks: dict[str, set] = defaultdict(set)
    max_down: Counter = Counter()
    for loc in locs.values():
        for pid, (down, _up) in loc["prov"].items():
            n_all[pid] += 1
            n_res[pid] += loc["res"]
            blocks[pid].add(loc["geoid"])
            max_down[pid] = max(max_down[pid], down)
    order = sorted(brands, key=lambda p: (-n_res[p], -n_all[p], p))
    providers = []
    for pid in order:
        brand = brands[pid].most_common(1)[0][0]
        providers.append({
            "id": pid,
            "brand": brand,
            "name": display_name(pid, brand),
            "locations": n_all[pid],
            "locations_res": n_res[pid],
            "blocks": len(blocks[pid]),
            "max_down": max_down[pid],
        })
    return providers


def combos_by_key(locs, pidx, key):
    """{feature key: Counter((res, ((pidx, down, up), ...)))}; key is 'geoid' or 'h3'."""
    per: dict[str, Counter] = defaultdict(Counter)
    for loc in locs.values():
        k = (1 if loc["res"] else 0,
             tuple(sorted((pidx[p], d, u) for p, (d, u) in loc["prov"].items())))
        per[loc[key]][k] += 1
    return per


def totals_by_key(every, idx) -> dict[str, list]:
    """{key: [residential_total, business_only_total]} over locations of any technology."""
    t: dict[str, list] = defaultdict(lambda: [0, 0])
    for ev in every.values():
        t[ev[idx]][0 if ev[1] else 1] += 1
    return t


def combos_json(counter: Counter | None) -> list:
    if not counter:
        return []
    return [[res, [list(t) for t in provs], n] for (res, provs), n in sorted(counter.items(), key=lambda kv: -kv[1])]


def bbox(geometry: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    for poly in polys:
        for x, y in poly[0]:
            xs.append(x)
            ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _round_coords(obj):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], float):
            return [round(v, COORD_DECIMALS) for v in obj]
        return [_round_coords(v) for v in obj]
    return obj


def slim_geometry(geometry: dict) -> dict:
    """Simplify a block outline for the web map (topology preserved per block)."""
    try:
        g = shape(geometry).simplify(SIMPLIFY_DEG, preserve_topology=True)
        if g.is_empty:
            raise ValueError
        geometry = mapping(g)
    except Exception:  # noqa: BLE001 - keep the original outline on any failure
        pass
    return {"type": geometry["type"], "coordinates": _round_coords(geometry["coordinates"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw", help="directory of FCC availability CSV/zip files")
    ap.add_argument("--blocks-dir", default="data/blocks", help="directory of 06<county>.geojson block files")
    ap.add_argument("--out", default="site/data", help="output directory")
    ap.add_argument("--county", default=",".join(BAY_AREA),
                    help="comma-separated 5-digit state+county FIPS prefixes (default: the nine Bay Area counties)")
    ap.add_argument("--region", default="Bay Area", help="label for the region in the page")
    ap.add_argument("--tech", default=FIBER_TECH, help="BDC technology code to keep; '' for any")
    ap.add_argument("--provider-id", default="", help="comma-separated provider_ids to keep (default: all)")
    ap.add_argument("--as-of", default="", help="override the data vintage label")
    args = ap.parse_args()

    raw_dir = Path(args.raw)
    all_files = sorted(p for p in raw_dir.glob("*") if p.suffix.lower() in (".csv", ".zip"))
    paths = [p for p in all_files if is_availability_file(p)]
    summary_paths = [p for p in all_files if "summary_by_geography" in p.name.lower()]
    if not paths:
        raise SystemExit(f"no .csv/.zip files found in {raw_dir}/ - run scripts/fetch_fcc.py or drop the "
                         "FCC download there (see README)")
    counties = [c.strip() for c in args.county.split(",") if c.strip()]
    block_files = [Path(args.blocks_dir) / f"{c}.geojson" for c in counties]
    missing = [str(p) for p in block_files if not p.exists()]
    if missing:
        raise SystemExit(f"missing block files (run scripts/fetch_blocks.py): {missing}")

    provider_ids = [p for p in args.provider_id.split(",") if p.strip()]
    fcc_summary = load_fcc_summary(summary_paths, set(counties)) if summary_paths else None
    if fcc_summary:
        sh = fcc_summary["share"]
        print(f"  FCC summary: {fcc_summary['units_res']:,} residential units in {fcc_summary['counties']} counties; "
              f"fiber 1000/100 reaches {sh['fiber']['speed_1000_100']:.1%}, any technology {sh['any']['speed_1000_100']:.1%}",
              file=sys.stderr)
    locs, brands, every, techs = load_locations(paths, counties, args.tech, provider_ids)
    if not locs:
        raise SystemExit("no matching locations - check --county / --tech / --provider-id filters")
    providers = build_providers(locs, brands)
    pidx = {p["id"]: i for i, p in enumerate(providers)}
    by_block = combos_by_key(locs, pidx, "geoid")
    by_hex = combos_by_key(locs, pidx, "h3")
    tot_block = totals_by_key(every, 0)
    tot_hex = totals_by_key(every, 2)
    denominator = "all" if (techs - {args.tech}) else "fiber"
    if denominator == "fiber":
        print("  note: only fiber rows found, so per-block totals equal fiber-served counts. Add a "
              "satellite or cable file for the same state to data/raw/ for true location totals.",
              file=sys.stderr)
    for p in providers[:15]:
        print(f"  {p['locations']:>9,} locations  {p['blocks']:>6,} blocks  {p['name']} [{p['id']}]", file=sys.stderr)
    if len(providers) > 15:
        print(f"  ... and {len(providers) - 15} more providers", file=sys.stderr)

    # ---- census blocks, tiled ------------------------------------------------
    out = Path(args.out)
    blocks_out = out / "blocks"
    blocks_out.mkdir(parents=True, exist_ok=True)
    for old in blocks_out.glob("*_*_*.json"):
        old.unlink()
    tiles: dict[tuple[int, int], list] = defaultdict(list)
    unmatched = set(by_block)
    n_blocks = hu_total = 0
    west = south = math.inf
    east = north = -math.inf
    for bf in block_files:
        fc = json.loads(bf.read_text())
        for f in fc["features"]:
            p = f["properties"]
            combos = by_block.get(p["geoid"])
            unmatched.discard(p["geoid"])
            if combos is None and p["housing_units"] == 0:
                continue  # parks, water, freeway blocks: nothing to show
            x0, y0, x1, y1 = bbox(f["geometry"])
            west, east, south, north = min(west, x0), max(east, x1), min(south, y0), max(north, y1)
            feature = {"type": "Feature", "geometry": slim_geometry(f["geometry"]), "properties": {
                "geoid": p["geoid"], "hu": p["housing_units"], "pop": p["population"],
                "c": combos_json(combos), "t": list(tot_block.get(p["geoid"], [0, 0]))}}
            tx0, ty0 = tile_xy(x0, y1, BLOCK_TILE_ZOOM)   # north-west corner
            tx1, ty1 = tile_xy(x1, y0, BLOCK_TILE_ZOOM)   # south-east corner
            for tx in range(tx0, tx1 + 1):
                for ty in range(ty0, ty1 + 1):
                    tiles[(tx, ty)].append(feature)
            n_blocks += 1
            hu_total += p["housing_units"]
    if unmatched:
        print(f"  warning: {len(unmatched)} block GEOIDs in the FCC data have no census geometry "
              f"(e.g. {sorted(unmatched)[:3]})", file=sys.stderr)
    tile_bytes = 0
    for (x, y), feats in tiles.items():
        tp = blocks_out / f"{BLOCK_TILE_ZOOM}_{x}_{y}.json"
        tp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        tile_bytes += tp.stat().st_size
    (blocks_out / "index.json").write_text(json.dumps({
        "z": BLOCK_TILE_ZOOM, "tiles": sorted(f"{x}_{y}" for x, y in tiles), "blocks": n_blocks}, separators=(",", ":")))
    print(f"  blocks: {n_blocks:,} in {len(tiles)} zoom-{BLOCK_TILE_ZOOM} tiles ({tile_bytes/1e6:.1f} MB; "
          f"largest {max(tp.stat().st_size for tp in blocks_out.glob(f'{BLOCK_TILE_ZOOM}_*.json'))/1e6:.1f} MB)", file=sys.stderr)

    # ---- H3 hexagons (overview + exact region-wide stats) --------------------
    def hex_features(combos_by_cell, totals_by_cell):
        feats = []
        for cell in sorted(set(combos_by_cell) | set(totals_by_cell)):
            ring = [[round(lng, 6), round(lat, 6)] for lat, lng in h3.cell_to_boundary(cell)]
            ring.append(ring[0])
            feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                          "properties": {"id": cell, "c": combos_json(combos_by_cell.get(cell)),
                                         "t": list(totals_by_cell.get(cell, [0, 0]))}})
        return feats

    bad = [c for c in set(by_hex) | set(tot_hex) if not h3.is_valid_cell(c)]
    for c in bad:
        by_hex.pop(c, None)
        tot_hex.pop(c, None)
    if bad:
        print(f"  warning: {len(bad)} invalid h3_res8_id values dropped", file=sys.stderr)
    hex_feats = hex_features(by_hex, tot_hex)
    # coarser res-7 parents for the zoomed-out view (same combination model, so it can be styled the same way)
    by_hex7: dict[str, Counter] = defaultdict(Counter)
    for cell, counter in by_hex.items():
        by_hex7[h3.cell_to_parent(cell, 7)].update(counter)
    tot_hex7: dict[str, list] = defaultdict(lambda: [0, 0])
    for cell, (r, bz) in tot_hex.items():
        parent = tot_hex7[h3.cell_to_parent(cell, 7)]
        parent[0] += r
        parent[1] += bz
    hex7_feats = hex_features(by_hex7, tot_hex7)

    as_of = args.as_of or next((v for v in (as_of_from_name(p.name) for p in paths) if v), "unknown")
    meta = {
        "as_of": as_of,
        "source_files": [p.name for p in paths],
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "region": args.region,
        "counties": {c: COUNTY_NAMES.get(c, c) for c in counties},
        "technology": args.tech,
        "techs_seen": sorted(techs),
        "denominator": denominator,
        "fcc_summary": fcc_summary,
        "locations_any_tech": len(every),
        "providers": providers,
        "locations": len(locs),
        "locations_res": sum(1 for l in locs.values() if l["res"]),
        "blocks_with_fiber": len(by_block),
        "blocks_total": n_blocks,
        "housing_units_total": hu_total,
        "hexes": len(hex_feats),
        "hexes7": len(hex7_feats),
        "bounds": [[round(south, 4), round(west, 4)], [round(north, 4), round(east, 4)]],
        "block_tiles": {"z": BLOCK_TILE_ZOOM, "count": len(tiles)},
    }
    payload = {"meta": meta, "hexes": {"type": "FeatureCollection", "features": hex_feats},
               "hexes7": {"type": "FeatureCollection", "features": hex7_feats}}
    ov = out / "overview.js"
    ov.write_text("window.FIBER_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    legacy = out / "fiber_blocks.js"
    if legacy.exists():
        legacy.unlink()
    print(json.dumps({k: v for k, v in meta.items() if k not in ("providers", "counties", "fcc_summary")}, indent=2))
    print(f"wrote {ov} ({ov.stat().st_size/1e6:.1f} MB) and {len(tiles)} block tiles in {blocks_out}/")


if __name__ == "__main__":
    main()
