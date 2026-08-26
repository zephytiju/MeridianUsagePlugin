# SPDX-License-Identifier: Apache-2.0
"""Deterministic test fixtures exercising only public Meridian Expressions."""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest

from meridian_storage import (
    ConflictError,
    Expression,
    OperationResult,
    ResourceRef,
    ValidationError,
)
from meridian_storage.plugins.usage import (
    DimensionSpec,
    MeterV1,
    UnitTransform,
    UsageEventV1,
    UsageScope,
    UsageWindow,
)

_FP = "sha256:" + ("0" * 64)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_thaw(item) for item in value]
    return value


class MemoryExecutor:
    """A contract-shaped in-memory Expression executor, not an Adapter or Engine."""

    def __init__(self) -> None:
        self.records: dict[str, dict[tuple[object, ...], dict[str, object]]] = {}
        self.expressions: list[Expression] = []
        self.fail_event_ids: set[str] = set()
        self.transaction_entries = 0

    @staticmethod
    def _identity(name: str, data: Mapping[str, object]) -> tuple[object, ...]:
        fields = {
            "meters": ("meterId", "meterVersion"),
            "events": ("scopeFingerprint", "eventId"),
            "aggregates": ("scopeFingerprint", "aggregateVersionId"),
            "batches": ("scopeFingerprint", "batchId"),
            "checkpoints": ("scopeFingerprint", "checkpointId"),
            "claims": ("scopeFingerprint", "claimId"),
        }[name]
        return tuple(data[field] for field in fields)

    @staticmethod
    def _operator_matches(actual: object, operator: str, expected: object) -> bool:
        if operator == "eq":
            result = actual == expected
        elif operator == "ne":
            result = actual != expected
        elif operator == "lt":
            result = cast(str, actual) < cast(str, expected)
        elif operator == "lte":
            result = cast(str, actual) <= cast(str, expected)
        elif operator == "gt":
            result = cast(str, actual) > cast(str, expected)
        elif operator == "gte":
            result = cast(str, actual) >= cast(str, expected)
        elif operator == "in":
            result = actual in cast(Sequence[object], expected)
        elif operator == "notIn":
            result = actual not in cast(Sequence[object], expected)
        elif operator == "isNull":
            result = (actual is None) is expected
        else:
            raise AssertionError(f"unsupported test operator {operator!r}")
        return result

    @staticmethod
    def _matches(record: Mapping[str, object], where: Mapping[str, object]) -> bool:
        for name, predicate in where.items():
            actual = record.get(name)
            if not isinstance(predicate, Mapping):
                if actual != predicate:
                    return False
                continue
            for operator, expected in predicate.items():
                if not MemoryExecutor._operator_matches(actual, operator, expected):
                    return False
        return True

    @staticmethod
    def _result(expression: Expression, resource: ResourceRef, data: object) -> OperationResult:
        return OperationResult(
            data=cast(object, data),
            catalog="structured",
            operation_contract=f"meridian.structured.{expression.method}",
            operation_version="1.0.0",
            resources=(resource,),
            request_id=f"request-{len(expression.method)}",
            execution_id=f"execution-{len(expression.method)}",
            operation_fingerprint=expression.fingerprint,
            registry_fingerprint=_FP,
            capability_fingerprint=_FP,
            provenance={"executor": "memory-contract"},
        )

    def execute(self, expression: Expression) -> OperationResult:
        self.expressions.append(expression)
        arguments = expression.arguments
        resource = ResourceRef.parse(cast(Mapping[str, object], arguments["resource"]))
        table = self.records.setdefault(resource.name, {})
        if expression.method == "get":
            where = cast(Mapping[str, object], arguments["where"])
            item = next((record for record in table.values() if self._matches(record, where)), None)
            return self._result(expression, resource, None if item is None else {"record": item})
        if expression.method == "put":
            data = cast(dict[str, object], _thaw(arguments["data"]))
            if resource.name == "events" and data.get("eventId") in self.fail_event_ids:
                raise ValidationError("TEST_USAGE_WRITE_FAILURE", "injected event write failure")
            key = self._identity(resource.name, data)
            existing = table.get(key)
            expected = arguments.get("expectedVersion")
            if expected is not None:
                current_version = 0 if existing is None else cast(int, existing["_version"])
                if expected != current_version:
                    raise ConflictError("TEST_CAS_CONFLICT", "conditional write conflict")
            elif existing is not None:
                raise ConflictError("TEST_IMMUTABLE_CONFLICT", "immutable identity conflict")
            data["_version"] = 1 if existing is None else cast(int, existing["_version"]) + 1
            table[key] = data
            return self._result(expression, resource, {"record": data})
        if expression.method == "query":
            where = cast(Mapping[str, object], arguments["where"])
            records = [record for record in table.values() if self._matches(record, where)]
            order = cast(Sequence[Mapping[str, object]], arguments["orderBy"])
            for item in reversed(order):
                name = cast(str, item["field"])
                reverse = item.get("direction", "asc") == "desc"
                records.sort(
                    key=lambda record: (record.get(name) is None, record.get(name)),
                    reverse=reverse,
                )
            offset = int(cast(str, arguments.get("cursor", "0")))
            limit = cast(int, arguments["limit"])
            page = records[offset : offset + limit]
            selected = cast(Sequence[str], arguments["select"])
            if selected:
                page = [{name: record.get(name) for name in selected} for record in page]
            next_cursor = str(offset + limit) if offset + limit < len(records) else None
            return self._result(
                expression,
                resource,
                {"items": page, "cursor": next_cursor},
            )
        raise AssertionError(f"unsupported test Expression {expression.method!r}")

    @contextmanager
    def transaction(self, resource: ResourceRef) -> Iterator[object]:
        assert resource.catalog == "structured"
        snapshot = copy.deepcopy(self.records)
        self.transaction_entries += 1
        try:
            yield self
        except BaseException:
            self.records = snapshot
            raise


@pytest.fixture
def scope() -> UsageScope:
    return UsageScope({"tenant": "acme", "environment": "test"})


@pytest.fixture
def meter() -> MeterV1:
    return MeterV1(
        meter_id="api.requests",
        version=1,
        quantity="requests",
        canonical_unit="request",
        transforms={
            "kilorequest": UnitTransform(Decimal("1000")),
        },
        dimensions=(
            DimensionSpec("region", required=True, max_cardinality=100),
            DimensionSpec("plan"),
        ),
        precision=38,
        scale=6,
        active_from=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def event(scope: UsageScope) -> UsageEventV1:
    return UsageEventV1(
        event_id="event-001",
        scope=scope,
        subject_id="account-42",
        meter_id="api.requests",
        meter_version=1,
        window=UsageWindow(
            datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 26, 10, 1, tzinfo=UTC),
        ),
        value=Decimal("2"),
        unit="kilorequest",
        dimensions={"region": "us-west", "plan": "pro"},
        source="publisher",
        recorded_at=datetime(2026, 8, 26, 10, 2, tzinfo=UTC),
    )


@pytest.fixture
def executor() -> MemoryExecutor:
    return MemoryExecutor()
