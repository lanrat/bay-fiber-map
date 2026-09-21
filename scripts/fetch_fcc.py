#!/usr/bin/env python3
"""Download Sonic's fixed-broadband availability CSV for California from the
FCC National Broadband Map (Broadband Data Collection, "BDC").

Two ways in, pick one:

  --mode api  (default)  Official BDC Public Data API. Needs a free FCC account
                         and an API token:
                           1. Sign in at https://broadbandmap.fcc.gov
                           2. Account menu -> "Manage API Access" -> generate token
                           3. export FCC_USERNAME='you@example.com'
                              export FCC_HASH_VALUE='<the token>'
                         Works from any network (the API path is not behind the
                         site's bot protection).

  --mode web             The same endpoints the website's Data Download page
                         calls. No account needed, but the FCC's CDN blocks
                         many cloud / datacenter IP ranges with "Access Denied";
                         run it from a normal home or office connection.

Either way the result is data/raw/<fcc file name>.zip, which build_map.py reads.

Examples
  python scripts/fetch_fcc.py --list-dates
  python scripts/fetch_fcc.py                       # newest vintage, Sonic, CA, fiber
  python scripts/fetch_fcc.py --as-of 2025-12-31
  python scripts/fetch_fcc.py --all-providers       # state-wide CA fiber file (all ISPs)
  python scripts/fetch_fcc.py --dump-listing        # print the raw file listing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://broadbandmap.fcc.gov/api/public/map"
WEB_BASE = "https://broadbandmap.fcc.gov/nbm/map/api"
UA = "Mozilla/5.0 (X11; Linux x86_64) fcc-fiber-map/1.0"

# BDC technology codes
TECH = {"10": "Copper", "40": "Cable", "50": "Fiber to the Premises", "60": "GSO Satellite",
        "61": "NGSO Satellite", "70": "Unlicensed Fixed Wireless", "71": "Licensed Fixed Wireless",
        "72": "LBR Fixed Wireless", "0": "Other"}


def die(msg: str) -> None:
    raise SystemExit(f"error: {msg}")


def pick(d: dict, *keys, default=None):
    """First present key, case-insensitive."""
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in lower and lower[k.lower()] not in (None, ""):
            return lower[k.lower()]
    return default


class Client:
    def __init__(self, mode: str, username: str | None, token: str | None):
        self.mode = mode
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.s.headers["Accept"] = "application/json, */*"
        if mode == "api":
            if not (username and token):
                die("--mode api needs FCC_USERNAME and FCC_HASH_VALUE in the environment "
                    "(see the docstring at the top of this script)")
            self.s.headers["username"] = username
            self.s.headers["hash_value"] = token
        else:
            self.s.headers["Referer"] = "https://broadbandmap.fcc.gov/data-download/nationwide-data"

    def get_json(self, url: str, **params) -> dict:
        for attempt in range(4):
            r = self.s.get(url, params=params or None, timeout=120)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code == 403 and "Access Denied" in r.text:
                die("the FCC CDN refused the request (Access Denied). This IP range is blocked; "
                    "use --mode api with an API token, or run from a home/office network.")
            if r.status_code == 401:
                die(f"401 Unauthorized from {url} - check FCC_USERNAME / FCC_HASH_VALUE")
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                die(f"non-JSON response from {url}: {r.text[:300]}")
        die("too many rate-limit retries")

    # ---- listing -----------------------------------------------------------
    def as_of_dates(self) -> list[str]:
        if self.mode == "api":
            data = self.get_json(f"{API_BASE}/listAsOfDates").get("data", [])
            dates = sorted({str(pick(d, "as_of_date"))[:10] for d in data
                            if str(pick(d, "data_type", default="availability")).lower().startswith("avail")})
        else:
            data = self.get_json(f"{WEB_BASE}/published/filing").get("data", [])
            dates = sorted({str(pick(d, "as_of_date"))[:10] for d in data if pick(d, "as_of_date")})
        return dates

    def listing(self, as_of: str, category: str, subcategory: str | None) -> list[dict]:
        if self.mode == "api":
            params = {"category": category}
            if subcategory:
                params["subcategory"] = subcategory
            resp = self.get_json(f"{API_BASE}/downloads/listAvailabilityData/{as_of}", **params)
            if str(resp.get("status", "")).lower() not in ("", "success", "ok") and not resp.get("data"):
                die(f"API said: {resp}")
            return resp.get("data", [])
        # web mode: find the filing (process_uuid) for that as-of date, then its file list
        filings = self.get_json(f"{WEB_BASE}/published/filing").get("data", [])
        cands = [f for f in filings if str(pick(f, "as_of_date"))[:10] == as_of]
        if not cands:
            die(f"no published filing for {as_of}; known dates: {sorted({str(pick(f,'as_of_date'))[:10] for f in filings})}")
        # prefer the 'availability' filing if several share the date
        cands.sort(key=lambda f: 0 if "avail" in json.dumps(f).lower() else 1)
        uuid = pick(cands[0], "process_uuid", "processUuid", "uuid")
        if not uuid:
            die(f"cannot find process_uuid in filing entry: {cands[0]}")
        files = self.get_json(f"{WEB_BASE}/national_map_process/nbm_get_data_download/{uuid}").get("data", [])
        return [f for f in files if str(pick(f, "category", default="")).lower().startswith(category.lower())]

    # ---- download ----------------------------------------------------------
    def download(self, entry: dict, out_dir: Path) -> Path:
        fid = pick(entry, "file_id", "id")
        name = str(pick(entry, "file_name", default=f"bdc_{fid}.zip"))
        if self.mode == "api":
            urls = [f"{API_BASE}/downloads/downloadFile/availability/{fid}",
                    f"{API_BASE}/downloads/downloadFile/availability/{fid}/1"]
        else:
            urls = [f"{WEB_BASE}/getNBMDataDownloadFile/{fid}/1"]
        out_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            with self.s.get(url, stream=True, timeout=600) as r:
                if r.status_code != 200:
                    print(f"  {url} -> HTTP {r.status_code}", file=sys.stderr)
                    continue
                first = next(r.iter_content(chunk_size=4), b"")
                if not first.startswith(b"PK"):   # not a zip: probably an error payload
                    print(f"  {url} -> not a zip ({r.headers.get('content-type')}): "
                          f"{(first + r.raw.read(200)).decode('utf-8', 'replace')[:200]}", file=sys.stderr)
                    continue
                cd = r.headers.get("content-disposition", "")
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    name = m.group(1)
                if not name.lower().endswith(".zip"):
                    name += ".zip"
                dest = out_dir / name
                tmp = dest.with_suffix(".part")
                total = 0
                with tmp.open("wb") as fh:
                    fh.write(first)
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        total += len(chunk)
                        print(f"\r  {name}: {total/1e6:,.1f} MB", end="", file=sys.stderr)
                print(file=sys.stderr)
                tmp.replace(dest)
                return dest
        die(f"could not download file_id={fid} ({name})")


def describe(e: dict) -> str:
    return (f"id={pick(e,'file_id','id')} state={pick(e,'state_fips','state_usps','state_name')} "
            f"provider={pick(e,'provider_name','brand_name')!s} ({pick(e,'provider_id')}) "
            f"tech={pick(e,'technology_code','technology_code_desc','technology_desc')} "
            f"type={pick(e,'file_type')} records={pick(e,'record_count')} name={pick(e,'file_name')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["api", "web"], default="api")
    ap.add_argument("--as-of", help="vintage, YYYY-MM-DD (default: newest)")
    ap.add_argument("--list-dates", action="store_true", help="print available vintages and exit")
    ap.add_argument("--provider", default="sonic", help="regex on provider name (default: sonic)")
    ap.add_argument("--provider-id", help="exact FCC provider_id instead of a name match")
    ap.add_argument("--state", default="06", help="state FIPS (default 06 = California)")
    ap.add_argument("--tech", default="50", help="comma-separated technology codes (default 50 = fiber)")
    ap.add_argument("--all-providers", action="store_true",
                    help="download the state-wide file(s) for --tech across every provider "
                         "(use as build_map.py --denominator to compute coverage share)")
    ap.add_argument("--category", help="override listing category (Provider | State | Summary)")
    ap.add_argument("--subcategory", help="override listing subcategory")
    ap.add_argument("--dump-listing", action="store_true", help="print the raw listing entries and exit")
    ap.add_argument("--out", default="data/raw")
    args = ap.parse_args()

    client = Client(args.mode, os.environ.get("FCC_USERNAME"), os.environ.get("FCC_HASH_VALUE"))

    dates = client.as_of_dates()
    if args.list_dates:
        print("\n".join(dates) or "(none)")
        return
    if not dates:
        die("no as-of dates returned")
    as_of = args.as_of or dates[-1]
    print(f"as-of date: {as_of}  (available: {', '.join(dates)})", file=sys.stderr)

    category = args.category or ("State" if args.all_providers else "Provider")
    # The API's subcategory names have shifted between releases; try the known ones.
    subcats = [args.subcategory] if args.subcategory else ["Location Coverage", "Fixed Broadband", None]
    entries: list[dict] = []
    for sc in subcats:
        try:
            entries = client.listing(as_of, category, sc)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"  listing category={category} subcategory={sc}: {exc}", file=sys.stderr)
            entries = []
        if entries:
            print(f"listing: category={category} subcategory={sc}: {len(entries):,} entries", file=sys.stderr)
            break
    if not entries:
        die("empty listing; try --dump-listing with --category/--subcategory (the API error text "
            "usually names the valid values)")
    if args.dump_listing:
        print(json.dumps(entries[:50], indent=2))
        print(f"... {len(entries)} entries; distinct subcategories: "
              f"{sorted({str(pick(e,'subcategory')) for e in entries})}")
        return

    techs = {t.strip() for t in args.tech.split(",") if t.strip()}
    want = []
    for e in entries:
        ft = str(pick(e, "file_type", default="csv")).lower()
        if ft not in ("csv", "1"):
            continue
        st = str(pick(e, "state_fips", default="")).zfill(2)
        if st != args.state.zfill(2):
            continue
        tc = str(pick(e, "technology_code", default=""))
        tdesc = str(pick(e, "technology_code_desc", "technology_desc", default=""))
        if techs and tc not in techs and not any(TECH.get(t, "").lower() == tdesc.lower() for t in techs):
            continue
        if not args.all_providers:
            pid = str(pick(e, "provider_id", default=""))
            pname = str(pick(e, "provider_name", "brand_name", default=""))
            if args.provider_id:
                if pid != args.provider_id:
                    continue
            elif not re.search(args.provider, pname, re.I):
                continue
        want.append(e)

    if not want:
        die("nothing matched the filters; use --dump-listing to inspect the listing")
    print("matched files:", file=sys.stderr)
    for e in want:
        print("  " + describe(e), file=sys.stderr)
    providers = {str(pick(e, "provider_name", "brand_name")) for e in want}
    if len(providers) > 1 and not args.all_providers:
        print(f"note: {len(providers)} providers matched /{args.provider}/: {sorted(providers)}. "
              "All are downloaded; use --provider-id to narrow.", file=sys.stderr)

    out_dir = Path(args.out)
    for e in want:
        dest = client.download(e, out_dir)
        print(f"saved {dest} ({dest.stat().st_size/1e6:,.1f} MB)")
    print("next: python scripts/build_map.py")


if __name__ == "__main__":
    main()
