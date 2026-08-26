# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from conftest import MemoryExecutor
from meridian_storage import Meridian, ResourceRef, RuntimeState
from meridian_storage.plugins.usage import (
    AggregationSpec,
    BatchItemResult,
    DimensionSpec,
    InvalidUsage,
    InvalidUsageResult,
    MeterV1,
    RetentionInput,
    UnitTransform,
    UnknownMeter,
    Usage,
    UsageAggregateV1,
    UsageCorrelation,
    UsageEventV1,
    UsageOrder,
    UsageQuery,
    UsageRepository,
    UsageResources,
    UsageScope,
)
from meridian_storage.plugins.usage._canonical import (
    bounded_text,
    decimal_value,
    json_value,
    logical_name,
    parse_datetime,
    require_fingerprint,
    string_map,
    token,
    unit_name,
    utc_datetime,
)
from meridian_storage.plugins.usage.query import _records, _validate_where
from meridian_storage.plugins.usage.repository import _single_record, _storage_version


@pytest.mark.parametrize(
    "call",
    [
        lambda: bounded_text("", "value"),
        lambda: bounded_text("\n", "value"),
        lambda: logical_name("not a name", "name"),
        lambda: token("bad token", "token"),
        lambda: unit_name("unit with spaces"),
        lambda: utc_datetime(datetime(2026, 1, 1), "timestamp"),
        lambda: parse_datetime(42, "timestamp"),
        lambda: parse_datetime("not-a-time", "timestamp"),
        lambda: decimal_value("NaN"),
        lambda: decimal_value("1e1001"),
        lambda: decimal_value(object()),
        lambda: string_map({str(index): "x" for index in range(33)}, "mapping"),
        lambda: json_value(2**70, "integer"),
        lambda: json_value(float("inf"), "float"),
        lambda: json_value({str(index): index for index in range(65)}, "object"),
        lambda: json_value(list(range(257)), "array"),
        lambda: json_value({1, 2}, "set"),
        lambda: require_fingerprint("sha256:no"),
    ],
)
def test_canonical_validators_fail_closed(call) -> None:
    with pytest.raises(InvalidUsage):
        call()


def test_canonical_depth_and_safe_json() -> None:
    nested: object = "leaf"
    for _ in range(10):
        nested = [nested]
    with pytest.raises(InvalidUsage, match="nesting"):
        json_value(nested, "nested")
    assert json_value({"safe": [1, True, None, 2.5]}, "safe") == {"safe": [1, True, None, 2.5]}


