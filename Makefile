PY ?= python3
PORT ?= 8000

.PHONY: all blocks fetch build serve test clean

all: blocks fetch build   ## download blocks + FCC data, then build the map

blocks:                    ## census block boundaries for San Francisco (public, no key)
	$(PY) scripts/fetch_blocks.py

fetch:                     ## Sonic's CA fiber availability file (needs FCC_USERNAME / FCC_HASH_VALUE, or --mode web)
	$(PY) scripts/fetch_fcc.py $(FETCH_ARGS)

build:                     ## join FCC rows to blocks -> site/data/fiber_blocks.js
	$(PY) scripts/build_map.py $(BUILD_ARGS)

serve:                     ## open http://localhost:$(PORT)
	@echo "map: http://localhost:$(PORT)/"
	$(PY) -m http.server $(PORT) --directory site

test:
	$(PY) -m unittest discover -s tests -v

clean:                     ## remove the generated map data (it is committed; rebuild with make build)
	rm -f site/data/fiber_blocks.js
