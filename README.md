# meridian-plugin-usage

Meridian V1's open-source Usage plugin provides immutable metering events,
exact unit normalization, bounded queries, deterministic window aggregation,
retention inputs, and OpenTelemetry/Evidence correlation over logical Meridian
Resources.

This distribution is a Python library and Meridian plugin. It is not a service
or a Usage Catalog. It owns no database, provisions no engine, accepts no
engine credentials, calculates no prices, and uses only released mapping-first
Meridian Expressions.

## Install

```console
python -m pip install meridian-plugin-usage==1.0.0
```

Python 3.12, 3.13, and 3.14 are supported. Runtime Meridian dependencies are
exactly pinned to their compatible 1.0.0 releases.

## Record immutable usage

Platform IaC renders the Meridian runtime, logical Resource placements,
identity/ACL, migrations, retention, and engine lifecycle. Application code
only creates the ready runtime and uses this library:

```python
from datetime import UTC, datetime
from decimal import Decimal

from meridian_storage.plugins.usage import (
    DimensionSpec,
    MeterV1,
    UnitTransform,
    Usage,
    UsageEvent,
    UsageScope,
    UsageWindow,
)

usage = Usage(ready_meridian)

meter = MeterV1(
    meter_id="api.requests",
    version=1,
    quantity="requests",
    canonical_unit="request",
    transforms={"kilorequest": UnitTransform(Decimal("1000"))},
    dimensions=(DimensionSpec("region", required=True),),
    scale=6,
    active_from=datetime(2026, 1, 1, tzinfo=UTC),
)
usage.repository.register_meter(meter)

event = UsageEvent(
    event_id="publisher-event-0001",
    scope=UsageScope({"tenant": "acme", "environment": "prod"}),
    subject_id="account-42",
    meter_id=meter.meter_id,
    meter_version=meter.version,
    window=UsageWindow(
        datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
        datetime(2026, 8, 26, 10, 1, tzinfo=UTC),
    ),
    value=Decimal("2"),
    unit="kilorequest",
    dimensions={"region": "us-west"},
    source="publisher",
)
receipt = usage.recorder.record(event)
assert receipt.event.value == Decimal("2000.000000")
```

An identical replay returns `RecordStatus.REPLAYED`. The same scoped
`event_id` with different normalized content raises the stable
`MERIDIAN_USAGE_IMMUTABLE_CONFLICT` error. Corrections are new signed
events referencing `correction_of`; existing events are never mutated.

## Query and aggregate

```python
page = usage.queries.events(
    event.scope,
    datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
    datetime(2026, 8, 26, 11, 0, tzinfo=UTC),
    where={"meterId": meter.meter_id},
).page(limit=100).execute()
```

Every query is scope-bound, time-bounded, cursor-paginated, and exposed both as
a mapping-first `Expression` and a serialized Query 1.0.0 logical plan.
Aggregation is a library worker with deterministic claims, immutable aggregate
versions, and compare-and-set watermark checkpoints. Retrying after a crash
replays identical outputs; late events create a new version that names the
version it supersedes.

## Batch profiles

- `partitioned` records deterministic per-event outcomes and is portable
  to append-oriented placements such as ClickHouse.
- `atomic` opens one Meridian transaction on the events Resource and is
  accepted only when the rendered placement advertises the transaction
  capability.

Batch manifests and aggregation control records are logical structured
Resources. They are not a private database.

## Contracts and evidence

The package ships released Semantics schemas, plugin/schema-provider entry
points, JSON Schema compatibility evidence, deterministic golden
fingerprints, unit/integration/conformance tests, reproducible build checks,
and Apache-2.0 LICENSE/NOTICE material.

See [architecture](docs/architecture.md), [recording](docs/recording.md),
[aggregation](docs/aggregation.md), [configuration](docs/configuration.md),
[compatibility](docs/compatibility.md), and [conformance](docs/conformance.md).

Licensed under the Apache License, Version 2.0.

