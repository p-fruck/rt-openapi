PYTHON ?= python3
WORKSPACE := $(CURDIR)

.PHONY: extract verify clean

extract:
	$(PYTHON) tools/extract_routes.py --workspace $(WORKSPACE)

verify: extract
	@python3 -m json.tool out/endpoint-inventory.json >/dev/null
	@python3 -m json.tool spec/openapi.json >/dev/null
	@echo "Artifacts generated and JSON is valid."

clean:
	rm -f out/endpoint-inventory.json spec/openapi.json spec/openapi.yaml
