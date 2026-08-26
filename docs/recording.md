# Recording

## Meters

`MeterV1` is immutable and addressed by `meter_id@version`. It
declares quantity, canonical unit, exact affine unit transforms, dimensions,
Decimal precision/scale, whole-second future event-time tolerance, and an
activation interval. A semantic change requires a new version.

Conversion accepts `Decimal`, integers, or decimal strings. Binary
floating-point values are rejected. Conversion traps inexact scale changes and
overflow; no silent rounding occurs.

## Events and corrections

`UsageEventV1` is scoped by a generic mapping and uses a half-open UTC
window. Its stored idempotency key is deterministically derived from the scope
fingerprint and event ID. Recording resolves the exact meter version, validates activity and
dimensions, converts to the canonical unit, captures safe correlation, and
rejects windows beyond the Meter's allowed future clock skew, then writes an
immutable record.

Duplicate semantics are content-aware:

- same scope, event ID, and canonical fingerprint: replay;
- same scope and event ID, different canonical fingerprint: conflict.

A correction is another event with `correction_of`,
`correction_reason`, and a signed delta. It must preserve the target
subject, meter version, window, and dimensions. Correction chains are allowed;
cycles are impossible because an event cannot reference itself and targets
must already exist.

## Batches

Batches contain at most 1,000 events, exactly one logical scope, and no
duplicate event IDs. Their fingerprint records the selected atomicity profile
and ordered canonical event fingerprints. A caller-provided batch ID is
immutable; reordered or otherwise divergent reuse conflicts.

The partitioned profile records per-item failures and a replay manifest. The
atomic profile uses one Meridian transaction against the events Resource after
meter resolution. It never attempts a cross-binding transaction.
