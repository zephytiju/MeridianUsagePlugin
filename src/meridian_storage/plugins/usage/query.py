# SPDX-License-Identifier: Apache-2.0
"""Bounded mapping-first Usage queries with released logical plans."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, cast

from meridian_storage import Expression, OperationResult, ResourceRef
from meridian_storage.query import (
    BooleanExpression,
    PageSpec,
    Projection,
    QueryOperation,
    QueryTarget,
    ResultSpec,
    SafetyBudget,
    Sort,
    ValueExpression,
    field,
)
from meridian_storage.semantics import StructuredCatalogSurface

from ._canonical import iso_datetime, json_value, logical_name, utc_datetime
from .errors import InvalidUsage, InvalidUsageResult
from .models import UsageScope, UsageWindow

_MAX_PAGE_SIZE = 500
_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "notIn", "isNull"})


class MeridianExecutor(Protocol):
    def execute(self, expression: Expression) -> OperationResult: ...


@dataclass(frozen=True, slots=True)
class UsageOrder:
    field: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", logical_name(self.field, "order field"))
        if self.direction not in {"asc", "desc"}:
            raise InvalidUsage("query order direction must be asc or desc")

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "direction": self.direction}


def _validate_where(values: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(values, Mapping) or len(values) > 32:
        raise InvalidUsage("query predicates must be a mapping with at most 32 fields")
    result: dict[str, object] = {}
    for raw_name, raw_value in values.items():
        name = logical_name(raw_name, "query field")
        if isinstance(raw_value, Mapping):
            if not raw_value or set(raw_value) - _OPERATORS:
                raise InvalidUsage("query predicate contains an unsupported operator")
            operators: dict[str, object] = {}
            for raw_operator, candidate in raw_value.items():
                operator = cast(str, raw_operator)
                if operator in {"in", "notIn"}:
                    if (
                        not isinstance(candidate, Sequence)
                        or isinstance(candidate, str | bytes | bytearray)
                        or not 1 <= len(candidate) <= 100
                    ):
                        raise InvalidUsage("membership predicates require 1 to 100 values")
                    operators[operator] = tuple(
                        json_value(item, "query predicate") for item in candidate
                    )
                elif operator == "isNull":
                    if not isinstance(candidate, bool):
                        raise InvalidUsage("isNull query predicates require a boolean")
                    operators[operator] = candidate
                else:
                    operators[operator] = json_value(candidate, "query predicate")
            result[name] = MappingProxyType(dict(sorted(operators.items())))
        else:
            result[name] = json_value(raw_value, "query predicate")
    return MappingProxyType(dict(sorted(result.items())))


def _logical_predicate(where: Mapping[str, object]) -> ValueExpression | None:
    predicates: list[ValueExpression] = []
    for name, value in sorted(where.items()):
        operand = field(name)
        if not isinstance(value, Mapping):
            predicates.append(operand.eq(value))
            continue
        for operator, candidate in sorted(value.items()):
            if operator == "in":
                predicates.append(operand.in_(cast(Sequence[object], candidate)))
            elif operator == "notIn":
                expression = operand.in_(cast(Sequence[object], candidate))
                predicates.append(type(expression)(expression.operand, expression.values, True))
            elif operator == "isNull":
                predicates.append(operand.is_null(cast(bool, candidate)))
            else:
                predicates.append(getattr(operand, operator)(candidate))
    if not predicates:
        return None
    if len(predicates) == 1:
        return predicates[0]
    return BooleanExpression("and", tuple(predicates))


def _records(data: object, maximum: int) -> tuple[Mapping[str, object], ...]:
    selected = data
    if isinstance(data, Mapping):
        for key in ("items", "records", "data"):
            if key in data:
                selected = data[key]
                break
    if not isinstance(selected, Sequence) or isinstance(selected, str | bytes | bytearray):
        raise InvalidUsageResult("Usage query returned an invalid record collection")
    if len(selected) > maximum:
        raise InvalidUsageResult("Usage query returned more records than its page limit")
    result: list[Mapping[str, object]] = []
    for item in selected:
        if not isinstance(item, Mapping):
            raise InvalidUsageResult("Usage query returned a non-record item")
        result.append(MappingProxyType(dict(item)))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class UsageQueryResult:
    items: tuple[Mapping[str, object], ...]
    cursor: str | None
    operation_result: OperationResult

    @property
    def provenance(self) -> Mapping[str, str]:
        return cast(Mapping[str, str], self.operation_result.provenance)


@dataclass(frozen=True, slots=True)
class UsageQuery:
    """A bounded query over one logical structured Usage Resource."""

    _executor: MeridianExecutor
    resource: ResourceRef
    scope: UsageScope
    window: UsageWindow
    where: Mapping[str, object] = dataclass_field(default_factory=dict)
    select: tuple[str, ...] = ()
    order_by: tuple[UsageOrder, ...] = (
        UsageOrder("windowStart"),
        UsageOrder("eventId"),
    )
    limit: int = 100
    cursor: str | None = None

    def __post_init__(self) -> None:
        try:
            resource = ResourceRef.parse(self.resource, catalog="structured")
        except (TypeError, ValueError) as exc:
            raise InvalidUsage("Usage queries require a logical structured Resource") from exc
        scope = UsageScope.parse(self.scope)
        if not isinstance(self.window, UsageWindow):
            raise InvalidUsage("Usage queries require a closed-open UsageWindow")
        where = _validate_where(self.where)
        reserved = {"scopeFingerprint", "windowStart"}
        if reserved & set(where):
            raise InvalidUsage("scopeFingerprint and windowStart predicates are derived inputs")
        selected = tuple(logical_name(item, "selected field") for item in self.select)
        if len(set(selected)) != len(selected):
            raise InvalidUsage("selected fields must be unique")
        order = tuple(self.order_by)
        if not order or any(not isinstance(item, UsageOrder) for item in order):
            raise InvalidUsage("query ordering requires at least one UsageOrder")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not 1 <= self.limit <= _MAX_PAGE_SIZE
        ):
            raise InvalidUsage(f"query page size must be between 1 and {_MAX_PAGE_SIZE}")
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or not self.cursor
            or len(self.cursor.encode("utf-8")) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in self.cursor)
        ):
            raise InvalidUsage("query cursor must be an opaque bounded token")
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "where", where)
        object.__setattr__(self, "select", selected)
        object.__setattr__(self, "order_by", order)

    @property
    def predicates(self) -> Mapping[str, object]:
        result = dict(self.where)
        result["scopeFingerprint"] = self.scope.fingerprint
        result["windowStart"] = {
            "gte": iso_datetime(self.window.start),
            "lt": iso_datetime(self.window.end),
        }
        return MappingProxyType(dict(sorted(result.items())))

    @property
    def expression(self) -> Expression:
        return StructuredCatalogSurface().query(
            resource=self.resource.to_dict(),
            where=self.predicates,
            select=self.select,
            order_by=tuple(item.to_dict() for item in self.order_by),
            limit=self.limit,
            cursor=self.cursor,
        )

    @property
    def logical_plan(self) -> QueryOperation:
        projections = tuple(Projection(field(name)) for name in self.select)
        sorts = tuple(Sort(field(item.field), item.direction) for item in self.order_by)
        return QueryOperation(
            catalog="structured",
            targets=(QueryTarget(self.resource),),
            operation="scan",
            result=ResultSpec("records", projections),
            filter=_logical_predicate(self.predicates),
            order=sorts,
            page=PageSpec(self.limit, self.cursor),
            consistency="eventual",
            budget=SafetyBudget(max_result_values=self.limit),
            extensions={"org.meridian.usage/query": "1.0.0"},
        )

    @property
    def fingerprint(self) -> str:
        return self.logical_plan.fingerprint

    def page(self, *, limit: int | None = None, cursor: str | None = None) -> UsageQuery:
        return replace(self, limit=self.limit if limit is None else limit, cursor=cursor)

    def selecting(self, *fields: str) -> UsageQuery:
        return replace(self, select=tuple(fields))

    def execute(self) -> UsageQueryResult:
        result = self._executor.execute(self.expression)
        cursor: str | None = None
        if isinstance(result.data, Mapping):
            candidate = result.data.get("cursor", result.data.get("nextCursor"))
            if candidate is not None and not isinstance(candidate, str):
                raise InvalidUsageResult("Usage query returned an invalid cursor")
            cursor = candidate
        return UsageQueryResult(_records(result.data, self.limit), cursor, result)


@dataclass(frozen=True, slots=True)
class UsageQueries:
    _executor: MeridianExecutor
    events_resource: ResourceRef
    aggregates_resource: ResourceRef

    def events(
        self,
        scope: UsageScope | Mapping[object, object],
        start: datetime,
        end: datetime,
        *,
        where: Mapping[str, object] | None = None,
    ) -> UsageQuery:
        return UsageQuery(
            self._executor,
            self.events_resource,
            UsageScope.parse(scope),
            UsageWindow(utc_datetime(start, "query start"), utc_datetime(end, "query end")),
            where or {},
        )

    def aggregates(
        self,
        scope: UsageScope | Mapping[object, object],
        start: datetime,
        end: datetime,
        *,
        where: Mapping[str, object] | None = None,
    ) -> UsageQuery:
        return UsageQuery(
            self._executor,
            self.aggregates_resource,
            UsageScope.parse(scope),
            UsageWindow(utc_datetime(start, "query start"), utc_datetime(end, "query end")),
            where or {},
            order_by=(
                UsageOrder("windowStart"),
                UsageOrder("aggregateId"),
                UsageOrder("aggregateRevision"),
            ),
        )


__all__ = [
    "MeridianExecutor",
    "UsageOrder",
    "UsageQueries",
    "UsageQuery",
    "UsageQueryResult",
]
