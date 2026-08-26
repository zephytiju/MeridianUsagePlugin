# SPDX-License-Identifier: Apache-2.0
"""Stable Usage error taxonomy built on Meridian's public error model."""

from __future__ import annotations

from typing import Any, cast

from meridian_storage import ConflictError, NotFoundError, TransientError, ValidationError


class InvalidUsage(ValidationError):
    """A Usage model, query, or operation is invalid."""

    def __init__(
        self, message: str, *, requirement: str = "usage.valid", **details: object
    ) -> None:
        resource_ref = details.pop("resource_ref", None)
        if details:
            raise TypeError(f"unsupported Usage error details: {sorted(details)!r}")
        self.requirement = requirement
        super().__init__(
            "MERIDIAN_USAGE_INVALID",
            message,
            resource_ref=cast(str | None, resource_ref),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["requirement"] = self.requirement
        return payload


class UnknownMeter(NotFoundError):
    """The immutable meter version does not exist."""

    def __init__(self, meter_ref: str) -> None:
        self.requirement = "usage.meter.exists"
        super().__init__(
            "MERIDIAN_USAGE_METER_NOT_FOUND",
            f"Usage meter {meter_ref!r} was not found",
            resource_ref=meter_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["requirement"] = self.requirement
        return payload


class InactiveMeter(InvalidUsage):
    """The event falls outside the meter version's activation interval."""

    def __init__(self, meter_ref: str) -> None:
        super().__init__(
            f"Usage meter {meter_ref!r} is inactive for the event window",
            requirement="usage.meter.active",
            resource_ref=meter_ref,
        )


class UnitMismatch(InvalidUsage):
    """A unit has no exact transform in the referenced meter version."""

    def __init__(self, unit: str, meter_ref: str) -> None:
        super().__init__(
            f"Unit {unit!r} is not declared by Usage meter {meter_ref!r}",
            requirement="usage.unit.declared",
            resource_ref=meter_ref,
        )


class DimensionViolation(InvalidUsage):
    """Event dimensions do not conform to the immutable meter version."""

    def __init__(self, message: str, meter_ref: str) -> None:
        super().__init__(
            message,
            requirement="usage.dimensions.conform",
            resource_ref=meter_ref,
        )


class DecimalOverflow(InvalidUsage):
    """An exact conversion cannot fit the declared Decimal domain."""

    def __init__(self, message: str) -> None:
        super().__init__(message, requirement="usage.decimal.exact")


class InvalidCorrection(InvalidUsage):
    """A correction does not preserve the target event identity domain."""

    def __init__(self, message: str) -> None:
        super().__init__(message, requirement="usage.correction.chain")


class UsageConflict(ConflictError):
    """The same immutable identity was replayed with different content."""

    def __init__(self, identity: str, *, kind: str = "record") -> None:
        self.requirement = "usage.immutable.identity"
        super().__init__(
            "MERIDIAN_USAGE_IMMUTABLE_CONFLICT",
            f"Usage {kind} identity {identity!r} already has different immutable content",
            resource_ref=identity,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["requirement"] = self.requirement
        return payload


class CheckpointConflict(ConflictError):
    """A watermark checkpoint compare-and-set lost a race."""

    def __init__(self, checkpoint_id: str) -> None:
        self.requirement = "usage.checkpoint.cas"
        super().__init__(
            "MERIDIAN_USAGE_CHECKPOINT_CONFLICT",
            f"Usage checkpoint {checkpoint_id!r} changed concurrently",
            resource_ref=checkpoint_id,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["requirement"] = self.requirement
        return payload


class ClaimUnavailable(TransientError):
    """Another aggregation worker owns a live claim."""

    def __init__(self, claim_id: str) -> None:
        self.requirement = "usage.aggregation.claim"
        super().__init__(
            "MERIDIAN_USAGE_CLAIM_UNAVAILABLE",
            f"Usage aggregation claim {claim_id!r} is currently owned",
            resource_ref=claim_id,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["requirement"] = self.requirement
        return payload


class InvalidUsageResult(InvalidUsage):
    """A backing Meridian Resource returned an invalid public result shape."""

    def __init__(self, message: str) -> None:
        super().__init__(message, requirement="usage.result.shape")


__all__ = [
    "CheckpointConflict",
    "ClaimUnavailable",
    "DecimalOverflow",
    "DimensionViolation",
    "InactiveMeter",
    "InvalidCorrection",
    "InvalidUsage",
    "InvalidUsageResult",
    "UnitMismatch",
    "UnknownMeter",
    "UsageConflict",
]
