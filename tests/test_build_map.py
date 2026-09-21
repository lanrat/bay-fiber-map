"""Unit tests for scripts/build_map.py using a SYNTHETIC fixture.

tests/fixtures/synthetic_fiber_sample.csv is hand-written in the FCC BDC
availability schema; it is not FCC data and must not be mistaken for it.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_map  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
_G = json.loads((FIX / "synthetic_geoids.json").read_text())
GEOIDS, CELLS = _G["geoids"], _G["cells"]
SAMPLE = FIX / "synthetic_fiber_sample.csv"
SF = ("06075",)


class HelperTests(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(build_map.display_name("190425", "Sonic Telecom, LLC"), "Sonic")
        self.assertEqual(build_map.display_name("1", "ZAYO GROUP LLC"), "ZAYO GROUP")
        self.assertEqual(build_map.display_name("1", "Fusion Cloud Services, Inc."), "Fusion Cloud Services")
        self.assertEqual(build_map.display_name("1", "AT&T"), "AT&T")

    def test_as_of_from_name(self):
        self.assertEqual(build_map.as_of_from_name("bdc_06_FibertothePremises_fixed_broadband_D25_15sep2026.zip"), "December 2025")
        self.assertEqual(build_map.as_of_from_name("bdc_06_Fiber_fixed_broadband_J25_01jan2026.zip"), "June 2025")
        self.assertIsNone(build_map.as_of_from_name("whatever.csv"))

    def test_tile_xy(self):
        self.assertEqual(build_map.tile_xy(-122.42, 37.77, 11), (327, 791))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.locs, self.brands, self.every, self.techs = build_map.load_locations([SAMPLE], SF, "50")

    def test_filters_and_dedup(self):
        # 3 homes in block0, 2 in block1, 1 in block2, 1 business in block3; tech 10 and Alameda rows dropped
        self.assertEqual(len(self.locs), 7)
        self.assertEqual(self.locs["1000003"]["prov"]["190425"], (10000, 10000))   # fastest of duplicate rows
        self.assertEqual(set(self.locs["1000001"]["prov"]), {"190425", "130077"})
        self.assertEqual(self.locs["1000001"]["h3"], CELLS[GEOIDS[0]])
        self.assertFalse(self.locs["1000030"]["res"])
        self.assertTrue(self.locs["1000020"]["res"])
        self.assertEqual(set(self.brands), {"190425", "130077", "130079", "130982"})
        self.assertEqual(self.techs, {"10", "50", "61"})   # copper row counts as a location too
        # denominator: every location of any technology; block1 has 2 fiber + 1 satellite-only
        totals = build_map.totals_by_key(self.every, 0)
        self.assertEqual(totals[GEOIDS[1]], [3, 0])
        self.assertEqual(totals[GEOIDS[0]], [3, 0])
        self.assertEqual(totals[GEOIDS[3]], [0, 1])
        by_hex = build_map.totals_by_key(self.every, 2)
        self.assertEqual(sum(v[0] + v[1] for v in by_hex.values()), 9)

    def test_providers_and_combos(self):
        providers = build_map.build_providers(self.locs, self.brands)
        names = [p["name"] for p in providers]
        self.assertEqual(names[:2], ["AT&T", "Sonic"])            # ordered by residential locations (4 vs 3)
        self.assertEqual(providers[0]["locations"], 4)
        sonic = providers[1]
        self.assertEqual((sonic["locations"], sonic["blocks"], sonic["max_down"]), (3, 1, 10000))
        zayo = next(p for p in providers if p["id"] == "130982")
        self.assertEqual((zayo["locations"], zayo["locations_res"]), (1, 0))
        pidx = {p["id"]: i for i, p in enumerate(providers)}
        combos = build_map.combos_by_key(self.locs, pidx, "geoid")
        b0 = combos[GEOIDS[0]]
        both = tuple(sorted([(pidx["190425"], 10000, 10000), (pidx["130077"], 5000, 5000)]))
        self.assertEqual(b0[(1, both)], 2)
        self.assertEqual(b0[(1, ((pidx["190425"], 10000, 10000),))], 1)
        self.assertEqual(combos[GEOIDS[3]][(0, ((pidx["130982"], 10000, 10000),))], 1)
        by_hex = build_map.combos_by_key(self.locs, pidx, "h3")
        self.assertEqual(sum(sum(c.values()) for c in by_hex.values()), 7)

    def test_provider_id_filter(self):
        locs, brands, _every, _techs = build_map.load_locations([SAMPLE], SF, "50", ["190425"])
        self.assertEqual(set(brands), {"190425"})
        self.assertEqual(len(locs), 3)

    def test_county_filter(self):
        locs, *_ = build_map.load_locations([SAMPLE], ("06001",), "50")
        self.assertEqual(set(locs), {"1000050"})


class EndToEndTests(unittest.TestCase):
    def test_cli_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"; raw.mkdir()
            shutil.copy(SAMPLE, raw / "bdc_06_FibertothePremises_fixed_broadband_D25_test.csv")
            out = Path(tmp) / "site"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "build_map.py"), "--raw", str(raw),
                            "--blocks-dir", str(ROOT / "data" / "blocks"), "--county", "06075", "--region", "Test",
                            "--out", str(out)], check=True, capture_output=True)
            text = (out / "overview.js").read_text()
            self.assertTrue(text.startswith("window.FIBER_DATA = "))
            payload = json.loads(text[len("window.FIBER_DATA = "):].rstrip(";\n"))
            meta = payload["meta"]
            self.assertEqual(meta["as_of"], "December 2025")
            self.assertEqual((meta["locations"], meta["locations_res"], meta["blocks_with_fiber"]), (7, 6, 4))
            self.assertEqual((meta["denominator"], meta["locations_any_tech"]), ("all", 9))
            self.assertEqual([p["name"] for p in meta["providers"]][:2], ["AT&T", "Sonic"])
            self.assertEqual(meta["counties"], {"06075": "San Francisco"})
            self.assertEqual(meta["blocks_total"], 5018)   # blocks with housing (fixture adds no housing-free fiber blocks)
            hexes = {f["properties"]["id"]: f["properties"] for f in payload["hexes"]["features"]}
            self.assertIn(CELLS[GEOIDS[0]], hexes)
            # fixture fiber locations per block: 3, 2, 1, 1 (business), 0; blocks may share a hexagon
            per_block = {GEOIDS[0]: 3, GEOIDS[1]: 2, GEOIDS[2]: 1, GEOIDS[3]: 1, GEOIDS[4]: 0}
            expected = sum(n for g, n in per_block.items() if CELLS[g] == CELLS[GEOIDS[0]])
            self.assertEqual(sum(n for _r, _p, n in hexes[CELLS[GEOIDS[0]]]["c"]), expected)
            self.assertEqual(len(payload["hexes"]["features"][0]["geometry"]["coordinates"][0]), 7)
            hex7 = payload["hexes7"]["features"]
            self.assertEqual(meta["hexes7"], len(hex7))
            self.assertEqual(sum(n for f in hex7 for _r, _p, n in f["properties"]["c"]), 7)   # every fiber location once
            self.assertEqual(sum(f["properties"]["t"][0] + f["properties"]["t"][1] for f in hex7), 9)
            index = json.loads((out / "blocks" / "index.json").read_text())
            self.assertEqual((index["z"], index["blocks"]), (12, 5018))
            props = {}
            for t in index["tiles"]:
                for f in json.loads((out / "blocks" / f"12_{t}.json").read_text())["features"]:
                    props[f["properties"]["geoid"]] = f["properties"]
            self.assertEqual(len(props), 5018)
            self.assertEqual(sum(n for _res, _provs, n in props[GEOIDS[0]]["c"]), 3)
            self.assertEqual(props[GEOIDS[1]]["t"], [3, 0])
            self.assertEqual(props[GEOIDS[4]]["c"], [])                 # housing but no fiber rows
            self.assertEqual(props[GEOIDS[4]]["t"], [1, 0])          # the copper-only location


if __name__ == "__main__":
    unittest.main()
