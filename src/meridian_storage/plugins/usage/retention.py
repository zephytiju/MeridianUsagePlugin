# SPDX-License-Identifier: Apache-2.0
"""Declarative retention inputs consumed by platform-owned lifecycle tooling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from meridian_storage import ResourceRef

from ._canonical import JsonValue, logical_name
from .errors import InvalidUsage


@dataclass(frozen=True, slots=True)
class RetentionInput:
    """A logical retention request; this library never applies or migrates it."""

    resource: ResourceRef
    policy_label: str
    retain_for: timedelta
    correction_grace: timedelta = timedelta(0)
    legal_hold_label: str | None = None

    def __post_init__(self) -> None:
        try:
            resource = ResourceRef.parse(self.resource, catalog="structured")
        except (TypeError, ValueError) as exc:
            raise InvalidUsage("retention inputs require a logical structured Resource") from exc
        if self.retain_for <= timedelta(0):
            raise InvalidUsage("retain_for must be positive")
        if self.correction_grace < timedelta(0):
            raise InvalidUsage("correction_grace cannot be negative")
        if (
            not self.retain_for.total_seconds().is_integer()
            or not self.correction_grace.total_seconds().is_integer()
        ):
            raise InvalidUsage("retention durations must use whole seconds")
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "policy_label", logical_name(self.policy_label, "policy_label"))
        if self.legal_hold_label is not None:
            object.__setattr__(
                self,
                "legal_hold_label",
                logical_name(self.legal_hold_label, "legal_hold_label"),
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": "meridian.usage.retention-input.v1",
            "resource": self.resource.to_dict(),
            "policyLabel": self.policy_label,
            "retainSeconds": int(self.retain_for.total_seconds()),
            "correctionGraceSeconds": int(self.correction_grace.total_seconds()),
            "legalHoldLabel": self.legal_hold_label,
        }


__all__ = ["RetentionInput"]
