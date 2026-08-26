# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from conftest import MemoryExecutor
from meridian_storage import ValidationError
from meridian_storage.plugins.usage import (
    BatchMode,
    CheckpointConflict,
    ClaimUnavailable,
    InvalidCorrection,
    InvalidUsage,
    MeterV1,
    RecordStatus,
    UsageAggregateV1,
    UsageConflict,
    UsageEventV1,
    UsageOrder,
    UsageQuery,
    UsageRepository,
    UsageScope,
    event_set_fingerprint,
)


def _ready(executor: MemoryExecutor, meter: MeterV1) -> UsageRepository:
    repository = UsageRepository(executor)
    registered, replayed = repository.register_meter(meter)
    assert registered == meter
    assert not replayed
    return repository


def test_meter_registration_replay_and_divergent_conflict(
    executor: MemoryExecutor,
    meter: MeterV1,
) -> None:
    repository = UsageRepository(executor)
    assert repository.register_meter(meter) == (meter, False)
    assert repository.register_meter(meter) == (meter, True)
    assert repository.get_meter(meter.meter_id, meter.version) == meter
    with pytest.raises(UsageConflict):
        repository.register_meter(replace(meter, description="different"))


def test_record_normalizes_correlates_replays_and_rejects_divergence(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    receipt = repository.record(event)
    assert receipt.status is RecordStatus.RECORDED
    assert receipt.event.value == Decimal("2000.000000")
    assert receipt.operation_result is not None
    assert receipt.correlation.operation_fingerprint is not None
    replay = repository.record(event)
    assert replay.status is RecordStatus.REPLAYED
    assert replay.event == receipt.event
    with pytest.raises(UsageConflict):
        repository.record(replace(event, value=Decimal("3")))


def test_correction_chain_requires_same_immutable_domain(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    base = repository.record(event).event
    correction = replace(
        event,
        event_id="event-correction",
        value=Decimal("-2"),
        correction_of=base.event_id,
        correction_reason="duplicate reversal",
    )
    recorded = repository.record(correction).event
    assert recorded.value == Decimal("-2000.000000")
    second = replace(
        event,
        event_id="event-correction-2",
        value=Decimal("1"),
        correction_of=recorded.event_id,
        correction_reason="partial restore",
    )
    assert repository.record(second).event.correction_of == recorded.event_id
    mismatched = replace(correction, event_id="bad-correction", subject_id="different")
    with pytest.raises(InvalidCorrection):
        repository.record(mismatched)
    missing = replace(correction, event_id="missing-correction", correction_of="absent")
    with pytest.raises(InvalidCorrection):
        repository.record(missing)


def test_partitioned_batch_replay_conflict_and_failure(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    second = replace(event, event_id="event-002", value=Decimal("1"))
    receipt = repository.record_batch(
        (event, second),
        batch_id="publisher-batch-1",
        mode=BatchMode.PARTITIONED,
    )
    assert receipt.complete
    assert [item.event_id for item in receipt.items] == ["event-001", "event-002"]
    replay = repository.record_batch(
        (event, second),
        batch_id="publisher-batch-1",
        mode=BatchMode.PARTITIONED,
    )
    assert replay.replayed
    with pytest.raises(UsageConflict):
        repository.record_batch(
            (second, event),
            batch_id="publisher-batch-1",
            mode=BatchMode.PARTITIONED,
        )
    with pytest.raises(UsageConflict):
        repository.record_batch(
            (event, second),
            batch_id="publisher-batch-1",
            mode=BatchMode.ATOMIC,
        )
    with pytest.raises(UsageConflict):
        repository.record_batch(
            (event,),
            batch_id="publisher-batch-1",
            mode=BatchMode.PARTITIONED,
        )
    third = replace(event, event_id="event-fails")
    executor.fail_event_ids.add(third.event_id)
    failed = repository.record_batch((third,), batch_id="publisher-batch-failure")
    assert failed.items[0].status is RecordStatus.FAILED
    assert failed.items[0].error_code == "TEST_USAGE_WRITE_FAILURE"


def test_atomic_batch_uses_one_transaction_and_rolls_back(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    first = replace(event, event_id="atomic-1")
    second = replace(event, event_id="atomic-failure")
    executor.fail_event_ids.add(second.event_id)
    with pytest.raises(ValidationError):
        repository.record_batch(
            (first, second),
            batch_id="atomic-batch",
            mode=BatchMode.ATOMIC,
        )
    assert executor.transaction_entries == 1
    assert repository.get_event(event.scope, first.event_id) is None
    assert repository._batch_record(event.scope, "atomic-batch") is None


def test_atomic_batch_requires_transaction_capability(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    _ready(executor, meter)

    class ExecuteOnly:
        def execute(self, expression):
            return executor.execute(expression)

    repository = UsageRepository(ExecuteOnly())
    with pytest.raises(InvalidUsage, match="transaction-capable"):
        repository.record_batch((event,), mode=BatchMode.ATOMIC)


def test_batch_enforces_one_scope(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    other = replace(event, event_id="other", scope=UsageScope({"tenant": "other"}))
    with pytest.raises(InvalidUsage, match="one scope"):
        repository.record_batch((event, other))
    with pytest.raises(InvalidUsage, match="duplicate event id"):
        repository.record_batch((event, event))


def test_bounded_queries_paginate_and_isolate_scopes(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    repository.record(event)
    repository.record(replace(event, event_id="event-002"))
    other_scope = UsageScope({"tenant": "other", "environment": "test"})
    repository.record(replace(event, event_id="other-event", scope=other_scope))
    query = repository.queries.events(
        event.scope,
        event.window.start,
        event.window.end + timedelta(minutes=1),
        where={"meterId": meter.meter_id},
    ).page(limit=1)
    first = query.execute()
    second = query.page(limit=1, cursor=first.cursor).execute()
    assert len(first.items) == len(second.items) == 1
    assert {first.items[0]["eventId"], second.items[0]["eventId"]} == {
        "event-001",
        "event-002",
    }
    assert all(item["scopeFingerprint"] == event.scope.fingerprint for item in first.items)
    assert query.expression.catalog == "structured"
    assert query.expression.arguments["where"]["scopeFingerprint"] == event.scope.fingerprint
    assert query.logical_plan.catalog == "structured"
    assert query.fingerprint.startswith("sha256:")


def test_query_validates_reserved_predicates_and_cursor(
    executor: MemoryExecutor,
    event: UsageEventV1,
) -> None:
    with pytest.raises(InvalidUsage, match="derived"):
        UsageQuery(
            executor,
            UsageRepository(executor).resources.events,
            event.scope,
            event.window,
            {"scopeFingerprint": "forged"},
        )
    with pytest.raises(InvalidUsage, match="cursor"):
        UsageQuery(
            executor,
            UsageRepository(executor).resources.events,
            event.scope,
            event.window,
            cursor="\n",
        )
    query = UsageQuery(
        executor,
        UsageRepository(executor).resources.events,
        event.scope,
        event.window,
        order_by=(UsageOrder("windowStart", "desc"),),
    )
    assert query.expression.arguments["orderBy"][0]["direction"] == "desc"


def test_checkpoint_cas_and_claim_races(
    executor: MemoryExecutor,
    scope: UsageScope,
) -> None:
    repository = UsageRepository(executor)
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    first = repository.advance_checkpoint(
        scope,
        "hourly",
        now,
        expected_revision=0,
        now=now,
    )
    assert first.revision == 1
    with pytest.raises(CheckpointConflict):
        repository.advance_checkpoint(
            scope,
            "hourly",
            now + timedelta(hours=1),
            expected_revision=0,
            now=now,
        )
    second = repository.advance_checkpoint(
        scope,
        "hourly",
        now + timedelta(hours=1),
        expected_revision=1,
        now=now,
    )
    assert second.revision == 2
    claim = repository.acquire_claim(
        scope,
        "hourly",
        "worker-a",
        expires_at=now + timedelta(minutes=5),
        now=now,
    )
    with pytest.raises(ClaimUnavailable):
        repository.acquire_claim(
            scope,
            "hourly",
            "worker-b",
            expires_at=now + timedelta(minutes=5),
            now=now,
        )
    released = repository.release_claim(claim, now=now + timedelta(seconds=1))
    assert released.expires_at == now + timedelta(seconds=1)
    acquired = repository.acquire_claim(
        scope,
        "hourly",
        "worker-b",
        expires_at=now + timedelta(minutes=6),
        now=now + timedelta(seconds=2),
    )
    assert acquired.owner == "worker-b"


def test_aggregate_versions_are_immutable_and_latest_is_selected(
    executor: MemoryExecutor,
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    repository = _ready(executor, meter)
    normalized = repository.record(event).event
    base = UsageAggregateV1(
        "aggregate-latest",
        1,
        event.scope,
        meter.meter_id,
        meter.version,
        event.window,
        {"region": "us-west"},
        normalized.value,
        1,
        event.window.end,
        event_set_fingerprint((normalized,)),
        event.recorded_at,
    )
    assert repository.put_aggregate(base) == (base, False)
    assert repository.put_aggregate(base) == (base, True)
    revised = replace(
        base,
        revision=2,
        total=Decimal("3000"),
        supersedes=base.version_id,
        source_fingerprint="sha256:" + ("1" * 64),
    )
    repository.put_aggregate(revised)
    revision_nine = replace(
        revised,
        revision=9,
        supersedes=revised.version_id,
        source_fingerprint="sha256:" + ("2" * 64),
    )
    repository.put_aggregate(revision_nine)
    revision_ten = replace(
        revised,
        revision=10,
        supersedes=revision_nine.version_id,
        source_fingerprint="sha256:" + ("3" * 64),
    )
    repository.put_aggregate(revision_ten)
    latest = repository.latest_aggregate(
        event.scope,
        base.aggregate_id,
        window_start=event.window.start,
        window_end=event.window.end,
    )
    assert latest == revision_ten
    with pytest.raises(UsageConflict):
        repository.put_aggregate(replace(revised, total=Decimal("4000")))
