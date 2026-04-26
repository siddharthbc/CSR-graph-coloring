
# Targets:
#   make test              -- Python vs golden (default config: t=3, α=1)
#   make test-normal       -- Python vs golden (paper Normal: P=12.5%|V|, α=2)
#   make test-csr          -- GPU CSR binary vs golden (default config)
#   make test-csl          -- Cerebras simulator vs golden (default config)
#   make test-csl-normal   -- Cerebras simulator vs golden_normal (Normal config)
#   make test-all          -- run all test suites
#   make golden            -- clone + build + run C++ for all configs
#   make golden-clean      -- nuke the cloned repo and golden outputs
#   make clean             -- remove all generated stuff

SHELL := /bin/bash

ROOT_DIR     := $(shell pwd)
TESTS_DIR    := $(ROOT_DIR)/tests
INPUTS_DIR   := $(TESTS_DIR)/inputs
GOLDEN_DIR   := $(TESTS_DIR)/golden
OFFICIAL_DIR := $(ROOT_DIR)/Picasso_golden

# official C++ repo
PICASSO_REPO := https://github.com/smferdous1/Picasso.git
PICASSO_BIN  := $(OFFICIAL_DIR)/build/apps/palcolEr
# GPU CSR binary (built only when CUDA is available)
PICASSO_BIN_CSR := $(OFFICIAL_DIR)/build/apps/palcolGr

# ---------- Parameter Configurations (from Picasso IPDPS'24 paper) ----------
# Default: fixed palette size (good for small test graphs)
PALETTE_SIZE := 3
ALPHA        := 1.0
SEED         := 123

# Paper Normal config: P = max(1, floor(12.5% of |V|)), α=2
# (best memory/speed tradeoff per Picasso IPDPS'24 paper)
# --inv = P (palette size) per test — recurse while invalid > P,
# then fall back to greedy.  P = max(1, floor(frac * n)).
PALETTE_NORMAL_FRAC := 0.125
ALPHA_NORMAL        := 2.0
GOLDEN_NORMAL_DIR   := $(TESTS_DIR)/golden_normal

