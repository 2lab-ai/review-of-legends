# The gate: `make check` must pass before every commit (AGENTS.md).
PYTHON ?= python3

.PHONY: check test compile dashboard-test

check: compile test dashboard-test

compile:
	$(PYTHON) -m compileall -q code_review_service scripts

test:
	$(PYTHON) -m unittest discover -s tests

dashboard-test:
	@if command -v node >/dev/null 2>&1; then cd dashboard && npm test; \
	else echo "node not found: skipping dashboard tests (CI runs them)"; fi
