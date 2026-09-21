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
GEOIDS = json.loads((FIX / "synthetic_geoids.json").read_text())["geoids"]
SAMPLE = FIX / "synthetic_fiber_sample.csv"


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


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.locs, self.brands = build_map.load_locations([SAMPLE], "06075", "50")

    def test_filters_and_dedup(self):
        # 3 homes in block0, 2 in block1, 1 in block2, 1 business in block3; tech 10 and Alameda rows dropped
        self.assertEqual(len(self.locs), 7)
        self.assertEqual(self.locs["1000003"]["prov"]["190425"], (10000, 10000))   # fastest of duplicate rows
        self.assertEqual(set(self.locs["1000001"]["prov"]), {"190425", "130077"})
        self.assertFalse(self.locs["1000030"]["res"])
        self.assertTrue(self.locs["1000020"]["res"])
        self.assertEqual(set(self.brands), {"190425", "130077", "130079", "130982"})

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
        combos = build_map.combos_by_block(self.locs, pidx)
        b0 = combos[GEOIDS[0]]
        both = tuple(sorted([(pidx["190425"], 10000, 10000), (pidx["130077"], 5000, 5000)]))
        self.assertEqual(b0[(1, both)], 2)
        self.assertEqual(b0[(1, ((pidx["190425"], 10000, 10000),))], 1)
        self.assertEqual(combos[GEOIDS[3]][(0, ((pidx["130982"], 10000, 10000),))], 1)

    def test_provider_id_filter(self):
        locs, brands = build_map.load_locations([SAMPLE], "06075", "50", ["190425"])
        self.assertEqual(set(brands), {"190425"})
        self.assertEqual(len(locs), 3)


class EndToEndTests(unittest.TestCase):
    def test_cli_writes_js(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"; raw.mkdir()
            shutil.copy(SAMPLE, raw / "bdc_06_FibertothePremises_fixed_broadband_D25_test.csv")
            out = Path(tmp) / "out.js"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "build_map.py"), "--raw", str(raw),
                            "--blocks", str(ROOT / "data" / "blocks_sf.geojson"), "--out", str(out)],
                           check=True, capture_output=True)
            text = out.read_text()
            self.assertTrue(text.startswith("window.FIBER_DATA = "))
            payload = json.loads(text[len("window.FIBER_DATA = "):].rstrip(";\n"))
            meta = payload["meta"]
            self.assertEqual(meta["as_of"], "December 2025")
            self.assertEqual((meta["locations"], meta["locations_res"], meta["blocks_with_fiber"]), (7, 6, 4))
            self.assertEqual([p["name"] for p in meta["providers"]][:2], ["AT&T", "Sonic"])
            props = {f["properties"]["geoid"]: f["properties"] for f in payload["blocks"]["features"]}
            self.assertEqual(sum(n for _res, _provs, n in props[GEOIDS[0]]["c"]), 3)
            self.assertEqual(props[GEOIDS[4]]["c"], [])                 # housing but no fiber rows
            self.assertGreater(props[GEOIDS[4]]["hu"], 0)
            self.assertNotIn("id", payload["blocks"]["features"][0])


if __name__ == "__main__":
    unittest.main()
