#!/usr/bin/env python3
"""Join FCC BDC fiber availability records to census blocks and emit map data.

Inputs
  data/raw/*.csv | *.zip   FCC BDC "availability" CSVs (as downloaded from
                            broadbandmap.fcc.gov, optionally still zipped).
  data/blocks_sf.geojson   Census blocks from scripts/fetch_blocks.py.

Output
  site/data/fiber_blocks.js   window.FIBER_DATA = {meta, blocks}

Every fiber provider in the files is kept.  For each census block the output
stores, instead of plain totals, a small list of *location combinations*:

    c: [[res, [[provider_idx, down_mbps, up_mbps], ...], count], ...]

i.e. "`count` locations in this block (residential=1 / business-only=0) are
served by exactly this set of providers at these advertised speeds".  The map
page can then compute exact distinct-location counts for any combination of
ISP / minimum-speed / residential filters without double counting a home that
several ISPs serve.

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
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

FIBER_TECH = "50"          # BDC technology code for Fiber to the Premises
SF_COUNTY_GEOID = "06075"  # state 06 (CA) + county 075 (San Francisco)

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


def _speed(v: str) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def load_locations(paths, county, tech, provider_ids=None):
    """Scan the FCC files once.

    Returns
      locs    {location_id: {"geoid", "res", "prov": {provider_id: (down, up)}}}
              res is True when any provider reports the location as residential
              (code R or X); business-only locations (all rows B) have res False.
      brands  {provider_id: Counter(brand_name)}
    """
    pids = {str(p).strip() for p in (provider_ids or []) if str(p).strip()}
    locs: dict[str, dict] = {}
    brands: dict[str, Counter] = defaultdict(Counter)
    seen = kept = 0
    for path in paths:
        print(f"reading {path} ...", file=sys.stderr)
        for header, rows in iter_csv_tables(path):
            col = {name: i for i, name in enumerate(header)}
            try:
                i_geoid, i_loc, i_tech = col["block_geoid"], col["location_id"], col["technology"]
                i_down, i_up = col["max_advertised_download_speed"], col["max_advertised_upload_speed"]
                i_pid, i_brand, i_br = col["provider_id"], col["brand_name"], col["business_residential_code"]
            except KeyError as exc:
                raise SystemExit(f"{path}: missing expected column {exc}; is this an FCC availability file?")
            for row in rows:
                seen += 1
                if seen % 2_000_000 == 0:
                    print(f"  ... {seen:,} rows", file=sys.stderr)
                geoid = row[i_geoid]
                if not geoid.startswith(county):
                    continue
                if tech and row[i_tech].strip() != tech:
                    continue
                pid = row[i_pid].strip()
                if pids and pid not in pids:
                    continue
                kept += 1
                brands[pid][row[i_brand].strip()] += 1
                br = (row[i_br] or "X").strip().upper()
                down, up = _speed(row[i_down]), _speed(row[i_up])
                loc = locs.get(row[i_loc])
                if loc is None:
                    loc = locs[row[i_loc]] = {"geoid": geoid, "res": False, "prov": {}}
                if br != "B":
                    loc["res"] = True
                prev = loc["prov"].get(pid)
                if prev is None or down > prev[0]:
                    loc["prov"][pid] = (down, up)
    print(f"  rows read: {seen:,}  rows kept: {kept:,}  distinct locations: {len(locs):,}  "
          f"providers: {len(brands)}", file=sys.stderr)
    return locs, brands


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


def combos_by_block(locs, pidx):
    """{geoid: Counter((res, ((pidx, down, up), ...)))}"""
    per_block: dict[str, Counter] = defaultdict(Counter)
    for loc in locs.values():
        key = (1 if loc["res"] else 0,
               tuple(sorted((pidx[p], d, u) for p, (d, u) in loc["prov"].items())))
        per_block[loc["geoid"]][key] += 1
    return per_block


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default="data/raw", help="directory of FCC availability CSV/zip files")
    ap.add_argument("--blocks", default="data/blocks_sf.geojson")
    ap.add_argument("--out", default="site/data/fiber_blocks.js")
    ap.add_argument("--county", default=SF_COUNTY_GEOID, help="5-digit state+county FIPS prefix")
    ap.add_argument("--tech", default=FIBER_TECH, help="BDC technology code to keep; '' for any")
    ap.add_argument("--provider-id", default="", help="comma-separated provider_ids to keep (default: all)")
    ap.add_argument("--as-of", default="", help="override the data vintage label")
    args = ap.parse_args()

    raw_dir = Path(args.raw)
    paths = sorted(p for p in raw_dir.glob("*") if p.suffix.lower() in (".csv", ".zip"))
    if not paths:
        raise SystemExit(f"no .csv/.zip files found in {raw_dir}/ - run scripts/fetch_fcc.py or drop the "
                         "FCC download there (see README)")

    provider_ids = [p for p in args.provider_id.split(",") if p.strip()]
    locs, brands = load_locations(paths, args.county, args.tech, provider_ids)
    if not locs:
        raise SystemExit("no matching locations - check --county / --tech / --provider-id filters")
    providers = build_providers(locs, brands)
    pidx = {p["id"]: i for i, p in enumerate(providers)}
    per_block = combos_by_block(locs, pidx)
    for p in providers:
        print(f"  {p['locations']:>8,} locations  {p['blocks']:>5,} blocks  {p['name']} [{p['id']}]",
              file=sys.stderr)

    fc = json.loads(Path(args.blocks).read_text())
    features = []
    unmatched = set(per_block)
    for f in fc["features"]:
        p = f["properties"]
        combos = per_block.get(p["geoid"])
        unmatched.discard(p["geoid"])
        if combos is None and p["housing_units"] == 0:
            continue  # parks, water, freeway blocks: nothing to show
        c = [[res, [list(t) for t in provs], n] for (res, provs), n in sorted(combos.items(), key=lambda kv: -kv[1])] if combos else []
        features.append({"type": "Feature", "geometry": f["geometry"], "properties": {
            "geoid": p["geoid"], "hu": p["housing_units"], "pop": p["population"], "c": c}})
    if unmatched:
        print(f"  warning: {len(unmatched)} block GEOIDs in the FCC data have no census geometry "
              f"(e.g. {sorted(unmatched)[:3]})", file=sys.stderr)

    as_of = args.as_of or next((v for v in (as_of_from_name(p.name) for p in paths) if v), "unknown")
    meta = {
        "as_of": as_of,
        "source_files": [p.name for p in paths],
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "county": args.county,
        "technology": args.tech,
        "providers": providers,
        "locations": len(locs),
        "locations_res": sum(1 for l in locs.values() if l["res"]),
        "blocks_with_fiber": len(per_block),
        "blocks_total": len(features),
        "housing_units_total": sum(f["properties"]["hu"] for f in features),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "blocks": {"type": "FeatureCollection", "features": features}}
    out.write_text("window.FIBER_DATA = " + json.dumps(payload, separators=(",", ":")) + ";\n")
    print(json.dumps({k: v for k, v in meta.items() if k != "providers"}, indent=2))
    print(f"wrote {out} ({out.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
