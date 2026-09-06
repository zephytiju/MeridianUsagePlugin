# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest

from meridian_storage import ConflictError
from meridian_storage.plugins.usage import (
    DecimalOverflow,
    InvalidUsageResult,
    MeterV1,
    UnitTransform,
    UsageConflict,
    UsageEventV1,
    UsageRepository,
)


def padded_record(event: UsageEventV1) -> dict[str, object]:
    record = event.to_dict()
    with localcontext() as context:
        context.prec = 1100
        context.traps[Inexact] = True
        for field in ("value", "originalValue"):
            if record[field] is not None:
                record[field] = format(Decimal(record[field]).quantize(Decimal("1e-18")), "f")
    return record


@pytest.mark.parametrize("scale", range(19))
@pytest.mark.parametrize("raw", ["12", "12.000000000000", "12.000000000000000000", "-0"])
def test_all_legal_meter_scales_recover_v1_bytes(event, meter, scale, raw):
    meter = replace(meter, scale=scale)
    original = replace(event, value=Decimal(raw)).normalized(meter)
    restored = UsageEventV1.from_mapping(padded_record(original))
    assert restored.to_dict() == original.to_dict()
    assert restored.normalized(meter).fingerprint == original.fingerprint


@pytest.mark.parametrize("raw", ["0." + "0" * 17 + "1" + "0" * 999, "1" + "0" * 57])
def test_legacy_input_digit_limit_and_full_precision(event, meter, raw):
    meter = replace(meter, precision=76, scale=18)
    original = replace(event, value=Decimal(raw), unit=meter.canonical_unit).normalized(meter)
    with localcontext() as context:
        context.prec = 3
        restored = UsageEventV1.from_mapping(padded_record(original))
    assert restored.to_dict() == original.to_dict()


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("value", "2001"),
        ("originalValue", "3"),
        ("subjectId", "different-subject"),
        ("provenance", {"source": "changed"}),
        ("originalUnit", "request"),
        ("fingerprint", "sha256:" + "f" * 64),
        ("scopeFingerprint", "sha256:" + "f" * 64),
        ("idempotencyKey", "sha256:" + "f" * 64),
        ("dimensionFingerprint", "sha256:" + "f" * 64),
        ("schemaVersion", "2.0.0"),
    ],
)
def test_recovery_never_trusts_a_stored_hash_or_changes_facts(event, meter, field, changed):
    record = padded_record(event.normalized(meter))
    record[field] = changed
    with pytest.raises(InvalidUsageResult):
        UsageEventV1.from_mapping(record)


def test_signed_correction_and_null_original_value(event, meter):
    correction = replace(
        event,
        event_id="correction",
        value=Decimal("-2.500"),
        correction_of=event.event_id,
        correction_reason="reverse excess",
    ).normalized(meter)
    assert UsageEventV1.from_mapping(padded_record(correction)).to_dict() == correction.to_dict()
    unnormalized = replace(event, original_value=None, original_unit=None)
    assert (
        UsageEventV1.from_mapping(padded_record(unnormalized)).to_dict() == unnormalized.to_dict()
    )


def test_reject_unrepresentable_original_before_storage(event):
    meter = MeterV1(
        event.meter_id,
        1,
        "requests",
        "request",
        transforms={"tiny": UnitTransform(Decimal("1000"))},
        scale=18,
    )
    with pytest.raises(DecimalOverflow, match="original_value"):
        replace(event, value=Decimal("1e-19"), unit="tiny", dimensions={}).normalized(meter)


def test_storage_precision_checks_do_not_round_with_ambient_context(event, meter):
    meter = replace(meter, precision=76, scale=18)
    maximum = "9" * 58 + "." + "9" * 18
    with localcontext() as context:
        context.prec = 3
        normalized = replace(event, value=Decimal(maximum), unit="request").normalized(meter)
        assert str(normalized.value) == maximum
        with pytest.raises(DecimalOverflow):
            replace(event, value=Decimal(maximum + "1"), unit="request").normalized(meter)


def test_lexically_different_originals_keep_existing_fingerprints(event, meter):
    first = event.normalized(meter)
    other = replace(event, value=Decimal("2.000")).normalized(meter)
    assert first.value == other.value
    assert first.fingerprint != other.fingerprint
    assert UsageEventV1.from_mapping(padded_record(first)).fingerprint == first.fingerprint
    assert UsageEventV1.from_mapping(padded_record(other)).fingerprint == other.fingerprint


def test_batch_and_event_replay_keep_original_receipt_fingerprints(executor, event, meter):
    repository = UsageRepository(executor)
    repository.register_meter(meter)
    first = repository.record_batch([event], batch_id="batch-1")
    for row in executor.records["events"].values():
        restored = UsageEventV1.from_mapping(row)
        row.update(padded_record(restored))
    # A fresh repository must preserve both the old manifest and event identity.
    reopened = UsageRepository(executor)
    replay = reopened.record_batch([event], batch_id="batch-1")
    assert replay.replayed
    assert replay.fingerprint == first.fingerprint
    assert replay.items == first.items
    second = reopened.record_batch([event], batch_id="batch-2")
    assert second.items[0].status.value == "replayed"
    assert second.items[0].fingerprint == first.items[0].fingerprint
    with pytest.raises(UsageConflict):
        reopened.record_batch([replace(event, value=Decimal("3"))], batch_id="batch-1")


def test_scoped_write_conflict_reconstructs_and_authenticates_persisted_event(
    executor, event, meter, monkeypatch
):
    repository = UsageRepository(executor)
    original = repository.record(event, meter=meter)
    for row in executor.records["events"].values():
        row.update(padded_record(original.event))
    get_event = repository.get_event
    reads = 0

    def race_get(*args):
        nonlocal reads
        reads += 1
        return None if reads == 1 else get_event(*args)

    def conflict(*_args, **_kwargs):
        raise ConflictError("TEST_IMMUTABLE_CONFLICT", "another writer created this event")

    monkeypatch.setattr(repository, "get_event", race_get)
    monkeypatch.setattr(repository, "_put", conflict)
    replay = repository.record(event, meter=meter)
    assert replay.replayed
    assert replay.event.to_dict() == original.event.to_dict()


def test_repository_rejects_missing_stored_fingerprint(executor, event, meter):
    repository = UsageRepository(executor)
    repository.record(event, meter=meter)
    for row in executor.records["events"].values():
        del row["fingerprint"]
    with pytest.raises(InvalidUsageResult, match="missing its fingerprint"):
        repository.get_event(event.scope, event.event_id)
