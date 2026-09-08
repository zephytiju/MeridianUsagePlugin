# Changelog

## 2.0.2 — 2026-09-08

- Replace historical exact Meridian runtime dependencies with justified public API
  compatibility bounds and an independent, complete public artifact hash lock.
- Validate Core 1.1.0, Semantics 2.0.1, Query 1.0.3, Evidence 1.0.2,
  Observability 1.0.3, PostgreSQL 2.2.0 and ClickHouse 1.1.1 without overrides.
- Require real Usage/Evidence atomic commit, rollback and restart replay plus
  ClickHouse receipt retry, paging, isolation and retained capability failures.
- Preserve record/Schema fingerprints and structured write semantics. Only the
  package-version-bearing bundle fingerprint changes; informational compatibility
  metadata v2 separates package ranges from API contract identifiers.


## 2.0.1

- Lower Usage query operators to released Query mapping predicates, preserving
  scoped half-open windows, operand semantics, ordering, projection, and pagination.
- Correct Event/Aggregate Resource profiles to their existing Schema-declared
  `time-series` profile. All Schema definitions and data fingerprints are unchanged.
- Verify default provider discovery and public repositories on real PostgreSQL.

## 2.0.0

- Consume structured put 2.0.0 with explicit immutable creation; remove storage
  version-zero creation. Use conditional patch and returned record versions for
  existing checkpoint/claim transitions, with distinct internal request keys.
- Publish state schemas 2.0.0 with only state fields mutable; retain original
  1.0.0 schema definitions and immutable identity/scope. Deployment activation
  updates pins/layouts without recreating or resetting state.
- Preserve the 1.0.3 event decimal repair and v1 fingerprint bytes. Reuse bounded
  equivalent-text recovery for aggregate totals, requiring full fingerprint
  authentication after numeric storage.
- Require the compatible released package set, including PostgreSQL 2.1.1 for
  real-engine tests and timestamp-preserving readback.

All notable changes follow Semantic Versioning.

## 1.0.3 - 2026-09-06

- Recover v1 event decimal representations after numeric storage by authenticating
  the complete original fingerprint, preserving event bytes and legacy replay.
- Reject original quantities that cannot fit Decimal(76,18) before storage, and
  make precision checks independent of the ambient Decimal context.
- Add required real PostgreSQL and frozen 1.0.0/1.0.2 legacy compatibility gates.

## 1.0.2 - 2026-08-27

- Restore the established PyPI distribution identity `meridian-plugin-usage`.
- Supersede the GitHub-only 1.0.1 artifacts without creating a second PyPI project.
- Lock conformance evidence to Usage LLD revision 22.

## 1.0.1 - 2026-08-27 (GitHub-only; not published to PyPI)

- Attempt the distribution rename to `meridian-storage-plugin-usage`; superseded by 1.0.2.
- Preserve the `meridian_storage.plugins.usage` import namespace and plugin/schema entry points.
- Lock conformance evidence to Usage LLD revision 19.

## 1.0.0 - 2026-08-26

- Add immutable versioned meters, usage events, and aggregate records.
- Add exact Decimal unit normalization and correction-chain validation.
- Add bounded mapping-first record/query/batch APIs backed only by Meridian.
- Add deterministic aggregation with claims, watermark checkpoints, and late data revisions.
- Add OpenTelemetry and Evidence correlation, retention inputs, schema/plugin providers,
  conformance evidence, CI, and reproducible distribution checks.
