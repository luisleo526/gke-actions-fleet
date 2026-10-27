PYTHON ?= .venv/bin/python
TOFU ?= tofu
CONFIG ?= config.json

.PHONY: setup validate test charts render plan install
setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
validate:
	$(PYTHON) scripts/runnerctl.py --config $(CONFIG) validate
render:
	$(PYTHON) scripts/runnerctl.py --config $(CONFIG) render
test:
	.venv/bin/ruff check scripts tests
	.venv/bin/ruff format --check scripts tests
	$(PYTHON) -m unittest discover -s tests -v
	$(TOFU) -chdir=infra fmt -check -recursive
	$(TOFU) -chdir=infra init -backend=false -input=false -lockfile=readonly
	$(TOFU) -chdir=infra validate
	$(TOFU) -chdir=infra test
charts:
	$(PYTHON) scripts/verify_charts.py
plan: render
	$(TOFU) -chdir=infra init -input=false
	$(TOFU) -chdir=infra plan -var-file=../build/terraform.tfvars.json -out=runner.tfplan
install:
	$(PYTHON) scripts/runnerctl.py --config $(CONFIG) install
