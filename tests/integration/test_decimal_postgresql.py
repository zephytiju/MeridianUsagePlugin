# SPDX-License-Identifier: Apache-2.0

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from integration.decimal_cases import CASES, make_case
from integration.postgres_backend import USAGE_RESOURCES
from meridian_storage import OperationContext
from meridian_storage.plugins.usage import (
    DecimalOverflow,
    InvalidUsageResult,
    MeterV1,
    UnitTransform,
    UsageConflict,
    UsageEventV1,
    UsageRepository,
)

LEGACY = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "legacy-decimal-events.json").read_text()
)["cases"]


def context():
    return OperationContext("test:publisher", tenant="synthetic", scope={"runtime": uuid4().hex})


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
def test_real_postgresql_roundtrip_replay_and_fresh_runtime(postgres_backend, case):
    meter, event = make_case(case)
    runtime = postgres_backend.start()
    ctx = context()
    repository = UsageRepository(runtime, USAGE_RESOURCES)
    with runtime.context(ctx):
        receipt = repository.record(event, meter=meter)
        readback = repository.get_event(event.scope, event.event_id)
        assert readback.to_dict() == receipt.event.to_dict()
        assert repository.record(event, meter=meter).replayed
        raw = runtime.execute(
            runtime.catalog("structured").get(
                resource=USAGE_RESOURCES.events.canonical,
                where={"scopeFingerprint": event.scope.fingerprint, "eventId": event.event_id},
            )
        ).data
        assert raw["fingerprint"] == receipt.event.fingerprint
        with pytest.raises(UsageConflict):
            repository.record(replace(event, subject_id="changed"), meter=meter)
    runtime.close()
    fresh = postgres_backend.start()
    with fresh.context(ctx):
        reopened = UsageRepository(fresh, USAGE_RESOURCES)
        assert reopened.get_event(event.scope, event.event_id).to_dict() == receipt.event.to_dict()
        assert reopened.record(event, meter=meter).event.fingerprint == receipt.event.fingerprint


def test_real_signed_correction_and_changed_quantity_conflict(postgres_backend):
    meter, event = make_case(CASES[0])
    runtime = postgres_backend.start()
    with runtime.context(context()):
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        repository.record(event, meter=meter)
        correction = replace(
            event,
            event_id="correction",
            value=Decimal("-2.500"),
            correction_of=event.event_id,
            correction_reason="reverse excess",
        )
        receipt = repository.record(correction, meter=meter)
        assert repository.get_event(event.scope, "correction").to_dict() == receipt.event.to_dict()
        assert repository.record(correction, meter=meter).replayed
        with pytest.raises(UsageConflict):
            repository.record(replace(correction, value=Decimal("-3")), meter=meter)


def test_rejected_transaction_has_no_committed_event_in_fresh_runtime(postgres_backend):
    meter, event = make_case(CASES[0])
    ctx = context()
    runtime = postgres_backend.start()
    repository = UsageRepository(runtime, USAGE_RESOURCES)
    with runtime.context(ctx), pytest.raises(UsageConflict):  # noqa: PT012, SIM117 - exception must leave the transaction
        with runtime.transaction(USAGE_RESOURCES.events):
            receipt = repository.record(event, meter=meter)
            # Keep the consumer's complete readback/fingerprint gate in place.
            assert (
                repository.get_event(event.scope, event.event_id).fingerprint
                == receipt.event.fingerprint
            )
            repository.record(replace(event, value=Decimal("13")), meter=meter)
    fresh = postgres_backend.start()
    with fresh.context(ctx):
        assert (
            UsageRepository(fresh, USAGE_RESOURCES).get_event(event.scope, event.event_id) is None
        )


def test_unrepresentable_original_is_rejected_before_any_write(postgres_backend):
    _, event = make_case(CASES[0])
    meter = MeterV1(
        event.meter_id,
        1,
        "requests",
        "request",
        scale=18,
        transforms={"tiny": UnitTransform(Decimal("1000"))},
    )
    event = replace(event, value=Decimal("1e-19"), unit="tiny")
    ctx = context()
    runtime = postgres_backend.start()
    with runtime.context(ctx), pytest.raises(DecimalOverflow):
        UsageRepository(runtime, USAGE_RESOURCES).record(event, meter=meter)
    fresh = postgres_backend.start()
    with fresh.context(ctx):
        assert (
            UsageRepository(fresh, USAGE_RESOURCES).get_event(event.scope, event.event_id) is None
        )


def test_corrupt_legacy_fingerprint_is_rejected_after_real_storage(postgres_backend):
    meter, event = make_case(CASES[0])
    record = event.normalized(meter).to_dict()
    record["subjectId"] = "tampered"
    runtime = postgres_backend.start()
    with runtime.context(context()):
        runtime.execute(
            runtime.catalog("structured").put(
                resource=USAGE_RESOURCES.events.canonical, data=record
            )
        )
        with pytest.raises(InvalidUsageResult):
            UsageRepository(runtime, USAGE_RESOURCES).get_event(event.scope, event.event_id)
    # The public parser must also authenticate the same complete record.
    with pytest.raises(InvalidUsageResult):
        UsageEventV1.from_mapping(record)


@pytest.mark.parametrize("case", LEGACY, ids=lambda case: case["name"])
def test_frozen_legacy_record_reconstructs_and_replays_unchanged(postgres_backend, case):
    meter = MeterV1.from_mapping(case["meter"])
    event = UsageEventV1.from_mapping(case["input"])
    # These bytes were captured from independently installed 1.0.0 and 1.0.2.
    assert event.normalized(meter).to_dict() == case["normalized"]
    ctx = context()
    runtime = postgres_backend.start()
    with runtime.context(ctx):
        runtime.execute(
            runtime.catalog("structured").put(
                resource=USAGE_RESOURCES.events.canonical, data=case["normalized"]
            )
        )
    fresh = postgres_backend.start()
    with fresh.context(ctx):
        repository = UsageRepository(fresh, USAGE_RESOURCES)
        assert repository.get_event(event.scope, event.event_id).to_dict() == case["normalized"]
        replay = repository.record(event, meter=meter)
        assert replay.replayed
        assert replay.event.to_dict() == case["normalized"]
