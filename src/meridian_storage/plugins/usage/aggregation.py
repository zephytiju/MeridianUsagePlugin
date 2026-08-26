# SPDX-License-Identifier: Apache-2.0
"""Deterministic library aggregation with claims and watermark checkpoints."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from types import MappingProxyType

from ._canonical import fingerprint, iso_datetime, logical_name, utc_datetime
from .correlation import UsageCorrelation
from .errors import InvalidUsage
from .models import (
    UsageAggregateV1,
    UsageEventV1,
    UsageScope,
    UsageWindow,
    event_set_fingerprint,
)
from .repository import UsageCheckpoint, UsageRepository


@dataclass(frozen=True, slots=True)
class AggregationSpec:
    """Portable sum aggregation inputs; no engine or lifecycle choice appears here."""

    scope: UsageScope
    meter_id: str
    meter_version: int
    initial_start: datetime
    window_size: timedelta
    dimensions: tuple[str, ...] = ()
    allowed_lateness: timedelta = timedelta(0)
    checkpoint_id: str = "usage-sum"
    claim_id: str = "usage-sum"
    page_size: int = 500
    algorithm: str = "sum.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", UsageScope.parse(self.scope))
        object.__setattr__(self, "meter_id", logical_name(self.meter_id, "meter_id"))
        if (
            isinstance(self.meter_version, bool)
            or not isinstance(self.meter_version, int)
            or self.meter_version < 1
        ):
            raise InvalidUsage("aggregation meter_version must be a positive integer")
        object.__setattr__(
            self,
            "initial_start",
            utc_datetime(self.initial_start, "aggregation initial_start"),
        )
        seconds = self.window_size.total_seconds()
        if self.window_size <= timedelta(0) or not seconds.is_integer():
            raise InvalidUsage("aggregation window_size must be positive whole seconds")
        if self.allowed_lateness < timedelta(0):
            raise InvalidUsage("allowed_lateness cannot be negative")
        if not self.allowed_lateness.total_seconds().is_integer():
            raise InvalidUsage("allowed_lateness must use whole seconds")
        dimensions = tuple(
            sorted(logical_name(item, "aggregation dimension") for item in self.dimensions)
        )
        if len(set(dimensions)) != len(dimensions):
            raise InvalidUsage("aggregation dimensions must be unique")
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(
            self,
            "checkpoint_id",
            logical_name(self.checkpoint_id, "checkpoint_id"),
        )
        object.__setattr__(self, "claim_id", logical_name(self.claim_id, "claim_id"))
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= 500
        ):
            raise InvalidUsage("aggregation page_size must be between 1 and 500")
        object.__setattr__(self, "algorithm", logical_name(self.algorithm, "algorithm"))

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "scopeFingerprint": self.scope.fingerprint,
                "meterId": self.meter_id,
                "meterVersion": self.meter_version,
                "initialStart": iso_datetime(self.initial_start),
                "windowSeconds": int(self.window_size.total_seconds()),
                "dimensions": list(self.dimensions),
                "allowedLatenessSeconds": int(self.allowed_lateness.total_seconds()),
                "algorithm": self.algorithm,
            }
        )


def _aggregate_id(
    spec: AggregationSpec,
    window: UsageWindow,
    dimensions: Mapping[str, str],
) -> str:
    digest = fingerprint(
        {
            "spec": spec.fingerprint,
            "window": window.to_dict(),
            "dimensions": dict(dimensions),
        }
    ).removeprefix("sha256:")
    return f"aggregate-{digest}"


def aggregate_events(
    events: Sequence[UsageEventV1],
    spec: AggregationSpec,
    *,
    watermark: datetime,
    created_at: datetime,
    previous: Mapping[str, UsageAggregateV1] | None = None,
) -> tuple[UsageAggregateV1, ...]:
    """Aggregate an order-independent normalized event set into immutable versions."""

    if not isinstance(spec, AggregationSpec):
        raise TypeError("spec must be AggregationSpec")
    selected_watermark = utc_datetime(watermark, "watermark")
    selected_created_at = utc_datetime(created_at, "created_at")
    prior = previous or {}
    groups: dict[tuple[datetime, tuple[tuple[str, str], ...]], list[UsageEventV1]] = defaultdict(
        list
    )
    for event in events:
        if not isinstance(event, UsageEventV1):
            raise TypeError("events must contain UsageEventV1 values")
        if event.scope != spec.scope or (
            event.meter_id,
            event.meter_version,
        ) != (spec.meter_id, spec.meter_version):
            raise InvalidUsage("aggregation events must match the spec scope and meter version")
        if event.window.start >= selected_watermark:
            continue
        dimension_key = tuple(
            (name, event.dimensions[name]) for name in spec.dimensions if name in event.dimensions
        )
        window = UsageWindow.bucket(event.window.start, spec.window_size)
        groups[(window.start, dimension_key)].append(event)
    aggregates: list[UsageAggregateV1] = []
    for (window_start, dimension_items), members in sorted(groups.items()):
        window = UsageWindow(window_start, window_start + spec.window_size)
        grouped_dimensions = MappingProxyType(dict(dimension_items))
        aggregate_id = _aggregate_id(spec, window, grouped_dimensions)
        source_fingerprint = event_set_fingerprint(members)
        current = prior.get(aggregate_id)
        if current is not None and current.source_fingerprint == source_fingerprint:
            aggregates.append(current)
            continue
        with localcontext() as context:
            context.prec = 100
            total = sum((item.value for item in members), start=Decimal(0))
        aggregates.append(
            UsageAggregateV1(
                aggregate_id=aggregate_id,
                revision=1 if current is None else current.revision + 1,
                scope=spec.scope,
                meter_id=spec.meter_id,
                meter_version=spec.meter_version,
                window=window,
                dimensions=grouped_dimensions,
                total=total,
                event_count=len(members),
                watermark=selected_watermark,
                source_fingerprint=source_fingerprint,
                created_at=selected_created_at,
                supersedes=None if current is None else current.version_id,
                algorithm=spec.algorithm,
                correlation=UsageCorrelation.capture(),
            )
        )
    return tuple(aggregates)


@dataclass(frozen=True, slots=True)
class AggregationRunResult:
    checkpoint: UsageCheckpoint
    events_read: int
    aggregates: tuple[UsageAggregateV1, ...]
    replayed_aggregates: int


class AggregationRunner:
    """Crash-recoverable aggregation coordinator over public UsageRepository APIs."""

    def __init__(
        self,
        repository: UsageRepository,
        spec: AggregationSpec,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if not isinstance(repository, UsageRepository):
            raise TypeError("repository must be UsageRepository")
        if not isinstance(spec, AggregationSpec):
            raise TypeError("spec must be AggregationSpec")
        if lease_duration <= timedelta(0):
            raise InvalidUsage("aggregation lease_duration must be positive")
        self._repository = repository
        self.spec = spec
        self.lease_duration = lease_duration

    def run(
        self,
        watermark: datetime,
        *,
        owner: str,
        now: datetime,
    ) -> AggregationRunResult:
        selected_watermark = utc_datetime(watermark, "watermark")
        selected_now = utc_datetime(now, "now")
        if selected_watermark <= self.spec.initial_start:
            raise InvalidUsage("aggregation watermark must follow initial_start")
        claim = self._repository.acquire_claim(
            self.spec.scope,
            self.spec.claim_id,
            owner,
            expires_at=selected_now + self.lease_duration,
            now=selected_now,
        )
        primary_error: BaseException | None = None
        try:
            checkpoint = self._repository.get_checkpoint(
                self.spec.scope,
                self.spec.checkpoint_id,
            )
            if checkpoint is not None and selected_watermark < checkpoint.watermark:
                raise InvalidUsage("aggregation watermark cannot move behind its checkpoint")
            start = self.spec.initial_start
            if checkpoint is not None:
                start = max(
                    self.spec.initial_start,
                    checkpoint.watermark - self.spec.allowed_lateness,
                )
            scan_start = UsageWindow.bucket(start, self.spec.window_size).start
            query = self._repository.queries.events(
                self.spec.scope,
                scan_start,
                selected_watermark,
                where={
                    "meterId": self.spec.meter_id,
                    "meterVersion": self.spec.meter_version,
                },
            ).page(limit=self.spec.page_size)
            events = self._repository.scan_events(query)
            provisional = aggregate_events(
                events,
                self.spec,
                watermark=selected_watermark,
                created_at=selected_now,
            )
            previous: dict[str, UsageAggregateV1] = {}
            for aggregate in provisional:
                current = self._repository.latest_aggregate(
                    self.spec.scope,
                    aggregate.aggregate_id,
                    window_start=aggregate.window.start,
                    window_end=aggregate.window.end,
                )
                if current is not None:
                    previous[aggregate.aggregate_id] = current
            aggregates = aggregate_events(
                events,
                self.spec,
                watermark=selected_watermark,
                created_at=selected_now,
                previous=previous,
            )
            replayed = 0
            for aggregate in aggregates:
                _, was_replayed = self._repository.put_aggregate(aggregate)
                replayed += int(was_replayed)
            updated = self._repository.advance_checkpoint(
                self.spec.scope,
                self.spec.checkpoint_id,
                selected_watermark,
                expected_revision=0 if checkpoint is None else checkpoint.revision,
                now=selected_now,
            )
            return AggregationRunResult(updated, len(events), aggregates, replayed)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                self._repository.release_claim(claim, now=selected_now)
            except BaseException:
                if primary_error is None:
                    raise


__all__ = [
    "AggregationRunResult",
    "AggregationRunner",
    "AggregationSpec",
    "aggregate_events",
]
