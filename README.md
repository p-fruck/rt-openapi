# RT REST2 OpenAPI Generator (Phase 2)

This repository builds a deterministic OpenAPI specification baseline for RT REST 2.0 from pinned RT source code and RT test evidence.

## What phase 2 produces

- Complete endpoint inventory: `out/endpoint-inventory.json`
- Deterministic test evidence map: `out/test-evidence.json`
- Runtime override map from probe analysis: `out/runtime-overrides.json`
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

By default, the probe script auto-loads `.env` from the workspace root and auto-selects auth mode from available credentials (`RT_TOKEN`, `RT_USERNAME` + `RT_PASSWORD`, or `RT_COOKIE`). It uses the configured base URL and falls back to `RT_SERVER` when needed.

Optional auth examples:

```bash
python3 tools/probe_live_rt.py --workspace . --auth-mode token --token "1-14-..." --base-url "http://localhost"
python3 tools/probe_live_rt.py --workspace . --auth-mode basic --user test --password secret --base-url "http://localhost"
```

## Analyze probe results

Compare runtime probe observations to generated OpenAPI responses and produce deterministic reports:

```bash
make analyze
```

Outputs:

- `out/probe-analysis.json`
- `out/coverage-report.md`

Create or refresh baseline snapshot:

```bash
make snapshot
```

Generate delta from current analysis vs baseline:

```bash
make delta
```

Delta outputs:

- `out/probe-delta.json`
- `out/probe-delta.md`
- Baseline file: `out/probe-analysis.baseline.json`

One-shot refresh for generation + probing + analysis:

```bash
make phase3
```

## Notes

- Routes are extracted from `lib/RT/REST2/Resource/*.pm` `dispatch_rules` regex declarations.
- Methods are extracted from `allowed_methods` when present, with deterministic fallbacks and test-based hints from `t/rest2/*.t`.
- RT tests are parsed to capture request method/path usage, asserted status codes, and JSON-body hints.
- Runtime probe analysis is consumed during extraction to add explicit observed status codes and prune `HEAD` operations that consistently return `405`.
- The OpenAPI output is still schema-light by design, but now includes test evidence metadata plus runtime evidence metadata.
- Query parameters are source-derived from resource classes and emitted as reusable OpenAPI component parameters.
- Collection and record GET responses now reference reusable schemas (`CollectionResponse`, `RecordObject`).
- Request/response media types are inferred from resource `content_types_accepted`/`content_types_provided` callbacks and included per operation.