# auto-discover test inputs (all JSON files in inputs/)
TEST_INPUTS := $(wildcard $(INPUTS_DIR)/*.json)
TEST_NAMES  := $(basename $(notdir $(TEST_INPUTS)))

PYTHON      := python3
PICASSO_CMD := $(PYTHON) -m picasso

# Cerebras CSL params
CSL_NUM_PES    := 8
CSL_GRID_ROWS  := 1
CSL_TEST_CMD   := $(PYTHON) $(ROOT_DIR)/picasso/run_csl_tests.py
CSL_RUN_SCOPE  ?= local
CSL_RUN_ID     ?= make-test-csl
CSL_RUN_DIR    := $(ROOT_DIR)/runs/$(CSL_RUN_SCOPE)/$(CSL_RUN_ID)
CSL_NORMAL_RUN_ID ?= make-test-csl-normal
CSL_NORMAL_RUN_DIR := $(ROOT_DIR)/runs/$(CSL_RUN_SCOPE)/$(CSL_NORMAL_RUN_ID)

.PHONY: golden golden-clone golden-build golden-run golden-run-normal \
       golden-clean clean test test-normal test-csr test-csl test-csl-normal test-all help

golden: golden-clone golden-build golden-run golden-run-normal
	@echo ""
	@echo "Done! Golden files generated for all configs."

golden-clone:
	@echo ""
	@echo "Cloning official Picasso repo"
	@if [ -d "$(OFFICIAL_DIR)" ]; then \
		echo "  already exists, skipping"; \
	else \
		git clone $(PICASSO_REPO) $(OFFICIAL_DIR); \
	fi

golden-build: golden-clone
	@echo ""
	@echo "Building official binary."
	@mkdir -p $(OFFICIAL_DIR)/build
	@cd $(OFFICIAL_DIR)/build && \
		CMAKE_EXTRA=""; \
		if command -v nvcc >/dev/null 2>&1; then \
			echo "  CUDA detected — building GPU targets too"; \
			CMAKE_EXTRA="-DCMAKE_CUDA_TOOLKIT_INCLUDE_DIRECTORIES=/usr/include"; \
		fi; \
		cmake .. $$CMAKE_EXTRA 2>&1 | tail -5
	@cd $(OFFICIAL_DIR)/build && make -j$$(nproc) 2>&1 | tail -5
	@echo "  binary: $(PICASSO_BIN)"
	@test -f $(PICASSO_BIN) && echo "  build OK" || (echo "  BUILD FAILED" && exit 1)
	@if [ -f $(PICASSO_BIN_CSR) ]; then echo "  CSR binary: $(PICASSO_BIN_CSR) OK"; fi

golden-run: golden-build
	@echo ""
	@echo "Running C++ binary on all test inputs"
	@mkdir -p $(GOLDEN_DIR)
	@for input in $(TEST_INPUTS); do \
		name=$$(basename "$$input" .json); \
		echo ""; \
		echo "--- $$name ---"; \
		$(PICASSO_BIN) \
			-t $(PALETTE_SIZE) \
			-a $(ALPHA) \
			-r \
			--sd $(SEED) \
			--in "$$input" \
			2>&1 | tee "$(GOLDEN_DIR)/$${name}_golden.txt"; \
		echo "$$?" > "$(GOLDEN_DIR)/$${name}_exitcode.txt"; \
	done
	@echo ""
	@echo "Golden files saved to $(GOLDEN_DIR)"
	@echo ""
	@echo "  Test                            Nodes  Edges  Colors"
	@echo "  ---------------------------------------------------"
	@for input in $(TEST_INPUTS); do \
		name=$$(basename "$$input" .json); \
		log="$(GOLDEN_DIR)/$${name}_golden.txt"; \
		nodes=$$(grep "Num Nodes:" "$$log" | head -1 | awk '{print $$NF}'); \
		edges=$$(grep "Num Edges:" "$$log" | head -1 | awk '{print $$NF}'); \
		colors=$$(grep "# of Final colors:" "$$log" | awk '{print $$NF}'); \
		printf "  %-35s %5s %6s %6s\n" "$$name" "$$nodes" "$$edges" "$$colors"; \
	done


test:
	@echo ""
	@echo "Running Python tests against golden outputs..."
	@echo ""
	@pass=0; fail=0; total=0; \
	for input in $(TEST_INPUTS); do \
		name=$$(basename "$$input" .json); \
		golden="$(GOLDEN_DIR)/$${name}_golden.txt"; \
		total=$$((total + 1)); \
		echo "--- $$name ---"; \
		if [ ! -f "$$golden" ]; then \
			echo "  SKIP (no golden file)"; \
			continue; \
		fi; \
		our=$$(cd $(ROOT_DIR) && $(PICASSO_CMD) \
			-t $(PALETTE_SIZE) \
			-a $(ALPHA) \
			-r \
			--sd $(SEED) \
			--in "$$input" 2>/dev/null); \
		our_nodes=$$(echo "$$our" | grep "Num Nodes:" | head -1 | awk '{print $$NF}'); \
		our_edges=$$(echo "$$our" | grep "Num Edges:" | head -1 | awk '{print $$NF}'); \
		our_conf=$$(echo "$$our" | grep "Num Conflict Edges:" | head -1 | awk '{print $$NF}'); \
		our_colors=$$(echo "$$our" | grep "# of Final colors:" | awk '{print $$NF}'); \
		ref_nodes=$$(grep "Num Nodes:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_edges=$$(grep "Num Edges:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_conf=$$(grep "Num Conflict Edges:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_colors=$$(grep "# of Final colors:" "$$golden" | awk '{print $$NF}'); \
		ok=true; \
		if [ "$$our_nodes" != "$$ref_nodes" ]; then \
			echo "  FAIL: Num Nodes: ours=$$our_nodes ref=$$ref_nodes"; \
			ok=false; \
		fi; \
		if [ "$$our_edges" != "$$ref_edges" ]; then \
			echo "  FAIL: Num Edges: ours=$$our_edges ref=$$ref_edges"; \
			ok=false; \
		fi; \
		if [ "$$our_conf" != "$$ref_conf" ]; then \
			echo "  WARN: Conflicts differ: ours=$$our_conf ref=$$ref_conf (RNG-dependent)"; \
		fi; \
		if [ "$$our_colors" != "$$ref_colors" ]; then \
			echo "  FAIL: Colors: ours=$$our_colors ref=$$ref_colors"; \
			ok=false; \
		fi; \
		if [ "$$ok" = true ]; then \
			echo "  PASS (Nodes=$$our_nodes Edges=$$our_edges Conflicts=$$our_conf Colors=$$our_colors)"; \
			pass=$$((pass + 1)); \
		else \
			fail=$$((fail + 1)); \
		fi; \
	done; \
	echo ""; \
	echo "Results: $$pass/$$total passed, $$fail failed"; \
	if [ "$$fail" -gt 0 ]; then exit 1; fi


test-csr:
	@echo ""
	@echo "Comparing GPU CSR (palcolGr) against adj-list golden (palcolEr)..."
	@echo ""
	@if [ ! -f $(PICASSO_BIN_CSR) ]; then \
		echo "  SKIP: $(PICASSO_BIN_CSR) not found (needs CUDA build)"; \
		exit 0; \
	fi; \
	pass=0; fail=0; total=0; \
	for input in $(TEST_INPUTS); do \
		name=$$(basename "$$input" .json); \
		golden="$(GOLDEN_DIR)/$${name}_golden.txt"; \
		total=$$((total + 1)); \
		echo "--- $$name ---"; \
		if [ ! -f "$$golden" ]; then \
			echo "  SKIP (no golden file)"; \
			continue; \
		fi; \
		csr_out=$$($(PICASSO_BIN_CSR) \
			-t $(PALETTE_SIZE) \
			-a $(ALPHA) \
			-r \
			--sd $(SEED) \
			--in "$$input" 2>&1); \
		csr_nodes=$$(echo "$$csr_out" | grep "Num Nodes:" | head -1 | awk '{print $$NF}'); \
		csr_conf=$$(echo "$$csr_out" | grep "Num Conflict Edges:" | head -1 | awk '{print $$NF}'); \
		csr_colors=$$(echo "$$csr_out" | grep "# of Final colors:" | awk '{print $$NF}'); \
		ref_nodes=$$(grep "Num Nodes:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_conf=$$(grep "Num Conflict Edges:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_colors=$$(grep "# of Final colors:" "$$golden" | awk '{print $$NF}'); \
		ok=true; \
		if [ "$$csr_nodes" != "$$ref_nodes" ]; then \
			echo "  FAIL: Num Nodes: csr=$$csr_nodes ref=$$ref_nodes"; \
			ok=false; \
		fi; \
		if [ "$$csr_conf" != "$$ref_conf" ]; then \
			echo "  FAIL: Conflicts: csr=$$csr_conf ref=$$ref_conf"; \
			ok=false; \
		fi; \
		if [ "$$csr_colors" != "$$ref_colors" ]; then \
			echo "  FAIL: Colors: csr=$$csr_colors ref=$$ref_colors"; \
			ok=false; \
		fi; \
		if [ "$$ok" = true ]; then \
			echo "  PASS (Nodes=$$csr_nodes Conflicts=$$csr_conf Colors=$$csr_colors)"; \
			pass=$$((pass + 1)); \
		else \
			fail=$$((fail + 1)); \
		fi; \
	done; \
	echo ""; \
	echo "CSR Results: $$pass/$$total passed, $$fail failed"; \
	if [ "$$fail" -gt 0 ]; then exit 1; fi


# ---------- Paper Normal config (P=12.5%, α=2) ----------

golden-run-normal: golden-build
	@echo ""
	@if [ ! -f $(PICASSO_BIN_CSR) ]; then \
		echo "ERROR: GPU CSR binary $(PICASSO_BIN_CSR) not found (needs CUDA build)"; exit 1; \
	fi
	@echo "Generating golden files for Normal config (GPU CSR, P=max(1,⌊$(PALETTE_NORMAL_FRAC)·n⌋), α=$(ALPHA_NORMAL), inv=P)..."
	@mkdir -p $(GOLDEN_NORMAL_DIR)
	@for input in $(TEST_INPUTS); do \
		name=$$(basename "$$input" .json); \
		n=$$($(PYTHON) -c "import json; print(len(json.load(open('$$input'))))"); \
		p=$$($(PYTHON) -c "import math; print(max(1, math.floor($(PALETTE_NORMAL_FRAC) * $$n)))"); \
		echo "--- $$name (n=$$n, P=$$p) ---"; \
		$(PICASSO_BIN_CSR) \
			-t $$p -a $(ALPHA_NORMAL) -r --sd $(SEED) --inv $$p \
			--in "$$input" \
			2>&1 | tee "$(GOLDEN_NORMAL_DIR)/$${name}_golden.txt"; \
		echo "$$?" > "$(GOLDEN_NORMAL_DIR)/$${name}_exitcode.txt"; \
	done
	@echo ""
	@echo "Normal golden files saved to $(GOLDEN_NORMAL_DIR)"

test-normal:
	@echo ""
	@echo "Running Python tests — Normal config (P=max(1,⌊$(PALETTE_NORMAL_FRAC)·n⌋), α=$(ALPHA_NORMAL))..."
	@echo ""
	@pass=0; fail=0; total=0; \
	for input in $(TEST_INPUTS); do \
		name=$$(basename "$$input" .json); \
		golden="$(GOLDEN_NORMAL_DIR)/$${name}_golden.txt"; \
		total=$$((total + 1)); \
		if [ ! -f "$$golden" ]; then \
			echo "--- $$name --- SKIP (no golden)"; \
			continue; \
		fi; \
		n=$$($(PYTHON) -c "import json; print(len(json.load(open('$$input'))))"); \
		p=$$($(PYTHON) -c "import math; print(max(1, math.floor($(PALETTE_NORMAL_FRAC) * $$n)))"); \
		echo "--- $$name (n=$$n, P=$$p) ---"; \
		our=$$(cd $(ROOT_DIR) && $(PICASSO_CMD) \
			-t $$p -a $(ALPHA_NORMAL) -r --sd $(SEED) --inv $$p \
			--in "$$input" 2>/dev/null); \
		our_nodes=$$(echo "$$our" | grep "Num Nodes:" | head -1 | awk '{print $$NF}'); \
		our_conf=$$(echo "$$our" | grep "Num Conflict Edges:" | head -1 | awk '{print $$NF}'); \
		our_colors=$$(echo "$$our" | grep "# of Final colors:" | awk '{print $$NF}'); \
		ref_nodes=$$(grep "Num Nodes:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_conf=$$(grep "Num Conflict Edges:" "$$golden" | head -1 | awk '{print $$NF}'); \
		ref_colors=$$(grep "# of Final colors:" "$$golden" | awk '{print $$NF}'); \
		ok=true; \
		if [ "$$our_nodes" != "$$ref_nodes" ]; then \
			echo "  FAIL: Num Nodes: ours=$$our_nodes ref=$$ref_nodes"; ok=false; \
		fi; \
		if [ "$$our_conf" != "$$ref_conf" ]; then \
			echo "  WARN: Conflicts differ: ours=$$our_conf ref=$$ref_conf"; \
		fi; \
		if [ -n "$$our_colors" ] && [ -n "$$ref_colors" ] && [ "$$our_colors" -gt "$$ref_colors" ]; then \
			echo "  FAIL: Colors: ours=$$our_colors ref=$$ref_colors (worse)"; ok=false; \
		elif [ -n "$$our_colors" ] && [ -n "$$ref_colors" ] && [ "$$our_colors" -lt "$$ref_colors" ]; then \
			echo "  NOTE: ours=$$our_colors < ref=$$ref_colors (better)"; \
		elif [ "$$our_colors" != "$$ref_colors" ]; then \
			echo "  FAIL: Colors: ours=$$our_colors ref=$$ref_colors"; ok=false; \
		fi; \
		if [ "$$ok" = true ]; then \
			echo "  PASS (Nodes=$$our_nodes Conflicts=$$our_conf Colors=$$our_colors)"; \
			pass=$$((pass + 1)); \
		else \
			fail=$$((fail + 1)); \
		fi; \
	done; \
	echo ""; \
	echo "Normal Results: $$pass passed, $$fail failed (of $$total)"; \
	if [ "$$fail" -gt 0 ]; then exit 1; fi


test-csl:
	@echo ""
	@echo "Running Cerebras CSL speculative parallel coloring on simulator against golden..."
	@echo ""
	@mkdir -p $(CSL_RUN_DIR)
	@$(CSL_TEST_CMD) --num-pes $(CSL_NUM_PES) --grid-rows $(CSL_GRID_ROWS) \
		--output-dir $(CSL_RUN_DIR)/results | tee $(CSL_RUN_DIR)/stdout.log

test-csl-normal:
	@echo ""
	@echo "Running Cerebras CSL on simulator against golden_normal (P=12.5%%|V|, α=2, inv=P)..."
	@echo ""
	@mkdir -p $(CSL_NORMAL_RUN_DIR)
	@$(CSL_TEST_CMD) --num-pes $(CSL_NUM_PES) --grid-rows $(CSL_GRID_ROWS) \
		--golden-dir $(GOLDEN_NORMAL_DIR) \
		--palette-frac $(PALETTE_NORMAL_FRAC) --alpha $(ALPHA_NORMAL) \
		--output-dir $(CSL_NORMAL_RUN_DIR)/results | tee $(CSL_NORMAL_RUN_DIR)/stdout.log

test-all: test test-csr test-normal test-csl test-csl-normal
	@echo ""
	@echo "All test suites completed."

# --- cleanup ---

golden-clean:
	@echo "Removing golden repo and outputs..."
	rm -rf $(OFFICIAL_DIR)
	rm -rf $(GOLDEN_DIR)
	rm -rf $(GOLDEN_NORMAL_DIR)

clean: golden-clean
	@echo "Removing generated artifacts..."
	rm -rf __pycache__ picasso/__pycache__
	rm -rf csl_compiled_out/bin csl_compiled_out/*.json csl_compiled_out/*.log
	rm -rf .venv

help:
	@echo "Targets:"
	@echo "  make test               run Python against golden outputs (default config)"
	@echo "  make test-normal        run Python against golden_normal (P=12.5%%|V|, α=2, inv=P)"
	@echo "  make test-csl           run Cerebras simulator against golden (default config)"
	@echo "  make test-csl-normal    run Cerebras simulator against golden_normal"
	@echo "  make test-all           run all test suites"
	@echo "  make golden             clone + build + run official C++ to generate golden files"
	@echo "  make golden-clone       clone the official repo"
	@echo "  make golden-build       build the official binary"
	@echo "  make golden-run         run tests and save golden outputs"
	@echo "  make golden-clean       remove cloned repo and golden files"
	@echo "  make clean              remove all generated stuff"
	@echo "  make help               show this"
	@echo ""
	@echo "CSL options (override on command line):"
	@echo "  CSL_NUM_PES=2      number of PEs for CSL simulation"
	@echo "  CSL_RUN_ID=foo     run directory name under runs/local/"
