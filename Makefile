PYTHON ?= .venv/bin/python

.PHONY: install check lint test audit lock clean

install:            ## Create the virtualenv and install the hashed dev tools (uv if installed, else pip)
	if command -v uv >/dev/null; then \
		uv venv --seed --allow-existing --python python3.14 .venv && uv pip install --python $(PYTHON) --require-hashes -r requirements-dev.txt && uv pip check --python $(PYTHON); \
	else \
		python3.14 -m venv .venv && $(PYTHON) -m pip install --upgrade pip && $(PYTHON) -m pip install --require-hashes -r requirements-dev.txt && $(PYTHON) -m pip check; \
	fi

check:              ## Everything CI checks, locally
	$(MAKE) lint test audit

lint:               ## ruff (with Bandit security rules) and cfn-lint on the template
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	.venv/bin/cfn-lint template.yaml --ignore-checks W3002  # W3002: CodeUri is a local path until `aws cloudformation package`

test:
	$(PYTHON) -m pytest

audit:              ## Check dependencies for known vulnerabilities
	$(PYTHON) -m pip_audit --require-hashes --disable-pip -r requirements-dev.txt
	$(PYTHON) -m pip_audit --require-hashes --disable-pip -r requirements.txt  # what ships

lock:               ## Regenerate the hashed lockfiles (Lambda runtime, dev tools) from the .in files
	for d in requirements requirements-dev; do $(PYTHON) -m piptools compile --quiet --generate-hashes --allow-unsafe \
		--strip-extras --no-emit-index-url --output-file=$$d.txt $$d.in; done

clean:
	rm -rf build .pytest_cache .ruff_cache .coverage
