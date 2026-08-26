# SPDX-License-Identifier: Apache-2.0
"""Safe Meridian, OpenTelemetry, and Evidence correlation for Usage records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from meridian_storage import OperationResult
from meridian_storage.evidence import Correlation
from meridian_storage.plugins.observability.context import ContextPolicy, correlation_attributes

from ._canonical import JsonValue


@dataclass(frozen=True, slots=True)
class UsageCorrelation:
    """Non-secret correlation values safe to preserve on immutable Usage records."""

    trace_id: str | None = None
    span_id: str | None = None
    request_id: str | None = None
    execution_id: str | None = None
    operation_fingerprint: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        validated = self.to_evidence()
        for name in (
            "trace_id",
            "span_id",
            "request_id",
            "execution_id",
            "operation_fingerprint",
            "correlation_id",
        ):
            object.__setattr__(self, name, getattr(validated, name))

    @classmethod
    def capture(cls) -> UsageCorrelation:
        """Capture only fields approved by the released Observability policy."""

        values = correlation_attributes(ContextPolicy())
        return cls(
            trace_id=cast(str | None, values.get("trace_id")),
            span_id=cast(str | None, values.get("span_id")),
            request_id=cast(str | None, values.get("meridian.request.id")),
            correlation_id=cast(str | None, values.get("meridian.correlation.id")),
        )

    @classmethod
    def from_mapping(cls, value: object) -> UsageCorrelation:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("correlation must be a mapping")
        mapping = dict(value)
        allowed = {
            "traceId",
            "spanId",
            "requestId",
            "executionId",
            "operationFingerprint",
            "correlationId",
        }
        if set(mapping) - allowed:
            raise ValueError("correlation contains unknown fields")
        return cls(
            trace_id=cast(str | None, mapping.get("traceId")),
            span_id=cast(str | None, mapping.get("spanId")),
            request_id=cast(str | None, mapping.get("requestId")),
            execution_id=cast(str | None, mapping.get("executionId")),
            operation_fingerprint=cast(str | None, mapping.get("operationFingerprint")),
            correlation_id=cast(str | None, mapping.get("correlationId")),
        )

    def with_result(self, result: OperationResult) -> UsageCorrelation:
        """Attach post-execution Meridian evidence to a receipt, not the event."""

        return replace(
            self,
            request_id=result.request_id,
            execution_id=result.execution_id,
            operation_fingerprint=result.operation_fingerprint,
        )

    def to_evidence(self) -> Correlation:
        return Correlation(
            trace_id=self.trace_id,
            span_id=self.span_id,
            request_id=self.request_id,
            execution_id=self.execution_id,
            operation_fingerprint=self.operation_fingerprint,
            correlation_id=self.correlation_id,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return self.to_evidence().to_dict()

    @property
    def empty(self) -> bool:
        return not self.to_dict()


__all__ = ["UsageCorrelation"]
