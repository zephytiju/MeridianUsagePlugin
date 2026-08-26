# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from opentelemetry.sdk.trace import TracerProvider

from meridian_storage import OperationContext, bind_context
from meridian_storage.plugins.usage import (
    DecimalOverflow,
    DimensionViolation,
    InactiveMeter,
    InvalidCorrection,
    InvalidUsage,
    MeterV1,
    UnitMismatch,
    UnitTransform,
    UsageAggregateV1,
    UsageCorrelation,
    UsageEventV1,
    UsageScope,
    UsageWindow,
    event_set_fingerprint,
)


def test_meter_version_exact_conversion_and_round_trip(meter: MeterV1) -> None:
    assert meter.ref == "api.requests@1"
    assert meter.normalize(Decimal("2.5"), "kilorequest") == Decimal("2500.000000")
    assert meter.normalize(3, "request") == Decimal("3.000000")
    assert MeterV1.from_mapping(meter.to_dict()) == meter
    assert meter.fingerprint == MeterV1.from_mapping(meter.to_dict()).fingerprint
    assert meter.is_active(datetime(2026, 8, 26, tzinfo=UTC))
    with pytest.raises(FrozenInstanceError):
        meter.version = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        meter.transforms["request"] = UnitTransform(Decimal("2"))  # type: ignore[index]


def test_meter_rejects_rounding_float_unknown_unit_and_dimensions(meter: MeterV1) -> None:
    with pytest.raises(InvalidUsage):
        meter.normalize(0.1, "request")
    with pytest.raises(UnitMismatch):
        meter.normalize(Decimal("1"), "second")
    with pytest.raises(DecimalOverflow):
        meter.normalize(Decimal("0.0000001"), "request")
    with pytest.raises(DimensionViolation):
        meter.validate_dimensions({"plan": "pro"})
    with pytest.raises(DimensionViolation):
        meter.validate_dimensions({"region": "west", "secret": "no"})


