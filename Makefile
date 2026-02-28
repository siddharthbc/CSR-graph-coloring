
# Targets:
#   make test         -- compare our Python output against C++ golden files
#   make golden       -- clone + build + run the official C++ repo to get golden outputs
#   make golden-clean -- nuke the cloned repo and golden outputs
#   make clean        -- remove all generated stuff

SHELL := /bin/bash

ROOT_DIR     := $(shell pwd)
TESTS_DIR    := $(ROOT_DIR)/tests
INPUTS_DIR   := $(TESTS_DIR)/inputs
GOLDEN_DIR   := $(TESTS_DIR)/golden
OFFICIAL_DIR := $(ROOT_DIR)/Picasso_golden

# official C++ repo
PICASSO_REPO := https://github.com/smferdous1/Picasso.git
PICASSO_BIN  := $(OFFICIAL_DIR)/build/apps/palcolEr

# coloring params (same defaults as the official README)
PALETTE_SIZE := 3
SEED         := 123

# auto-discover test inputs
TEST_INPUTS := $(wildcard $(INPUTS_DIR)/test*.json)
TEST_NAMES  := $(basename $(notdir $(TEST_INPUTS)))

PYTHON      := python3
PICASSO_CMD := $(PYTHON) -m picasso

.PHONY: golden golden-clone golden-build golden-run golden-clean clean test help

golden: golden-clone golden-build golden-run
	@echo ""
	@echo "Done! Golden files are in $(GOLDEN_DIR)"
	@ls -la $(GOLDEN_DIR)

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
	@cd $(OFFICIAL_DIR)/build && cmake .. 2>&1 | tail -5
	@cd $(OFFICIAL_DIR)/build && make -j$$(nproc) 2>&1 | tail -5
	@echo "  binary: $(PICASSO_BIN)"
	@test -f $(PICASSO_BIN) && echo "  build OK" || (echo "  BUILD FAILED" && exit 1)

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


# --- cleanup ---

golden-clean:
	@echo "Removing golden repo and outputs..."
	rm -rf $(OFFICIAL_DIR)
	rm -rf $(GOLDEN_DIR)

clean: golden-clean
	@echo "Removing generated artifacts..."
	rm -rf __pycache__ picasso/__pycache__
	rm -rf .venv

help:
	@echo "Targets:"
	@echo "  make test          run Python against golden outputs"
	@echo "  make golden        clone + build + run official C++ to generate golden files"
	@echo "  make golden-clone  clone the official repo"
	@echo "  make golden-build  build the official binary"
	@echo "  make golden-run    run tests and save golden outputs"
	@echo "  make golden-clean  remove cloned repo and golden files"
	@echo "  make clean         remove all generated stuff"
	@echo "  make help          show this"
