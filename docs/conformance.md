# Conformance

The deterministic verifier is:

```console
python scripts/verify_contracts.py
```

It validates the JSON Schema contract instance, exact released dependency
pins, locked revision evidence, plugin manifest, schema/resource bundle,
model/schema golden fingerprints, one-project/one-package layout, PEP 420
namespace ownership, and the absence of Adapter, Engine, or database-client
imports.

## Acceptance matrix

| Requirement | Evidence |
| --- | --- |
| Meter version and exact conversion | `tests/test_models.py` |
| Batch replay/divergent conflict and atomicity profiles | `tests/test_repository.py` |
| Correction chains and half-open intervals | `tests/test_models.py`, `tests/test_repository.py` |
| Decimal determinism | `tests/test_models.py`, golden fingerprints |
| Scope/pagination/physical isolation | `tests/test_repository.py`, ClickHouse compile integration |
| Crash recovery/checkpoint races/late events | `tests/test_aggregation.py` |
| OTel and Evidence correlation | `tests/test_models.py`, `tests/test_released_integration.py` |
| Cost consumer compatibility without pricing | `tests/test_released_integration.py` |
| Released Meridian and ClickHouse contracts | `tests/test_schema_plugin.py`, `tests/test_released_integration.py` |
| Stable error envelopes | `tests/test_conformance.py` |
| Package/license/reproducibility | `scripts/verify_artifacts.py`, `scripts/compare_artifacts.py` |

CI runs Ruff, strict mypy, the contract verifier, all tests with branch
coverage, Bandit, pip-audit, two reproducible builds, Twine validation, and
archive-boundary inspection.
