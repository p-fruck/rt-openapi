# RT REST2 OpenAPI Generator (Phase 1)

This repository builds a deterministic OpenAPI skeleton for RT REST 2.0 from pinned RT source code.

## What phase 1 produces

- Complete endpoint inventory: `out/endpoint-inventory.json`
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

## Notes

- Routes are extracted from `lib/RT/REST2/Resource/*.pm` `dispatch_rules` regex declarations.
- Methods are extracted from `allowed_methods` when present, with deterministic fallbacks and test-based hints from `t/rest2/*.t`.
- This is intentionally a path/method skeleton. Schema fidelity is added in phase 2/3.
