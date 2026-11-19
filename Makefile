# Repository-wide tasks. Each service builds and tests on its own (services/<name>).

ENV ?= dev
PYTHON ?= python3

.PHONY: loadtest-double-sell
# T058 (SC-003): 1,000 simultaneous purchase attempts across 100 fresh items in sv-dev, then no item
# may have more than one paid order. Run on the lab host as bot, in a venv made from the seed
# service's pinned requirements (tests/load/double_sell.py reuses its browser code).
loadtest-double-sell:
	@test "$(ENV)" = dev || { echo "loadtest-double-sell runs only with ENV=dev"; exit 2; }
	$(PYTHON) tests/load/double_sell.py --env $(ENV)
