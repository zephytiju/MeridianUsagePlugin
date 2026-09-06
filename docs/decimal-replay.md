# Decimal storage and v1 replay compatibility

Usage 1.0.0 and 1.0.2 fingerprint the fixed-point decimal text of both `value`
and `originalValue`. Meter normalization supplies its declared scale (default
12); original input retains its lexical scale. The owning schema stores both
as `Decimal(76,18)`. PostgreSQL preserves the number while padding it to scale
18, so reconstructing the original event previously changed its fingerprint.

## Compatibility strategy

The 1.0.3 fix retains the exact v1 encoder, event schema version, meter rules,
event fingerprints, batch fingerprints and event-set fingerprints. It does not
strip trailing zeros on new writes or replace a calculated hash with a stored
hash. There are no added schema fields or data migrations. Existing event,
meter, aggregate and schema golden fingerprints remain unchanged. The schema
provider bundle fingerprint changes because it includes the package version.

When a stored fingerprint already matches the reconstructed event, reading is
unchanged. Otherwise, reconstruction enumerates only numerically equal decimal
representations and recalculates the **complete v1 event digest**, including
both decimal fields, timestamps, provenance, correlation, scope, dimensions and
all other event facts. It returns the representation whose SHA-256 matches the
persisted digest. A mismatch raises `InvalidUsageResult`. Derived identity and
schema fields are checked as well; repository reads require a fingerprint.

This recovery is bounded by the existing public input domain. Normalized
`value` has one of 19 legal meter scales. `originalValue` has at most 1000
coefficient digits, including trailing zeros. Each nonzero stored number has
at most 1000 admissible fixed-point original representations. Zero has always
encoded as `0`; positive exponents serialize as fixed-point integers. Recovery
therefore examines at most 19,000 exact candidates. Fixed canonical fields are
encoded once, and SHA-256 prefix states are reused. No Decimal arithmetic,
rounding or ambient-context normalization occurs during recovery.

This also covers original inputs padded beyond 18 places when their numerical
value fits storage. Two inputs that differ in lexical original scale keep
their existing, distinct v1 fingerprints. Arbitrary old records whose numbers
were actually rounded by storage cannot be authenticated and are rejected.
New normalization rejects any original number outside `Decimal(76,18)` before
writing, including a tiny input that would otherwise convert to a legal meter
quantity. Precision validation inspects the exact coefficient and exponent.

## Verification and boundaries

`tests/fixtures/legacy-decimal-events.json` contains complete records captured
from independent released environments. `scripts/reproduce_decimal_v1.py` runs
the real PostgreSQL facade with coherent Cost 1.0.0 + Usage 1.0.0, and separately
with Usage 1.0.2 without Cost. Both must reproduce the old failures and exactly
match those fixtures. CI runs both environments without a source override.

The repaired suite verifies all legal meter scales, mixed original scales,
scale18/raw12, zero, signed corrections, exact conversion, full 76-digit
precision, input digit limits, changed facts and corrupted hashes. Real
PostgreSQL tests cover frozen old records, fresh Runtime instances, unchanged
stored fingerprints, identical replay and absence after transaction rejection.
The consumer's full fingerprint equality gate stays intact. A fresh Runtime
instance is not a PostgreSQL restart or a process-crash durability claim.

Set `USAGE_TEST_POSTGRES_DSN` to an isolated PostgreSQL/PostGIS 16 test database
and run `pytest`. The fixtures provision and remove only their own randomized
schema, using the released owning schemas and adapter migration contract.
CI and release validation provide this database and fail if live acceptance
is unavailable. `pytest --ignore=tests/integration` runs the non-live subset.

This patch retains the released 1.0.0 Meridian dependency closure. Immutable
meter registration and the explicit structured-put migration are separate
tasks. Cost 1.0.0 still pins Usage 1.0.0; consumers must wait for a separately
published compatible Cost release instead of overriding that dependency.
