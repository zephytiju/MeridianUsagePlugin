# SPDX-License-Identifier: Apache-2.0
"""Immutable generic Usage V1 models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from types import MappingProxyType
from typing import cast

from ._canonical import (
    bounded_text,
    decimal_text,
    decimal_value,
    fingerprint,
    iso_datetime,
    logical_name,
    parse_datetime,
    require_fingerprint,
    string_map,
    token,
    unit_name,
    utc_datetime,
)
from .correlation import UsageCorrelation
from .errors import (
    DecimalOverflow,
    DimensionViolation,
    InactiveMeter,
    InvalidCorrection,
    InvalidUsage,
    UnitMismatch,
)

_SCHEMA_VERSION = "1.0.0"


def _storage_decimal(value: object, name: str) -> Decimal:
    selected = decimal_value(value, name)
    normalized = selected if selected.is_zero() else selected.normalize()
    exponent = cast(int, normalized.as_tuple().exponent)
    fractional_digits = max(0, -exponent)
    integer_digits = 1 if normalized.is_zero() else max(1, normalized.adjusted() + 1)
    if fractional_digits > 18 or integer_digits > 58:
        raise DecimalOverflow(f"{name} must fit the released Decimal(76, 18) Usage schema")
    return selected


@dataclass(frozen=True, slots=True)
class UsageScope:
    """A generic, physically isolatable set of logical scope labels."""

    values: Mapping[str, str]

    def __post_init__(self) -> None:
        selected = string_map(self.values, "scope", maximum_entries=16, value_maximum=256)
        if not selected:
            raise InvalidUsage("scope must contain at least one logical label")
        object.__setattr__(self, "values", selected)

    @property
    def fingerprint(self) -> str:
        return fingerprint(dict(self.values))

    def to_dict(self) -> dict[str, str]:
        return dict(self.values)

    @classmethod
    def parse(cls, value: UsageScope | Mapping[object, object]) -> UsageScope:
        return value if isinstance(value, cls) else cls(cast(Mapping[str, str], value))


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """A closed-open UTC interval where start is included and end is excluded."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = utc_datetime(self.start, "window start")
        end = utc_datetime(self.end, "window end")
        if start >= end:
            raise InvalidUsage(
                "usage windows must be non-empty closed-open intervals",
                requirement="usage.window.half-open",
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, instant: datetime) -> bool:
        selected = utc_datetime(instant, "instant")
        return self.start <= selected < self.end

    def overlaps(self, other: UsageWindow) -> bool:
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict[str, str]:
        return {"start": iso_datetime(self.start), "end": iso_datetime(self.end)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UsageWindow:
        if set(value) != {"start", "end"}:
            raise InvalidUsage("window requires exactly start and end")
        return cls(
            parse_datetime(value["start"], "window start"),
            parse_datetime(value["end"], "window end"),
        )

    @classmethod
    def bucket(cls, instant: datetime, size: timedelta) -> UsageWindow:
        selected = utc_datetime(instant, "bucket instant")
        seconds = size.total_seconds()
        if size <= timedelta(0) or not seconds.is_integer():
            raise InvalidUsage("bucket size must be a positive whole number of seconds")
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        delta = selected - epoch
        elapsed_microseconds = (
            delta.days * 86_400 + delta.seconds
        ) * 1_000_000 + delta.microseconds
        width_microseconds = int(seconds) * 1_000_000
        bucket_microseconds = (elapsed_microseconds // width_microseconds) * width_microseconds
        start = epoch + timedelta(microseconds=bucket_microseconds)
        return cls(start, start + size)


@dataclass(frozen=True, slots=True)
class UnitTransform:
    """An exact affine transform into a meter's canonical unit."""

    scale: Decimal = Decimal(1)
    offset: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        scale = decimal_value(self.scale, "unit scale")
        offset = decimal_value(self.offset, "unit offset")
        if scale <= 0:
            raise InvalidUsage("unit transform scale must be positive")
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "offset", offset)

    def apply(self, value: Decimal) -> Decimal:
        selected = decimal_value(value)
        operand_digits = len(selected.as_tuple().digits)
        scale_digits = len(self.scale.as_tuple().digits)
        offset_digits = len(self.offset.as_tuple().digits)
        exponent_span = (
            abs(selected.adjusted()) + abs(self.scale.adjusted()) + abs(self.offset.adjusted())
        )
        with localcontext() as context:
            context.prec = max(
                100,
                operand_digits + scale_digits + offset_digits + exponent_span + 20,
            )
            context.traps[Inexact] = True
            return selected * self.scale + self.offset

    def to_dict(self) -> dict[str, str]:
        return {"scale": decimal_text(self.scale), "offset": decimal_text(self.offset)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UnitTransform:
        if set(value) - {"scale", "offset"} or "scale" not in value:
            raise InvalidUsage("unit transform requires scale and optional offset")
        return cls(
            decimal_value(value["scale"], "unit scale"),
            decimal_value(value.get("offset", "0"), "unit offset"),
        )


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    name: str
    required: bool = False
    max_cardinality: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", logical_name(self.name, "dimension name"))
        if not isinstance(self.required, bool):
            raise InvalidUsage("dimension required must be boolean")
        if self.max_cardinality is not None and (
            isinstance(self.max_cardinality, bool)
            or not isinstance(self.max_cardinality, int)
            or self.max_cardinality < 1
        ):
            raise InvalidUsage("dimension max_cardinality must be a positive integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "maxCardinality": self.max_cardinality,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DimensionSpec:
        if "name" not in value or set(value) - {"name", "required", "maxCardinality"}:
            raise InvalidUsage("dimension specification contains unknown or missing fields")
        return cls(
            cast(str, value["name"]),
            cast(bool, value.get("required", False)),
            cast(int | None, value.get("maxCardinality")),
        )


def _meter_transforms(values: object, canonical_unit: str) -> dict[str, UnitTransform]:
    if not isinstance(values, Mapping) or len(values) > 64:
        raise InvalidUsage("meter transforms must be a mapping with at most 64 entries")
    transforms: dict[str, UnitTransform] = {}
    for raw_unit, raw_transform in values.items():
        selected_unit = unit_name(raw_unit)
        if not isinstance(raw_transform, UnitTransform):
            raise InvalidUsage("meter transforms must contain UnitTransform values")
        transforms[selected_unit] = raw_transform
    identity = UnitTransform()
    declared_identity = transforms.get(canonical_unit)
    if declared_identity is not None and declared_identity != identity:
        raise InvalidUsage("the canonical unit transform must be the identity")
    transforms[canonical_unit] = identity
    if len(transforms) > 64:
        raise InvalidUsage("meter transforms must declare at most 64 accepted units")
    return transforms


def _meter_dimensions(values: object) -> tuple[DimensionSpec, ...]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, str | bytes | bytearray)
        or len(values) > 32
    ):
        raise InvalidUsage("meter dimensions must contain at most 32 entries")
    dimensions = tuple(values)
    if any(not isinstance(item, DimensionSpec) for item in dimensions):
        raise InvalidUsage("meter dimensions must contain DimensionSpec values")
    selected = tuple(
        sorted(cast(tuple[DimensionSpec, ...], dimensions), key=lambda item: item.name)
    )
    if len({item.name for item in selected}) != len(selected):
        raise InvalidUsage("meter dimension names must be unique")
    return selected


@dataclass(frozen=True, slots=True)
class MeterV1:
    """One immutable, explicitly versioned measurement contract."""

    meter_id: str
    version: int
    quantity: str
    canonical_unit: str
    transforms: Mapping[str, UnitTransform] = field(default_factory=dict)
    dimensions: tuple[DimensionSpec, ...] = ()
    precision: int = 38
    scale: int = 12
    event_time_tolerance: timedelta = timedelta(0)
    active_from: datetime = field(default_factory=lambda: datetime(1970, 1, 1, tzinfo=UTC))
    retired_at: datetime | None = None
    description: str | None = None
    schema_version: str = field(default=_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        meter_id = logical_name(self.meter_id, "meter_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InvalidUsage("meter version must be a positive integer")
        quantity = logical_name(self.quantity, "quantity")
        canonical_unit = unit_name(self.canonical_unit)
        if (
            isinstance(self.precision, bool)
            or not isinstance(self.precision, int)
            or not 1 <= self.precision <= 76
            or isinstance(self.scale, bool)
            or not isinstance(self.scale, int)
            or not 0 <= self.scale <= min(self.precision, 18)
            or self.precision - self.scale > 58
        ):
            raise InvalidUsage("meter Decimal precision/scale must fit Decimal(76, 18)")
        transforms = _meter_transforms(self.transforms, canonical_unit)
        dimensions = _meter_dimensions(self.dimensions)
        if not isinstance(self.event_time_tolerance, timedelta):
            raise InvalidUsage("meter event_time_tolerance must be a timedelta")
        if self.event_time_tolerance < timedelta(0):
            raise InvalidUsage("meter event_time_tolerance cannot be negative")
        if self.event_time_tolerance.microseconds:
            raise InvalidUsage("meter event_time_tolerance must use whole seconds")
        active_from = utc_datetime(self.active_from, "active_from")
        retired_at = (
            None if self.retired_at is None else utc_datetime(self.retired_at, "retired_at")
        )
        if retired_at is not None and retired_at <= active_from:
            raise InvalidUsage("retired_at must follow active_from")
        description = (
            None
            if self.description is None
            else bounded_text(self.description, "description", maximum=2048)
        )
        object.__setattr__(self, "meter_id", meter_id)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "canonical_unit", canonical_unit)
        object.__setattr__(self, "transforms", MappingProxyType(dict(sorted(transforms.items()))))
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "active_from", active_from)
        object.__setattr__(self, "retired_at", retired_at)
        object.__setattr__(self, "description", description)

    @property
    def ref(self) -> str:
        return f"{self.meter_id}@{self.version}"

    @property
    def dimension_map(self) -> Mapping[str, DimensionSpec]:
        return MappingProxyType({item.name: item for item in self.dimensions})

    def is_active(self, instant: datetime) -> bool:
        selected = utc_datetime(instant, "meter instant")
        return self.active_from <= selected and (
            self.retired_at is None or selected < self.retired_at
        )

    def validate_event_time(self, window: UsageWindow, recorded_at: datetime) -> None:
        """Reject observations farther in the future than the Meter permits."""

        selected_recorded_at = utc_datetime(recorded_at, "recorded_at")
        if window.end > selected_recorded_at and (
            window.end - selected_recorded_at > self.event_time_tolerance
        ):
            raise InvalidUsage(
                "usage event window exceeds the meter event-time tolerance",
                requirement="usage.meter.event-time-tolerance",
            )

    def accepts_window(self, window: UsageWindow) -> bool:
        return self.is_active(window.start) and (
            self.retired_at is None or window.end <= self.retired_at
        )

    def validate_dimensions(self, values: Mapping[str, str]) -> Mapping[str, str]:
        selected = string_map(values, "dimensions", maximum_entries=32)
        declared = self.dimension_map
        unknown = sorted(set(selected) - set(declared))
        missing = sorted(
            item.name for item in self.dimensions if item.required and item.name not in selected
        )
        if unknown:
            raise DimensionViolation(f"undeclared dimensions: {unknown!r}", self.ref)
        if missing:
            raise DimensionViolation(f"required dimensions are missing: {missing!r}", self.ref)
        return selected

    def normalize(self, value: object, unit: str) -> Decimal:
        selected_unit = unit_name(unit)
        try:
            transform = self.transforms[selected_unit]
        except KeyError as exc:
            raise UnitMismatch(selected_unit, self.ref) from exc
        return self._fit(transform.apply(decimal_value(value)))

    def _fit(self, value: Decimal) -> Decimal:
        quantum = Decimal(1).scaleb(-self.scale)
        try:
            with localcontext() as context:
                context.prec = max(100, self.precision + 20)
                context.traps[Inexact] = True
                quantized = value.quantize(quantum)
        except (Inexact, InvalidOperation) as exc:
            raise DecimalOverflow(
                f"Decimal value cannot be represented exactly at scale {self.scale}"
            ) from exc
        integer_digits = 1 if quantized.is_zero() else max(1, quantized.adjusted() + 1)
        if integer_digits + self.scale > self.precision:
            raise DecimalOverflow(
                f"Decimal value exceeds meter precision {self.precision} and scale {self.scale}"
            )
        return Decimal(0).quantize(quantum) if quantized.is_zero() else quantized

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "meterId": self.meter_id,
            "meterVersion": self.version,
            "quantity": self.quantity,
            "canonicalUnit": self.canonical_unit,
            "transforms": {
                name: transform.to_dict() for name, transform in self.transforms.items()
            },
            "dimensions": [item.to_dict() for item in self.dimensions],
            "precision": self.precision,
            "scale": self.scale,
            "eventTimeToleranceSeconds": int(self.event_time_tolerance.total_seconds()),
            "activeFrom": iso_datetime(self.active_from),
            "retiredAt": None if self.retired_at is None else iso_datetime(self.retired_at),
            "description": self.description,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> MeterV1:
        required = {
            "meterId",
            "meterVersion",
            "quantity",
            "canonicalUnit",
            "transforms",
            "dimensions",
            "precision",
            "scale",
            "eventTimeToleranceSeconds",
            "activeFrom",
        }
        if required - set(value):
            raise InvalidUsage("meter record is missing required fields")
        transforms = value["transforms"]
        dimensions = value["dimensions"]
        event_time_tolerance_seconds = value["eventTimeToleranceSeconds"]
        if (
            not isinstance(transforms, Mapping)
            or not isinstance(dimensions, Sequence)
            or isinstance(dimensions, str | bytes | bytearray)
            or isinstance(event_time_tolerance_seconds, bool)
            or not isinstance(event_time_tolerance_seconds, int)
        ):
            raise InvalidUsage("meter transforms, dimensions, or event-time tolerance are invalid")
        return cls(
            meter_id=cast(str, value["meterId"]),
            version=cast(int, value["meterVersion"]),
            quantity=cast(str, value["quantity"]),
            canonical_unit=cast(str, value["canonicalUnit"]),
            transforms={
                cast(str, name): UnitTransform.from_mapping(cast(Mapping[str, object], item))
                for name, item in transforms.items()
            },
            dimensions=tuple(
                DimensionSpec.from_mapping(cast(Mapping[str, object], item)) for item in dimensions
            ),
            precision=cast(int, value["precision"]),
            scale=cast(int, value["scale"]),
            event_time_tolerance=timedelta(seconds=event_time_tolerance_seconds),
            active_from=parse_datetime(value["activeFrom"], "active_from"),
            retired_at=(
                None
                if value.get("retiredAt") is None
                else parse_datetime(value["retiredAt"], "retired_at")
            ),
            description=cast(str | None, value.get("description")),
        )


@dataclass(frozen=True, slots=True)
class UsageEventV1:
    """One immutable usage fact or signed correction delta."""

    event_id: str
    scope: UsageScope
    subject_id: str
    meter_id: str
    meter_version: int
    window: UsageWindow
    value: Decimal
    unit: str
    dimensions: Mapping[str, str] = field(default_factory=dict)
    source: str = "application"
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    correction_of: str | None = None
    correction_reason: str | None = None
    correlation: UsageCorrelation = field(default_factory=UsageCorrelation)
    provenance: Mapping[str, str] = field(default_factory=dict)
    original_value: Decimal | None = None
    original_unit: str | None = None
    schema_version: str = field(default=_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        event_id = token(self.event_id, "event_id")
        scope = UsageScope.parse(self.scope)
        subject_id = bounded_text(self.subject_id, "subject_id", 512)
        meter_id = logical_name(self.meter_id, "meter_id")
        if (
            isinstance(self.meter_version, bool)
            or not isinstance(self.meter_version, int)
            or self.meter_version < 1
        ):
            raise InvalidUsage("meter_version must be a positive integer")
        if not isinstance(self.window, UsageWindow):
            raise InvalidUsage("window must be a UsageWindow")
        value = decimal_value(self.value)
        unit = unit_name(self.unit)
        dimensions = string_map(self.dimensions, "dimensions", maximum_entries=32)
        source = logical_name(self.source, "source")
        recorded_at = utc_datetime(self.recorded_at, "recorded_at")
        correction_of = (
            None if self.correction_of is None else token(self.correction_of, "correction_of")
        )
        correction_reason = (
            None
            if self.correction_reason is None
            else bounded_text(self.correction_reason, "correction_reason", 1024)
        )
        if correction_of is None:
            if value < 0:
                raise InvalidCorrection("ordinary usage events cannot contain negative values")
            if correction_reason is not None:
                raise InvalidCorrection("correction_reason requires correction_of")
        elif correction_of == event_id:
            raise InvalidCorrection("a usage event cannot correct itself")
        elif correction_reason is None:
            raise InvalidCorrection("correction events require correction_reason")
        if not isinstance(self.correlation, UsageCorrelation):
            raise InvalidUsage("correlation must be UsageCorrelation")
        provenance = string_map(
            self.provenance,
            "provenance",
            maximum_entries=32,
            key_names=False,
            value_maximum=2048,
        )
        original_value = (
            None
            if self.original_value is None
            else decimal_value(self.original_value, "original_value")
        )
        original_unit = None if self.original_unit is None else unit_name(self.original_unit)
        if (original_value is None) != (original_unit is None):
            raise InvalidUsage("original_value and original_unit must be supplied together")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "meter_id", meter_id)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "correction_of", correction_of)
        object.__setattr__(self, "correction_reason", correction_reason)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "original_value", original_value)
        object.__setattr__(self, "original_unit", original_unit)

    @property
    def meter_ref(self) -> str:
        return f"{self.meter_id}@{self.meter_version}"

    @property
    def identity(self) -> str:
        return f"{self.scope.fingerprint}/{self.event_id}"

    @property
    def idempotency_key(self) -> str:
        return fingerprint(
            {
                "scopeFingerprint": self.scope.fingerprint,
                "eventId": self.event_id,
            }
        )

    @property
    def dimension_fingerprint(self) -> str:
        return fingerprint(dict(self.dimensions))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(include_fingerprint=False))

    def normalized(self, meter: MeterV1) -> UsageEventV1:
        if (self.meter_id, self.meter_version) != (meter.meter_id, meter.version):
            raise InvalidUsage(
                f"event references {self.meter_ref!r}, not supplied meter {meter.ref!r}",
                requirement="usage.meter.version",
            )
        if not meter.accepts_window(self.window):
            raise InactiveMeter(meter.ref)
        meter.validate_event_time(self.window, self.recorded_at)
        dimensions = meter.validate_dimensions(self.dimensions)
        normalized = meter.normalize(self.value, self.unit)
        return replace(
            self,
            value=normalized,
            unit=meter.canonical_unit,
            dimensions=dimensions,
            original_value=self.value if self.original_value is None else self.original_value,
            original_unit=self.unit if self.original_unit is None else self.original_unit,
        )

    def with_captured_correlation(self) -> UsageEventV1:
        if not self.correlation.empty:
            return self
        return replace(self, correlation=UsageCorrelation.capture())

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "eventId": self.event_id,
            "idempotencyKey": self.idempotency_key,
            "scope": self.scope.to_dict(),
            "scopeFingerprint": self.scope.fingerprint,
            "subjectId": self.subject_id,
            "meterId": self.meter_id,
            "meterVersion": self.meter_version,
            "windowStart": iso_datetime(self.window.start),
            "windowEnd": iso_datetime(self.window.end),
            "value": decimal_text(self.value),
            "unit": self.unit,
            "dimensions": dict(self.dimensions),
            "dimensionFingerprint": self.dimension_fingerprint,
            "source": self.source,
            "recordedAt": iso_datetime(self.recorded_at),
            "correctionOf": self.correction_of,
            "correctionReason": self.correction_reason,
            "correlation": self.correlation.to_dict(),
            "provenance": dict(self.provenance),
            "originalValue": (
                None if self.original_value is None else decimal_text(self.original_value)
            ),
            "originalUnit": self.original_unit,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UsageEventV1:
        required = {
            "eventId",
            "scope",
            "subjectId",
            "meterId",
            "meterVersion",
            "windowStart",
            "windowEnd",
            "value",
            "unit",
        }
        if required - set(value):
            raise InvalidUsage("usage event record is missing required fields")
        scope = value["scope"]
        dimensions = value.get("dimensions", {})
        provenance = value.get("provenance", {})
        if (
            not isinstance(scope, Mapping)
            or not isinstance(dimensions, Mapping)
            or not isinstance(provenance, Mapping)
        ):
            raise InvalidUsage("usage event scope, dimensions, or provenance has invalid shape")
        recorded_at = value.get("recordedAt", value["windowEnd"])
        return cls(
            event_id=cast(str, value["eventId"]),
            scope=UsageScope(cast(Mapping[str, str], scope)),
            subject_id=cast(str, value["subjectId"]),
            meter_id=cast(str, value["meterId"]),
            meter_version=cast(int, value["meterVersion"]),
            window=UsageWindow(
                parse_datetime(value["windowStart"], "window start"),
                parse_datetime(value["windowEnd"], "window end"),
            ),
            value=decimal_value(value["value"]),
            unit=cast(str, value["unit"]),
            dimensions=cast(Mapping[str, str], dimensions),
            source=cast(str, value.get("source", "application")),
            recorded_at=parse_datetime(recorded_at, "recorded_at"),
            correction_of=cast(str | None, value.get("correctionOf")),
            correction_reason=cast(str | None, value.get("correctionReason")),
            correlation=UsageCorrelation.from_mapping(value.get("correlation")),
            provenance=cast(Mapping[str, str], provenance),
            original_value=(
                None
                if value.get("originalValue") is None
                else decimal_value(value["originalValue"], "original_value")
            ),
            original_unit=cast(str | None, value.get("originalUnit")),
        )


