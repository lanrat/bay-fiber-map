# San Francisco fiber map

A local, static map of which San Francisco census blocks have fiber-to-the-premises
internet, from **every ISP that files fiber availability with the FCC** (Sonic,
AT&T, Astound, Xfinity, ...), filterable by ISP and advertised speed. Built from
the FCC's Broadband Data Collection (BDC) availability data behind the
[National Broadband Map](https://broadbandmap.fcc.gov/).

```
make blocks      # 1. census block boundaries for SF  (Census TIGERweb, no key)
make fetch       # 2. FCC availability file            (see "Getting the FCC data")
make build       # 3. join -> site/data/fiber_blocks.js
make buildings   # 4. building footprints (DataSF) -> site/data/buildings/  (optional)
make serve       # 5. open http://localhost:8000/
```

Python 3.9+ and `requests` are needed (`pip install -r requirements.txt`);
`make buildings` also needs `shapely`.
The map page itself is plain HTML + [Leaflet](https://leafletjs.com/) (vendored
in `site/vendor/`); it needs internet access only for the OpenStreetMap basemap tiles
and the optional address search.

## What the map shows

Each 2020 census block is colored by the best match among its locations for the
current filters:

* **ISPs** - tick any combination of providers (counts show that ISP's
  locations at the selected minimum speed).
* **Minimum advertised download** - any / 1 / 2 / 5 / 10 Gbps. Default 1 Gbps.
* **Include business-only locations** - off by default, so the map is about homes.
  Some ISPs (Zayo, Cogent, Vast, most of Xfinity's fiber) only file business locations.
* **Color blocks by** *fastest speed* (10 Gbps+, 5-10, 1-5, under 1) or
  *number of ISPs* meeting the filters (1, 2, 3+), i.e. how much choice a block has.

Hover or click a block for every ISP present, its serviceable locations and
maximum advertised speeds, whether the selected ISPs reach **all** or only
**some** of the block's locations, the 2020 census housing units and
population, and a link that opens the same spot on the FCC map, where the
FCC shows its per-address dots. "Table view" lists matching blocks;
"Download CSV" exports every block with per-ISP columns.

**Zoomed in (zoom 15+) the map switches to building footprints** from DataSF,
each painted with its block's status: solid where the selected ISPs reach every
location in the block, faded with a dashed outline where they reach only some
(the FCC data does not say which buildings), gray where none match.

The summary tiles are exact distinct-location counts, not sums per ISP: the
build stores, per block, how many locations are served by each *combination*
of ISP and speed, so a home with both Sonic and AT&T is counted once.

### Confirmed vs partial blocks, and the location total

"Reaches all N locations" needs the true number of FCC serviceable locations
in a block, which the fiber file alone cannot give (it lists only fiber-served
ones). Satellite ISPs file nearly every location in the country, so dropping a
**satellite (or cable) availability file for California** into `data/raw/`
next to the fiber file makes `build_map.py` count every location per block;
the map then reports `meta.denominator = "all"` and labels blocks accordingly.
With only the fiber file the totals are fiber-served locations and the page
says so.

### Why blocks and not individual homes

The public FCC files identify each serviceable location only by an opaque
`location_id`, its census block (`block_geoid`) and an H3 hexagon. The
coordinates and addresses behind those IDs are the CostQuest **Broadband
Serviceable Location Fabric**, which is licensed and not part of the public
download, so a census block is the finest public geography. In San Francisco a
block is usually a single city block, which is close to the "which homes"
question but not the same thing: a block is colored "10 Gbps+" if *any*
matching location in it is reported at 10 Gbps. The per-block counts in the
popup show how many locations that is.

Other caveats:

* Counts are FCC "broadband serviceable locations" (roughly structures), not
  housing units; a 40-unit building is one location.
* Resold service is filed by the network owner: Sonic's resale of AT&T fiber
  shows up under AT&T (provider 130077), not Sonic.
* Speeds are what the provider *advertises* to the FCC, as of the filing date.

## Getting the FCC data

`scripts/build_map.py` reads every `.csv`/`.zip` in `data/raw/` that follows the
FCC availability schema (`frn, provider_id, brand_name, location_id, technology,
max_advertised_download_speed, ...`). Any of these works:

1. **State-wide fiber file** (recommended; every ISP):
   on the [Data Download](https://broadbandmap.fcc.gov/data-download/nationwide-data?version=dec2025)
   page choose *Fixed Broadband → State → California → Fiber to the Premises → CSV*.
   The file is named like `bdc_06_FibertothePremises_fixed_broadband_D25_15sep2026.zip`
   (D25 = data as of December 2025). Drop it in `data/raw/`.
2. **Provider file(s)**: *Provider → e.g. Sonic Telecom, LLC → California → Fiber
   to the Premises → CSV*. Smaller; the map then only knows about those ISPs.
3. **`scripts/fetch_fcc.py`** automates either download:
   * `--mode api` (default) uses the official BDC Public Data API and needs a free
     FCC account token: sign in at broadbandmap.fcc.gov, open *Manage API Access*,
     generate a token, then `export FCC_USERNAME=... FCC_HASH_VALUE=...`.
   * `--mode web` calls the same endpoints the website's download page uses, with
     no account. The FCC's CDN blocks many cloud/datacenter IP ranges, so run
     this from a normal home or office network.
   * `--all-providers` fetches the state-wide file (option 1) instead of Sonic's.

Then `python scripts/build_map.py`. Useful flags: `--provider-id` to keep only
some ISPs (e.g. `190425` = Sonic Telecom, LLC), `--tech` (`50` = fiber; `40` =
cable if you download that file too), `--county` (any 5-digit state+county FIPS,
e.g. `06001` for Alameda; run `scripts/fetch_blocks.py --county 001` first),
`--as-of` to label the vintage.

## Deploying to GitHub Pages

`.github/workflows/pages.yml` runs the tests and publishes `site/` to GitHub
Pages on every push to the default branch (other branches only build and test).
The built `site/data/fiber_blocks.js` is committed, so the deploy needs nothing
from the FCC; to refresh the data either rebuild locally and commit the new
file, or set the repository variable `FCC_DATA_URL` to a URL of the FCC
California fiber zip and the workflow rebuilds it on each deploy.

One-time setup: in the repository *Settings -> Pages*, set **Source** to
**GitHub Actions** (the workflow also tries to enable this itself). The map is
then served at `https://<user>.github.io/<repo>/`.

## Layout

```
scripts/fetch_blocks.py   Census TIGERweb -> data/blocks_sf.geojson (committed, 2.9 MB)
scripts/fetch_fcc.py      FCC BDC download (API or website mode) -> data/raw/
scripts/build_map.py      join + per-block aggregation -> site/data/fiber_blocks.js
scripts/fetch_buildings.py DataSF footprints -> site/data/buildings/14_x_y.json (tiled, 14 MB)
site/index.html           the map (Leaflet, no build step)
tests/                    unit tests on a small SYNTHETIC fixture (`make test`)
```

`data/raw/` is git-ignored; `site/data/fiber_blocks.js` is committed (see above).

## Sources

* FCC National Broadband Map, BDC fixed availability data, December 31 2025 vintage.
  [Data download](https://broadbandmap.fcc.gov/data-download/nationwide-data?version=dec2025),
  [Public Data API spec](https://www.fcc.gov/sites/default/files/bdc-public-data-api-spec.pdf).
* U.S. Census Bureau, [TIGERweb](https://tigerweb.geo.census.gov/) 2020 census blocks.
* DataSF, [Building Footprints](https://data.sf.gov/d/ynuv-fyni) (City and County of San Francisco).
* Basemap tiles © OpenStreetMap contributors. Address search: OSM Nominatim.
