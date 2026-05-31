PYTHON ?= python3
WORKSPACE := $(CURDIR)

.PHONY: extract probe verify clean

extract:
	$(PYTHON) tools/extract_routes.py --workspace $(WORKSPACE)

probe:
	$(PYTHON) tools/probe_live_rt.py --workspace $(WORKSPACE)

verify: extract
	@python3 -m json.tool out/endpoint-inventory.json >/dev/null
	@python3 -m json.tool out/test-evidence.json >/dev/null
	@python3 -m json.tool spec/openapi.json >/dev/null
	@echo "Artifacts generated and JSON is valid."

clean:
	rm -f out/endpoint-inventory.json out/test-evidence.json spec/openapi.json spec/openapi.yaml
