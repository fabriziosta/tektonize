.PHONY: help install install-dev venv activate lint format test clean convert report image

.DEFAULT_GOAL := help

INPUT_DIR ?= /path/to/jenkins-shared-library
IMAGE ?= tektonize:latest
VENV ?= .venv
PYTHON ?= python3
VENV_BIN := $(VENV)/bin
PIP := $(VENV_BIN)/pip
PY := $(VENV_BIN)/python
TEKTONIZE := $(VENV_BIN)/tektonize
# Absolute path so activation checks match $VIRTUAL_ENV
VENV_ABS := $(abspath $(VENV))

help:
	@echo "tektonize — Makefile targets"
	@echo ""
	@echo "Setup"
	@echo "  make venv          Create .venv (if missing) and upgrade pip"
	@echo "  make activate      Print 'source …/activate' for: eval \"\$$(make activate)\""
	@echo "  make install       Editable install into .venv"
	@echo "  make install-dev   Editable install with dev extras (pytest, ruff, …)"
	@echo ""
	@echo "Develop"
	@echo "  make lint          compileall + ruff + black --check + mypy"
	@echo "  make format        Auto-fix with ruff --fix and black"
	@echo "  make test          Run pytest -v tests/"
	@echo "  make clean         Remove output/, build caches (keeps .venv)"
	@echo ""
	@echo "Convert"
	@echo "  make convert       Convert a Jenkins shared library"
	@echo "                     INPUT_DIR=$(INPUT_DIR)"
	@echo "                     OUTPUT_DIR=./output/<UTC-timestamp>  (optional override)"
	@echo "                     IN_PLACE=1  write outputs into INPUT_DIR"
	@echo "  make report        Print conversion report for a prior run"
	@echo "                     Requires: OUTPUT_DIR=./output/<run>"
	@echo ""
	@echo "Container"
	@echo "  make image         Build container image (IMAGE=$(IMAGE))"
	@echo ""
	@echo "Examples"
	@echo "  make install-dev && eval \"\$$(make activate)\""
	@echo "  make convert INPUT_DIR=./tests/fixtures/sample_lib"
	@echo "  make convert INPUT_DIR=./lib IN_PLACE=1"
	@echo "  make report OUTPUT_DIR=./output/20261019T100933Z"

venv: $(VENV_BIN)/activate

$(VENV_BIN)/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

# Print a source line for the current shell: eval "$(make activate)"
activate: venv
	@echo 'source "$(VENV_ABS)/bin/activate"'

# Remind the *calling* shell to activate when it has not already.
define remind_activate
	@if [ "$$VIRTUAL_ENV" = "$(VENV_ABS)" ]; then \
		echo "venv already active: $$VIRTUAL_ENV"; \
	else \
		echo ""; \
		echo "venv is not active in this shell. Activate with:"; \
		echo "  eval \"\$$(make activate)\""; \
		echo "  # or: source $(VENV)/bin/activate"; \
	fi
endef

install: venv
	. $(VENV_BIN)/activate && pip install -e .
	. $(VENV_BIN)/activate && tektonize --help
	$(remind_activate)

install-dev: venv
	. $(VENV_BIN)/activate && pip install -e ".[dev]"
	. $(VENV_BIN)/activate && tektonize --help
	$(remind_activate)

lint: venv
	. $(VENV_BIN)/activate && python -m compileall -q src/tektonize
	. $(VENV_BIN)/activate && ruff check src/ tests/
	. $(VENV_BIN)/activate && black --check src/ tests/
	. $(VENV_BIN)/activate && mypy

format: venv
	. $(VENV_BIN)/activate && ruff check --fix src/ tests/
	. $(VENV_BIN)/activate && black src/ tests/

test: venv
	. $(VENV_BIN)/activate && pytest -v tests/

clean:
	@echo "Cleaning tektonize build artifacts and caches..."
	@echo "  removing: output/  build/  dist/  *.egg-info"
	@echo "  removing: .pytest_cache  .mypy_cache  .ruff_cache  __pycache__/"
	@rm -rf output/ build/ dist/ *.egg-info .pytest_cache __pycache__ .mypy_cache .ruff_cache
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "Done. Kept: $(VENV)/ (virtualenv) and source tree."

# Optional: OUTPUT_DIR=./output/custom  (default: ./output/<UTC-timestamp>)
# Optional: IN_PLACE=1  write into INPUT_DIR (overrides OUTPUT_DIR)
OUTPUT_DIR ?=
IN_PLACE ?=

convert: venv
	. $(VENV_BIN)/activate && tektonize convert \
		--input-dir $(INPUT_DIR) \
		$(if $(filter 1 true TRUE yes YES,$(IN_PLACE)),--in-place,$(if $(OUTPUT_DIR),--output-dir $(OUTPUT_DIR),))

# Required: OUTPUT_DIR=./output/<run>  (shows report paths when failures > 0)
report: venv
	@if [ -z "$(OUTPUT_DIR)" ]; then \
		echo "Usage: make report OUTPUT_DIR=./output/<run>"; \
		exit 1; \
	fi
	. $(VENV_BIN)/activate && tektonize report --output-dir $(OUTPUT_DIR)

image:
	podman build -t $(IMAGE) -f Containerfile .
