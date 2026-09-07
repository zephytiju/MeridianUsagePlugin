# SPDX-License-Identifier: Apache-2.0
"""Mode acceptance through the released runtime and unchanged owning schemas."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from integration.decimal_cases import CASES, make_case
from integration.postgres_backend import USAGE_RESOURCES
from integration.test_decimal_postgresql import context
from meridian_storage.plugins.usage import (
    CheckpointConflict,
    ClaimUnavailable,
    InvalidUsageResult,
    UsageAggregateV1,
    UsageConflict,
    UsageRepository,
    event_set_fingerprint,
)


def test_meter_registration_on_released_mode_contract(mode_backend):
    meter, _ = make_case(CASES[0])
    runtime = mode_backend.start()
    with runtime.context(context()):
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        assert repository.register_meter(meter) == (meter, False)
        assert repository.register_meter(meter) == (meter, True)


@pytest.mark.parametrize("fresh_runtime", [False, True])
def test_checkpoint_advances_persisted_state(mode_backend, fresh_runtime):
    _, event = make_case(CASES[0])
    runtime = mode_backend.start()
    ctx = context()
    with runtime.context(ctx):
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        now = datetime(2026, 9, 1, tzinfo=UTC)
        first = repository.advance_checkpoint(event.scope, "hourly", now, expected_revision=0)
    fresh = mode_backend.start() if fresh_runtime else runtime
    with fresh.context(ctx):
        repository = UsageRepository(fresh, USAGE_RESOURCES)
        second = repository.advance_checkpoint(
            event.scope, "hourly", now + timedelta(hours=1), expected_revision=first.revision
        )
        persisted = repository.get_checkpoint(event.scope, "hourly")
        assert (persisted.revision, persisted.watermark) == (second.revision, second.watermark)
        with pytest.raises(CheckpointConflict):
            repository.advance_checkpoint(event.scope, "hourly", now, expected_revision=0)
        assert repository.get_checkpoint(event.scope, "hourly") == persisted


@pytest.mark.parametrize(
    "total", ["12", "12.000000000000", "12.000000000000000000", "0", "-2.500", "1." + "0" * 25]
)
def test_aggregate_published_values_survive_readback_and_retry(mode_backend, total):
    meter, event = make_case(CASES[0])
    normalized = event.normalized(meter)
    aggregate = UsageAggregateV1(
        "mode-test",
        1,
        event.scope,
        meter.meter_id,
        meter.version,
        event.window,
        {},
        Decimal(total),
        1,
        event.window.end,
        event_set_fingerprint((normalized,)),
        event.recorded_at,
    )
    runtime = mode_backend.start()
    with runtime.context(context()):
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        assert repository.put_aggregate(aggregate) == (aggregate, False)
        assert repository.put_aggregate(aggregate) == (aggregate, True)
        with pytest.raises(UsageConflict):
            repository.put_aggregate(replace(aggregate, total=aggregate.total + Decimal("1")))
        with pytest.raises(InvalidUsageResult):
            UsageAggregateV1.from_mapping({**aggregate.to_dict(), "eventCount": 99})
        with pytest.raises(InvalidUsageResult):
            UsageAggregateV1.from_mapping({**aggregate.to_dict(), "aggregateVersionId": "forged"})


def test_checkpoint_initialization_preserves_domain_timestamp(mode_backend):
    _, event = make_case(CASES[0])
    runtime = mode_backend.start()
    with runtime.context(context()):
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        now = datetime(2026, 9, 1, tzinfo=UTC)
        first = repository.advance_checkpoint(
            event.scope, "timestamp", now, expected_revision=0, now=now
        )
        assert repository.get_checkpoint(event.scope, "timestamp") == first


class ReadBarrier:
    """Force both public reads to finish before the competing writes start."""

    def __init__(self, runtime, barrier, resource):
        self.runtime = runtime
        self.barrier = barrier
        self.resource = resource.to_dict()
        self.waited = False

    def execute(self, expression):
        result = self.runtime.execute(expression)
        if (
            not self.waited
            and expression.method == "get"
            and dict(expression.arguments["resource"]) == self.resource
        ):
            self.waited = True
            self.barrier.wait(timeout=10)
        return result


@pytest.mark.parametrize("changed", [False, True])
def test_concurrent_meter_registration_keeps_one_definition(mode_backend, changed):
    meter, _ = make_case(CASES[0])
    ctx = context()
    runtimes = [mode_backend.start(), mode_backend.start()]
    barrier = Barrier(2)

    def register(index):
        runtime = runtimes[index]
        candidate = replace(meter, description="changed") if changed and index else meter
        repository = UsageRepository(
            ReadBarrier(runtime, barrier, USAGE_RESOURCES.meters), USAGE_RESOURCES
        )
        with runtime.context(ctx):
            try:
                return repository.register_meter(candidate)
            except UsageConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, range(2)))
    if changed:
        assert results.count("conflict") == 1
    else:
        assert sorted(result[1] for result in results) == [False, True]
    with runtimes[0].context(ctx):
        stored = UsageRepository(runtimes[0], USAGE_RESOURCES).get_meter(
            meter.meter_id, meter.version
        )
        assert stored == next(
            result[0] for result in results if result != "conflict" and not result[1]
        )


@pytest.mark.parametrize("initialize", [False, True])
def test_concurrent_checkpoint_has_one_cas_winner(mode_backend, initialize):
    _, event = make_case(CASES[0])
    ctx = context()
    runtimes = [mode_backend.start(), mode_backend.start()]
    now = datetime(2026, 9, 1, tzinfo=UTC)
    revision = 0 if initialize else 1
    if not initialize:
        with runtimes[0].context(ctx):
            UsageRepository(runtimes[0], USAGE_RESOURCES).advance_checkpoint(
                event.scope, "race", now, expected_revision=0, now=now
            )
    barrier = Barrier(2)

    def advance(index):
        runtime = runtimes[index]
        repository = UsageRepository(
            ReadBarrier(runtime, barrier, USAGE_RESOURCES.checkpoints), USAGE_RESOURCES
        )
        with runtime.context(ctx):
            try:
                return repository.advance_checkpoint(
                    event.scope,
                    "race",
                    now + timedelta(hours=index + 1),
                    expected_revision=revision,
                    now=now,
                )
            except CheckpointConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(advance, range(2)))
    assert results.count("conflict") == 1
    winner = next(result for result in results if result != "conflict")
    with runtimes[0].context(ctx):
        persisted = UsageRepository(runtimes[0], USAGE_RESOURCES).get_checkpoint(
            event.scope, "race"
        )
        assert (persisted.revision, persisted.watermark) == (winner.revision, winner.watermark)


def test_claim_renewal_release_and_stale_owner(mode_backend):
    _, event = make_case(CASES[0])
    runtime = mode_backend.start()
    now = datetime(2026, 9, 1, tzinfo=UTC)
    with runtime.context(context()):
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        first = repository.acquire_claim(
            event.scope, "claim", "a", expires_at=now + timedelta(minutes=5), now=now
        )
        second = repository.acquire_claim(
            event.scope, "claim", "a", expires_at=now + timedelta(minutes=6), now=now
        )
        assert second.revision == first.revision + 1
        with pytest.raises(ClaimUnavailable):
            repository.release_claim(first, now=now)
        repository.release_claim(second, now=now)
        third = repository.acquire_claim(
            event.scope, "claim", "b", expires_at=now + timedelta(minutes=7), now=now
        )
        persisted = repository.get_claim(event.scope, "claim")
        assert (persisted.revision, persisted.owner, persisted.expires_at) == (
            third.revision,
            third.owner,
            third.expires_at,
        )


def test_real_batch_replay_and_physical_scope_isolation(mode_backend):
    meter, event = make_case(CASES[0])
    runtime = mode_backend.start()
    ctx = context()
    repository = UsageRepository(runtime, USAGE_RESOURCES)
    with runtime.context(ctx):
        repository.register_meter(meter)
        receipt = repository.record_batch((event,), batch_id="mode-batch")
        assert receipt.complete
        assert repository.record_batch((event,), batch_id="mode-batch").replayed
        with pytest.raises(UsageConflict):
            repository.record_batch((replace(event, value=Decimal("99")),), batch_id="mode-batch")
        with pytest.raises(UsageConflict):
            repository.register_meter(replace(meter, description="changed"))
    with runtime.context(context()):
        assert repository.get_event(event.scope, event.event_id) is None
        alternative = replace(meter, description="other mandatory scope")
        assert repository.register_meter(alternative) == (alternative, False)
        assert repository.record_batch((event,), batch_id="mode-batch").complete
    with runtime.context(ctx):
        assert repository.get_meter(meter.meter_id, meter.version) == meter