def test_meter_activation_and_affine_transform() -> None:
    temperature = MeterV1(
        "temperature",
        3,
        "temperature",
        "K",
        transforms={"C": UnitTransform(Decimal("1"), Decimal("273.15"))},
        precision=10,
        scale=2,
        active_from=datetime(2026, 1, 1, tzinfo=UTC),
        retired_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert temperature.normalize(Decimal("20"), "C") == Decimal("293.15")
    assert not temperature.is_active(datetime(2027, 1, 1, tzinfo=UTC))
    with pytest.raises(InvalidUsage):
        UnitTransform(Decimal("0"))
    with pytest.raises(InvalidUsage):
        MeterV1("bad", 1, "q", "u", transforms={"u": UnitTransform(Decimal("2"))})


def test_half_open_windows_are_utc_and_bucketed() -> None:
    window = UsageWindow(
        datetime.fromisoformat("2026-08-26T10:00:00+02:00"),
        datetime.fromisoformat("2026-08-26T10:05:00+02:00"),
    )
    assert window.start == datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
    assert window.contains(window.start)
    assert not window.contains(window.end)
    assert window.overlaps(UsageWindow(window.end - timedelta(seconds=1), window.end))
    assert UsageWindow.from_mapping(window.to_dict()) == window
    bucket = UsageWindow.bucket(window.start + timedelta(seconds=74), timedelta(minutes=1))
    assert bucket.start.second == 0
    assert bucket.duration == timedelta(minutes=1)
    pre_epoch = UsageWindow.bucket(
        datetime(1969, 12, 31, 23, 59, 59, 500_000, tzinfo=UTC),
        timedelta(seconds=1),
    )
    assert pre_epoch == UsageWindow(
        datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC),
        datetime(1970, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(InvalidUsage):
        UsageWindow(window.start, window.start)
    with pytest.raises(InvalidUsage):
        UsageWindow.bucket(window.start, timedelta(milliseconds=1))


def test_event_normalization_is_immutable_and_deterministic(
    meter: MeterV1,
    event: UsageEventV1,
) -> None:
    normalized = event.normalized(meter)
    assert event.value == Decimal("2")
    assert normalized.value == Decimal("2000.000000")
    assert normalized.unit == "request"
    assert normalized.original_value == Decimal("2")
    assert normalized.original_unit == "kilorequest"
    assert UsageEventV1.from_mapping(normalized.to_dict()) == normalized
    assert UsageEventV1.from_mapping(normalized.to_dict()).fingerprint == normalized.fingerprint
    assert normalized.identity.endswith("/event-001")
    assert normalized.to_dict()["idempotencyKey"] == normalized.idempotency_key
    assert event_set_fingerprint((normalized,)) == event_set_fingerprint((normalized,))
    future = replace(
        event,
        recorded_at=event.window.start,
    )
    with pytest.raises(InvalidUsage, match="event-time tolerance"):
        future.normalized(meter)
    tolerated = replace(meter, event_time_tolerance=timedelta(minutes=1))
    assert future.normalized(tolerated).value == Decimal("2000.000000")


def test_events_require_valid_correction_shape(
    event: UsageEventV1,
    meter: MeterV1,
) -> None:
    values = event.to_dict(include_fingerprint=False)
    values["eventId"] = "negative"
    values["value"] = "-1"
    with pytest.raises(InvalidCorrection):
        UsageEventV1.from_mapping(values)
    values["correctionOf"] = "event-001"
    values["correctionReason"] = "reverse duplicate"
    correction = UsageEventV1.from_mapping(values).normalized(meter)
    assert correction.value == Decimal("-1000.000000")
    values["eventId"] = "event-001"
    with pytest.raises(InvalidCorrection):
        UsageEventV1.from_mapping(values)


def test_event_rejects_inactive_meter(event: UsageEventV1, meter: MeterV1) -> None:
    retired = MeterV1(
        meter.meter_id,
        meter.version,
        meter.quantity,
        meter.canonical_unit,
        transforms=meter.transforms,
        dimensions=meter.dimensions,
        precision=meter.precision,
        scale=meter.scale,
        active_from=datetime(2025, 1, 1, tzinfo=UTC),
        retired_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(InactiveMeter):
        event.normalized(retired)
    crossing = replace(
        meter,
        active_from=event.window.start - timedelta(days=1),
        retired_at=event.window.start + timedelta(seconds=30),
    )
    with pytest.raises(InactiveMeter):
        event.normalized(crossing)


def test_correlation_preserves_request_and_otel_but_not_authorization() -> None:
    context = OperationContext(
        "principal-secret",
        request_id="request-42",
        tenant="tenant-secret",
        scope={"account": "scope-secret"},
        correlation_id="correlation-42",
    )
    tracer = TracerProvider().get_tracer("usage-test")
    with bind_context(context), tracer.start_as_current_span("record"):
        correlation = UsageCorrelation.capture()
    payload = correlation.to_dict()
    assert payload["requestId"] == "request-42"
    assert payload["correlationId"] == "correlation-42"
    assert len(payload["traceId"]) == 32
    assert len(payload["spanId"]) == 16
    assert "principal" not in str(payload)
    assert "tenant-secret" not in str(payload)
    assert "scope-secret" not in str(payload)
    assert correlation.to_evidence().to_dict() == payload


def test_aggregate_round_trip(scope: UsageScope, event: UsageEventV1) -> None:
    normalized = event
    aggregate = UsageAggregateV1(
        "aggregate-abc",
        1,
        scope,
        event.meter_id,
        event.meter_version,
        event.window,
        {"region": "us-west"},
        Decimal("2"),
        1,
        event.window.end,
        event_set_fingerprint((normalized,)),
        event.recorded_at,
    )
    assert aggregate.version_id == "aggregate-abc@1"
    assert UsageAggregateV1.from_mapping(aggregate.to_dict()) == aggregate
    with pytest.raises(InvalidUsage):
        UsageAggregateV1(
            "aggregate-abc",
            0,
            scope,
            event.meter_id,
            event.meter_version,
            event.window,
            {},
            Decimal(0),
            0,
            event.window.end,
            event_set_fingerprint((event,)),
            event.recorded_at,
        )
