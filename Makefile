.PHONY: test upstream build bundle

# Our integration tests are offline. Upstream source never lives in Git here.
test:
	python3 -m unittest discover -s packaging -p 'test_*.py'
	python3 -m unittest discover -s scripts -p 'test_*.py'
	for script in install.sh packaging/start.sh packaging/supervise.sh packaging/rollback-bind.sh packaging/S98quasarbridge packaging/install-device.sh packaging/recovery.sh; do sh -n "$$script" || exit 1; done

# One stable release lookup, exact official Git checkout, patch check and full
# Go race/vet/config/routes. Refuse an existing directory instead of overlaying it.
upstream:
	python3 scripts/upstream.py bridge

build: upstream
	python3 scripts/upstream.py build

# Requires the checked source from `make build` (or `make test build bundle`).
# An allowlisted fresh bundle never contains local keys/config/old data.
bundle:
	test ! -e dist || { printf '%s\n' 'dist exists; inspect and move it before building a clean bundle' >&2; exit 1; }
	python3 scripts/upstream.py bundle
