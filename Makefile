PYTHON ?= python3
WORKSPACE := $(CURDIR)

.PHONY: extract probe analyze snapshot delta phase3 verify clean

extract:
	$(PYTHON) tools/extract_routes.py --workspace $(WORKSPACE)

probe:
	$(PYTHON) tools/probe_live_rt.py --workspace $(WORKSPACE)

analyze:
	$(PYTHON) tools/analyze_probe_results.py --workspace $(WORKSPACE)

snapshot:
	$(PYTHON) tools/analyze_probe_results.py --workspace $(WORKSPACE) --snapshot

delta:
	$(PYTHON) tools/analyze_probe_results.py --workspace $(WORKSPACE) --delta

phase3: extract probe analyze
	@echo "Phase 3 artifacts refreshed (inventory, spec, probe, analysis)."

verify: extract
	@python3 -m json.tool out/endpoint-inventory.json >/dev/null
	@python3 -m json.tool out/test-evidence.json >/dev/null
	@python3 -m json.tool out/runtime-overrides.json >/dev/null
	@python3 -m json.tool spec/openapi.json >/dev/null
	@python3 -m json.tool out/probe-results.json >/dev/null || true
	@python3 -m json.tool out/probe-analysis.json >/dev/null || true
	@python3 -m json.tool out/probe-analysis.baseline.json >/dev/null || true
	@python3 -m json.tool out/probe-delta.json >/dev/null || true
	@echo "Artifacts generated and JSON is valid."

clean:
	rm -f out/endpoint-inventory.json out/test-evidence.json out/runtime-overrides.json out/probe-analysis.baseline.json out/probe-delta.json out/probe-delta.md spec/openapi.json spec/openapi.yaml
