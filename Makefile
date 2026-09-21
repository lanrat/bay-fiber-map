PY ?= python3
PORT ?= 8000

.PHONY: all blocks fetch build buildings serve test clean

all: blocks fetch build   ## download blocks + FCC data, then build the map

blocks:                    ## census block boundaries for the nine Bay Area counties (public, no key)
	$(PY) scripts/fetch_blocks.py

fetch:                     ## Sonic's CA fiber availability file (needs FCC_USERNAME / FCC_HASH_VALUE, or --mode web)
	$(PY) scripts/fetch_fcc.py $(FETCH_ARGS)

build:                     ## join FCC rows to hexagons + blocks -> site/data/overview.js, site/data/blocks/
	$(PY) scripts/build_map.py $(BUILD_ARGS)

buildings:                 ## building footprints (DataSF + Overture Maps) -> site/data/buildings/ (needs shapely, overturemaps)
	$(PY) scripts/fetch_buildings.py

serve:                     ## open http://localhost:$(PORT)
	@echo "map: http://localhost:$(PORT)/"
	$(PY) -m http.server $(PORT) --directory site

test:
	$(PY) -m unittest discover -s tests -v

clean:                     ## remove the generated map data (it is committed; rebuild with make build)
	rm -rf site/data/overview.js site/data/blocks
