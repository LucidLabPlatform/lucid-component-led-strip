PYTHON ?= python3
VENV ?= .venv
PACKAGE = lucid_component_led_strip

.PHONY: help setup setup-venv dev test test-unit test-integration test-coverage build clean

help:
	@echo "lucid-component-led-strip"
	@echo "  make setup-venv      - Create .venv, install project + deps (run this first)"
	@echo "  make test            - Unit + integration tests"
	@echo "  make test-unit       - Unit tests only"
	@echo "  make test-coverage   - Tests with coverage report"
	@echo "  make build           - Build wheel and sdist"
	@echo "  make clean           - Remove build artifacts"

setup:
	@echo "Components run via agent-core; no .env needed here."

# On macOS, rpi-ws281x does not build (Linux-only). Install without deps then add
# everything except rpi-ws281x so we can run tests and build the wheel.
setup-venv:
	@test -d $(VENV) || ($(PYTHON) -m venv $(VENV) && echo "Created $(VENV).")
	@$(VENV)/bin/pip install -q -e . --no-deps
	@$(VENV)/bin/pip install -q "lucid-component-base @ git+https://github.com/LucidLabPlatform/lucid-component-base@v1.0.0" "numpy>=2.0.0" pytest build pytest-cov
	@echo "Ready. Run 'make test' or 'make build'."

dev:
	@echo "Component has no standalone runtime. Use 'make test' or 'make build'."

test: test-unit test-integration
	@echo "All tests passed."

test-unit:
	@$(VENV)/bin/python -m pytest tests/ -v -q

test-integration:
	@if [ -d tests/integration ]; then \
		$(VENV)/bin/python -m pytest tests/integration/ -v -q; \
	else \
		echo "No integration tests."; \
	fi

test-coverage:
	@$(VENV)/bin/python -m pytest tests/ --cov=src/$(PACKAGE) --cov-report=html --cov-report=term-missing -q

build:
	@test -d $(VENV) || (echo "Run 'make setup-venv' first." && exit 1)
	@$(VENV)/bin/python -m build

clean:
	@rm -rf build/ dist/ *.egg-info src/*.egg-info
	@rm -rf .pytest_cache .coverage htmlcov/