def test_model_constructor_validation(event: UsageEventV1, meter: MeterV1) -> None:
    with pytest.raises(InvalidUsage):
        UsageScope({})
    with pytest.raises(InvalidUsage):
        UnitTransform.from_mapping({"offset": "1"})
    with pytest.raises(InvalidUsage):
        DimensionSpec("region", required=1)  # type: ignore[arg-type]
    with pytest.raises(InvalidUsage):
        DimensionSpec("region", max_cardinality=0)
    with pytest.raises(InvalidUsage):
        DimensionSpec.from_mapping({"required": True})
    with pytest.raises(InvalidUsage):
        replace(meter, version=0)
    with pytest.raises(InvalidUsage):
        replace(meter, precision=77)
    with pytest.raises(InvalidUsage):
        replace(meter, precision=76, scale=0)
    with pytest.raises(InvalidUsage):
        replace(meter, scale=19)
    with pytest.raises(InvalidUsage):
        replace(meter, transforms={"request": object()})  # type: ignore[dict-item]
    with pytest.raises(InvalidUsage):
        replace(
            meter,
            transforms={f"unit{index}": UnitTransform() for index in range(65)},
        )
    with pytest.raises(InvalidUsage):
        replace(meter, dimensions=tuple(DimensionSpec(f"d{index}") for index in range(33)))
    with pytest.raises(InvalidUsage):
        replace(meter, dimensions=(DimensionSpec("same"), DimensionSpec("same")))
    with pytest.raises(InvalidUsage):
        replace(meter, event_time_tolerance=timedelta(seconds=-1))
    with pytest.raises(InvalidUsage):
        replace(meter, event_time_tolerance=timedelta(milliseconds=1))
    with pytest.raises(InvalidUsage):
        replace(
            meter,
            active_from=datetime(2026, 2, 1, tzinfo=UTC),
            retired_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(InvalidUsage):
        MeterV1.from_mapping({})
    with pytest.raises(InvalidUsage):
        MeterV1.from_mapping(
            {
                **meter.to_dict(),
                "transforms": [],
            }
        )
    with pytest.raises(InvalidUsage):
        replace(event, meter_version=0)
    with pytest.raises(InvalidUsage):
        replace(event, correlation=object())  # type: ignore[arg-type]
    with pytest.raises(InvalidUsage):
        replace(event, original_value=Decimal("1"), original_unit=None)


def test_aggregate_and_correlation_mapping_validation(
    event: UsageEventV1,
) -> None:
    with pytest.raises(TypeError):
        UsageCorrelation.from_mapping("bad")
    with pytest.raises(ValueError, match="unknown"):
        UsageCorrelation.from_mapping({"unknown": "field"})
    with pytest.raises(InvalidUsage):
        UsageAggregateV1.from_mapping({})
    payload = {
        "aggregateId": "aggregate",
        "aggregateRevision": 1,
        "scope": "bad",
        "meterId": event.meter_id,
        "meterVersion": 1,
        "windowStart": event.window.to_dict()["start"],
        "windowEnd": event.window.to_dict()["end"],
        "dimensions": {},
        "total": "1",
        "eventCount": 1,
        "watermark": event.window.to_dict()["end"],
        "sourceFingerprint": "sha256:" + ("0" * 64),
        "createdAt": event.window.to_dict()["end"],
    }
    with pytest.raises(InvalidUsageResult):
        _single_record("not-a-record")
    with pytest.raises(InvalidUsage):
        UsageAggregateV1.from_mapping(payload)
    payload["scope"] = event.scope.to_dict()
    payload["total"] = "1e59"
    with pytest.raises(InvalidUsage):
        UsageAggregateV1.from_mapping(payload)


def test_query_operator_and_result_validation(
    executor: MemoryExecutor,
    event: UsageEventV1,
) -> None:
    where = _validate_where(
        {
            "value": {"ne": 0, "gt": -1, "lte": 5},
            "source": {"in": ["a", "b"], "notIn": ["c"]},
            "correctionOf": {"isNull": True},
        }
    )
    query = UsageQuery(
        executor,
        UsageResources().events,
        event.scope,
        event.window,
        where,
        select=("eventId",),
        order_by=(UsageOrder("eventId"),),
    )
    assert query.selecting("eventId", "value").select == ("eventId", "value")
    assert query.logical_plan.filter is not None
    with pytest.raises(InvalidUsage):
        _validate_where({"source": {"unsupported": "x"}})
    with pytest.raises(InvalidUsage):
        _validate_where({"source": {"in": []}})
    with pytest.raises(InvalidUsage):
        _validate_where({"source": {"isNull": "yes"}})
    with pytest.raises(InvalidUsage):
        replace(query, select=("eventId", "eventId"))
    with pytest.raises(InvalidUsage):
        replace(query, order_by=())
    with pytest.raises(InvalidUsage):
        replace(query, limit=0)
    with pytest.raises(InvalidUsageResult):
        _records("bad", 1)
    with pytest.raises(InvalidUsageResult):
        _records([{"a": 1}, {"a": 2}], 1)
    with pytest.raises(InvalidUsageResult):
        _records([1], 1)


def test_repository_and_control_validation(
    executor: MemoryExecutor,
    event: UsageEventV1,
) -> None:
    with pytest.raises(InvalidUsage):
        UsageResources(events=ResourceRef("evidence", "usage", "events"))
    resource = ResourceRef("structured", "usage", "same")
    with pytest.raises(InvalidUsage):
        UsageResources(meters=resource, events=resource)
    repository = UsageRepository(executor)
    with pytest.raises(UnknownMeter):
        repository.get_meter("missing", 1)
    with pytest.raises(InvalidUsage):
        repository.get_meter("missing", 0)
    with pytest.raises(InvalidUsage):
        repository.record_batch(())
    with pytest.raises(InvalidUsage, match="batch mode"):
        repository.record_batch((event,), mode="unknown")
    with pytest.raises(InvalidUsage):
        repository.scan_events(
            UsageQuery(executor, repository.resources.aggregates, event.scope, event.window)
        )
    with pytest.raises(InvalidUsage):
        repository.scan_events(
            UsageQuery(executor, repository.resources.events, event.scope, event.window),
            max_pages=0,
        )
    with pytest.raises(InvalidUsageResult):
        _storage_version({"_version": True})
    with pytest.raises(InvalidUsageResult):
        BatchItemResult.from_mapping(
            {
                "eventId": "event",
                "status": 1,
                "fingerprint": "sha256:" + ("0" * 64),
            }
        )
    now = datetime(2026, 8, 26, tzinfo=UTC)
    with pytest.raises(InvalidUsage):
        repository.acquire_claim(
            event.scope,
            "claim",
            "worker",
            expires_at=now,
            now=now,
        )


def test_retention_and_ready_usage_facade(event: UsageEventV1) -> None:
    with pytest.raises(InvalidUsage):
        RetentionInput(
            UsageResources().events,
            "usage-events",
            timedelta(days=1),
            correction_grace=timedelta(seconds=-1),
        )
    with pytest.raises(InvalidUsage):
        RetentionInput(
            UsageResources().events,
            "usage-events",
            timedelta(seconds=1, microseconds=1),
        )
    held = RetentionInput(
        UsageResources().events,
        "usage-events",
        timedelta(days=1),
        legal_hold_label="billing-hold",
    )
    assert held.legal_hold_label == "billing-hold"

    class ReadyMeridian(Meridian):
        @property
        def state(self) -> RuntimeState:
            return RuntimeState.READY

        def execute(self, expression):
            raise AssertionError(expression)

    runtime = ReadyMeridian.__new__(ReadyMeridian)
    usage = Usage(runtime)
    inputs = usage.retention_inputs(
        event_retention=timedelta(days=90),
        aggregate_retention=timedelta(days=365),
    )
    assert len(inputs) == 2
    spec = AggregationSpec(
        event.scope,
        event.meter_id,
        event.meter_version,
        event.window.start,
        timedelta(hours=1),
    )
    assert usage.aggregation(spec).spec == spec
