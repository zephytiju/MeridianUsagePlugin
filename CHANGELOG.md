# Changelog

All notable changes follow Semantic Versioning.

## 1.0.1 - 2026-08-27

- Correct the public distribution identity to `meridian-storage-plugin-usage`.
- Preserve the `meridian_storage.plugins.usage` import namespace and plugin/schema entry points.
- Lock conformance evidence to Usage LLD revision 19.

## 1.0.0 - 2026-08-26

- Add immutable versioned meters, usage events, and aggregate records.
- Add exact Decimal unit normalization and correction-chain validation.
- Add bounded mapping-first record/query/batch APIs backed only by Meridian.
- Add deterministic aggregation with claims, watermark checkpoints, and late data revisions.
- Add OpenTelemetry and Evidence correlation, retention inputs, schema/plugin providers,
  conformance evidence, CI, and reproducible distribution checks.
