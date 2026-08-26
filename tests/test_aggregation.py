# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from conftest import MemoryExecutor
from meridian_storage.plugins.usage import (
    AggregationRunner,
    AggregationSpec,
    CheckpointConflict,
    InvalidUsage,
    MeterV1,
    UsageEventV1,
    UsageRepository,
    aggregate_events,
)


def _spec(event: UsageEventV1) -> AggregationSpec:
    return AggregationSpec(
        event.scope,
        event.meter_id,
        event.meter_version,
        event.window.start,
        timedelta(hours=1),
        dimensions=("region",),
        allowed_lateness=timedelta(hours=2),
        checkpoint_id="hourly-api",
        claim_id="hourly-api",
        page_size=1,
    )


def test_pure_aggregation_is_order_independent_and_corrections_are_signed(
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    base = event.normalized(meter)
    second = replace(event, event_id="event-002", value=Decimal("1")).normalized(meter)
    correction = replace(
        event,
        event_id="event-correction",
        value=Decimal("-0.5"),
        correction_of=event.event_id,
        correction_reason="partial adjustment",
    ).normalized(meter)
    watermark = datetime(2026, 8, 26, 11, tzinfo=UTC)
    created = datetime(2026, 8, 26, 11, 1, tzinfo=UTC)
    spec = _spec(event)
    forward = aggregate_events(
        (base, second, correction),
        spec,
        watermark=watermark,
        created_at=created,
    )
    reverse = aggregate_events(
        (correction, second, base),
        spec,
        watermark=watermark,
        created_at=created,
    )
    assert forward == reverse
    assert len(forward) == 1
    assert forward[0].total == Decimal("2500.000000")
    assert forward[0].event_count == 3
    assert forward[0].revision == 1
    replay = aggregate_events(
        (base, correction, second),
        spec,
        watermark=watermark + timedelta(hours=1),
        created_at=created + timedelta(hours=1),
        previous={forward[0].aggregate_id: forward[0]},
    )
    assert replay == forward


def test_runner_pages_advances_checkpoint_and_revises_late_windows(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = UsageRepository(executor)
    repository.register_meter(meter)
    repository.record(event)
    spec = _spec(event)
    runner = AggregationRunner(repository, spec)
    first = runner.run(
        datetime(2026, 8, 26, 11, tzinfo=UTC),
        owner="worker-a",
        now=datetime(2026, 8, 26, 11, 5, tzinfo=UTC),
    )
    assert first.events_read == 1
    assert first.checkpoint.revision == 1
    assert first.aggregates[0].revision == 1
    late = replace(
        event,
        event_id="late-event",
        value=Decimal("1"),
        recorded_at=datetime(2026, 8, 26, 11, 30, tzinfo=UTC),
    )
    repository.record(late)
    second = runner.run(
        datetime(2026, 8, 26, 12, tzinfo=UTC),
        owner="worker-a",
        now=datetime(2026, 8, 26, 12, 5, tzinfo=UTC),
    )
    assert second.checkpoint.revision == 2
    revised = next(
        item for item in second.aggregates if item.aggregate_id == first.aggregates[0].aggregate_id
    )
    assert revised.revision == 2
    assert revised.supersedes == first.aggregates[0].version_id
    assert revised.total == Decimal("3000.000000")


def test_runner_recovers_after_aggregate_write_before_checkpoint(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    class CrashOnceRepository(UsageRepository):
        crash_once = True

        def advance_checkpoint(self, *args, **kwargs):
            if self.crash_once:
                self.crash_once = False
                raise CheckpointConflict("hourly-api")
            return super().advance_checkpoint(*args, **kwargs)

    repository = CrashOnceRepository(executor)
    repository.register_meter(meter)
    repository.record(event)
    runner = AggregationRunner(repository, _spec(event))
    watermark = datetime(2026, 8, 26, 11, tzinfo=UTC)
    now = datetime(2026, 8, 26, 11, 5, tzinfo=UTC)
    with pytest.raises(CheckpointConflict):
        runner.run(watermark, owner="worker-a", now=now)
    assert len(executor.records["aggregates"]) == 1
    recovered = runner.run(
        watermark,
        owner="worker-a",
        now=now + timedelta(minutes=1),
    )
    assert recovered.replayed_aggregates == 1
    assert recovered.checkpoint.revision == 1
    assert len(executor.records["aggregates"]) == 1


def test_aggregation_validates_scope_meter_and_watermarks(
    event: UsageEventV1,
    meter: MeterV1,
) -> None:
    spec = _spec(event)
    normalized = event.normalized(meter)
    with pytest.raises(InvalidUsage, match="follow initial_start"):
        AggregationRunner(UsageRepository(MemoryExecutor()), spec).run(
            spec.initial_start,
            owner="worker",
            now=spec.initial_start,
        )
    with pytest.raises(InvalidUsage, match="match"):
        aggregate_events(
            (replace(normalized, meter_id="different"),),
            spec,
            watermark=spec.initial_start + timedelta(hours=1),
            created_at=spec.initial_start,
        )
    with pytest.raises(InvalidUsage):
        replace(spec, page_size=0)
    with pytest.raises(InvalidUsage, match="whole seconds"):
        replace(spec, allowed_lateness=timedelta(milliseconds=500))