UsageEvent = UsageEventV1


@dataclass(frozen=True, slots=True)
class UsageAggregateV1:
    """One immutable aggregate version derived from a deterministic event set."""

    aggregate_id: str
    revision: int
    scope: UsageScope
    meter_id: str
    meter_version: int
    window: UsageWindow
    dimensions: Mapping[str, str]
    total: Decimal
    event_count: int
    watermark: datetime
    source_fingerprint: str
    created_at: datetime
    supersedes: str | None = None
    algorithm: str = "sum.v1"
    correlation: UsageCorrelation = field(default_factory=UsageCorrelation)
    schema_version: str = field(default=_SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_id", token(self.aggregate_id, "aggregate_id"))
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise InvalidUsage("aggregate revision must be a positive integer")
        object.__setattr__(self, "scope", UsageScope.parse(self.scope))
        object.__setattr__(self, "meter_id", logical_name(self.meter_id, "meter_id"))
        if (
            isinstance(self.meter_version, bool)
            or not isinstance(self.meter_version, int)
            or self.meter_version < 1
        ):
            raise InvalidUsage("meter_version must be a positive integer")
        if not isinstance(self.window, UsageWindow):
            raise InvalidUsage("aggregate window must be a UsageWindow")
        object.__setattr__(
            self,
            "dimensions",
            string_map(self.dimensions, "aggregate dimensions", maximum_entries=32),
        )
        object.__setattr__(self, "total", _storage_decimal(self.total, "aggregate total"))
        if (
            isinstance(self.event_count, bool)
            or not isinstance(self.event_count, int)
            or self.event_count < 0
        ):
            raise InvalidUsage("aggregate event_count must be a non-negative integer")
        object.__setattr__(self, "watermark", utc_datetime(self.watermark, "watermark"))
        object.__setattr__(
            self,
            "source_fingerprint",
            require_fingerprint(self.source_fingerprint, "source_fingerprint"),
        )
        object.__setattr__(self, "created_at", utc_datetime(self.created_at, "created_at"))
        if self.supersedes is not None:
            object.__setattr__(self, "supersedes", token(self.supersedes, "supersedes"))
        object.__setattr__(self, "algorithm", token(self.algorithm, "algorithm"))
        if not isinstance(self.correlation, UsageCorrelation):
            raise InvalidUsage("aggregate correlation must be UsageCorrelation")

    @property
    def version_id(self) -> str:
        return f"{self.aggregate_id}@{self.revision}"

    @property
    def dimension_fingerprint(self) -> str:
        return fingerprint(dict(self.dimensions))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "aggregateId": self.aggregate_id,
            "aggregateRevision": self.revision,
            "aggregateVersionId": self.version_id,
            "scope": self.scope.to_dict(),
            "scopeFingerprint": self.scope.fingerprint,
            "meterId": self.meter_id,
            "meterVersion": self.meter_version,
            "windowStart": iso_datetime(self.window.start),
            "windowEnd": iso_datetime(self.window.end),
            "dimensions": dict(self.dimensions),
            "dimensionFingerprint": self.dimension_fingerprint,
            "total": decimal_text(self.total),
            "eventCount": self.event_count,
            "watermark": iso_datetime(self.watermark),
            "sourceFingerprint": self.source_fingerprint,
            "createdAt": iso_datetime(self.created_at),
            "supersedes": self.supersedes,
            "algorithm": self.algorithm,
            "correlation": self.correlation.to_dict(),
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UsageAggregateV1:
        required = {
            "aggregateId",
            "aggregateRevision",
            "scope",
            "meterId",
            "meterVersion",
            "windowStart",
            "windowEnd",
            "dimensions",
            "total",
            "eventCount",
            "watermark",
            "sourceFingerprint",
            "createdAt",
        }
        if required - set(value):
            raise InvalidUsage("usage aggregate record is missing required fields")
        scope = value["scope"]
        dimensions = value["dimensions"]
        if not isinstance(scope, Mapping) or not isinstance(dimensions, Mapping):
            raise InvalidUsage("aggregate scope or dimensions has invalid shape")
        return cls(
            aggregate_id=cast(str, value["aggregateId"]),
            revision=cast(int, value["aggregateRevision"]),
            scope=UsageScope(cast(Mapping[str, str], scope)),
            meter_id=cast(str, value["meterId"]),
            meter_version=cast(int, value["meterVersion"]),
            window=UsageWindow(
                parse_datetime(value["windowStart"], "window start"),
                parse_datetime(value["windowEnd"], "window end"),
            ),
            dimensions=cast(Mapping[str, str], dimensions),
            total=decimal_value(value["total"], "aggregate total"),
            event_count=cast(int, value["eventCount"]),
            watermark=parse_datetime(value["watermark"], "watermark"),
            source_fingerprint=cast(str, value["sourceFingerprint"]),
            created_at=parse_datetime(value["createdAt"], "created_at"),
            supersedes=cast(str | None, value.get("supersedes")),
            algorithm=cast(str, value.get("algorithm", "sum.v1")),
            correlation=UsageCorrelation.from_mapping(value.get("correlation")),
        )


UsageAggregate = UsageAggregateV1


def event_set_fingerprint(events: Sequence[UsageEventV1]) -> str:
    """Fingerprint a set independently of input order."""

    return fingerprint(sorted(item.fingerprint for item in events))


__all__ = [
    "DimensionSpec",
    "MeterV1",
    "UnitTransform",
    "UsageAggregate",
    "UsageAggregateV1",
    "UsageEvent",
    "UsageEventV1",
    "UsageScope",
    "UsageWindow",
    "event_set_fingerprint",
]
