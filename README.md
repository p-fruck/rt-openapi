# RT REST2 OpenAPI Generator (Phase 2)

This repository builds a deterministic OpenAPI specification baseline for RT REST 2.0 from pinned RT source code and RT test evidence.

## What phase 2 produces

- Complete endpoint inventory: `out/endpoint-inventory.json`
- Deterministic test evidence map: `out/test-evidence.json`
- OpenAPI skeleton (methods + path params + base security):
  - `spec/openapi.json`
  - `spec/openapi.yaml`

## Deterministic inputs

Pinned RT source is stored in `vendor/rt` and locked in `rt-source.lock.json`.

Current lock:

- Repo: `https://github.com/bestpractical/rt.git`
- Ref: `stable`
- Commit: `530d776903e05293927600cca506f5336928cf28`

## Generate

```bash
make extract
```

## Probe local RT instance

This runs safe read-only probes (GET/HEAD) against generated operations and writes `out/probe-results.json`.

```bash
make probe
```

Optional auth examples:

```bash
python3 tools/probe_live_rt.py --workspace . --auth-mode token --token "1-14-..." --base-url "http://localhost"
python3 tools/probe_live_rt.py --workspace . --auth-mode basic --user test --password secret --base-url "http://localhost"
```

## Notes

- Routes are extracted from `lib/RT/REST2/Resource/*.pm` `dispatch_rules` regex declarations.
- Methods are extracted from `allowed_methods` when present, with deterministic fallbacks and test-based hints from `t/rest2/*.t`.
- RT tests are parsed to capture request method/path usage, asserted status codes, and JSON-body hints.
- The OpenAPI output is still schema-light by design, but now includes observed response status codes and operation-level test evidence metadata.
