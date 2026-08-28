# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from importlib.metadata import metadata, requires, version

from conftest import MemoryExecutor
from meridian_storage.evidence import Correlation
from meridian_storage.plugins.usage import (
    UsageAggregateV1,
    UsageEventV1,
    UsageQuery,
    UsageRepository,
    event_set_fingerprint,
)
from meridian_storage.query import QueryOperation
from meridian_storage.semantics import StructuredCatalogProvider, StructuredCatalogSurface


def test_distribution_uses_only_exact_released_runtime_contracts() -> None:
    assert version("meridian-plugin-usage") == "1.0.2"
    project = metadata("meridian-plugin-usage")
    assert project["License-Expression"] == "Apache-2.0"
    dependencies = requires("meridian-plugin-usage") or []
    runtime = {item for item in dependencies if "extra ==" not in item}
    assert runtime == {
        "meridian-plugin-observability==1.0.0",
        "meridian-storage-core==1.0.0",
        "meridian-storage-evidence==1.0.0",
        "meridian-storage-query==1.0.0",
        "meridian-storage-semantics==1.0.0",
    }


def test_mapping_first_query_normalizes_through_released_semantics(
    executor: MemoryExecutor,
    event: UsageEventV1,
) -> None:
    query = UsageQuery(
        executor,
        UsageRepository(executor).resources.events,
        event.scope,
        event.window,
        {"meterId": event.meter_id},
    )
    operation = StructuredCatalogProvider().normalize(query.expression)
    assert operation.catalog == "structured"
    assert operation.operation_contract == "meridian.structured.query"
    assert operation.resources == (query.resource,)
    assert operation.input["where"]["scopeFingerprint"] == event.scope.fingerprint
    assert isinstance(query.logical_plan, QueryOperation)
    assert query.logical_plan.to_dict()["formatVersion"] == "meridian.operation.query.v1"


def test_mapping_first_put_normalizes_without_adapter_concepts(
    executor: MemoryExecutor,
    meter,
    event: UsageEventV1,
) -> None:
    resources = UsageRepository(executor).resources
    expression = StructuredCatalogSurface().put(
        resource=resources.meters.to_dict(),
        data=meter.to_dict(),
        expected_version=0,
    )
    operation = StructuredCatalogProvider().normalize(expression)
    assert operation.operation_contract == "meridian.structured.put"
    assert operation.input["expectedVersion"] == 0
    assert "adapter" not in str(operation.to_dict()).lower()
    assert "engine" not in str(operation.to_dict()).lower()
    event_expression = StructuredCatalogSurface().put(
        resource=resources.events.to_dict(),
        data=event.normalized(meter).to_dict(),
    )
    event_operation = StructuredCatalogProvider().normalize(event_expression)
    assert event_operation.operation_contract == "meridian.structured.put"
    assert "expectedVersion" not in event_operation.input


def test_evidence_and_external_cost_consumer_use_public_records_only(
    event: UsageEventV1,
) -> None:
    normalized = replace(
        event,
        value=Decimal("2.500000"),
        unit="request",
        original_value=event.value,
        original_unit=event.unit,
    )
    aggregate = UsageAggregateV1(
        "aggregate-cost-input",
        1,
        event.scope,
        event.meter_id,
        event.meter_version,
        event.window,
        {"region": "us-west"},
        normalized.value,
        1,
        event.window.end,
        event_set_fingerprint((normalized,)),
        event.recorded_at + timedelta(minutes=1),
    )

    def external_cost_input(value: UsageAggregateV1) -> tuple[str, int, Decimal]:
        return value.meter_id, value.meter_version, value.total

    assert external_cost_input(aggregate) == ("api.requests", 1, Decimal("2.500000"))
    assert "price" not in aggregate.to_dict()
    assert "cost" not in aggregate.to_dict()
    assert isinstance(aggregate.correlation.to_evidence(), Correlation)
