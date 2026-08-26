# SPDX-License-Identifier: Apache-2.0
"""Meridian-backed persistence and recording APIs for immutable Usage data."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, cast

from meridian_storage import (
    ConflictError,
    Expression,
    MeridianError,
    NotFoundError,
    OperationResult,
    ResourceRef,
    bind_context,
    current_context,
)
from meridian_storage.semantics import StructuredCatalogSurface

from ._canonical import (
    bounded_text,
    fingerprint,
    iso_datetime,
    logical_name,
    parse_datetime,
    token,
    utc_datetime,
)
from .correlation import UsageCorrelation
from .errors import (
    CheckpointConflict,
    ClaimUnavailable,
    InvalidCorrection,
    InvalidUsage,
    InvalidUsageResult,
    UnknownMeter,
    UsageConflict,
)
from .models import (
    MeterV1,
    UsageAggregateV1,
    UsageEventV1,
    UsageScope,
)
from .query import MeridianExecutor, UsageOrder, UsageQueries, UsageQuery

_MAX_BATCH = 1000


class TransactionalExecutor(MeridianExecutor, Protocol):
    def transaction(self, resource: ResourceRef) -> AbstractContextManager[object]: ...


@dataclass(frozen=True, slots=True)
class UsageResources:
    """Logical resources rendered and placed by deployment IaC."""

    meters: ResourceRef = field(
        default_factory=lambda: ResourceRef("structured", "usage", "meters")
    )
    events: ResourceRef = field(
        default_factory=lambda: ResourceRef("structured", "usage", "events")
    )
    aggregates: ResourceRef = field(
        default_factory=lambda: ResourceRef("structured", "usage", "aggregates")
    )
    batches: ResourceRef = field(
        default_factory=lambda: ResourceRef("structured", "usage", "batches")
    )
    checkpoints: ResourceRef = field(
        default_factory=lambda: ResourceRef("structured", "usage", "checkpoints")
    )
    claims: ResourceRef = field(
        default_factory=lambda: ResourceRef("structured", "usage", "claims")
    )

    def __post_init__(self) -> None:
        names = ("meters", "events", "aggregates", "batches", "checkpoints", "claims")
        refs: list[ResourceRef] = []
        for name in names:
            try:
                selected = ResourceRef.parse(getattr(self, name), catalog="structured")
            except (TypeError, ValueError) as exc:
                raise InvalidUsage(f"{name} must be a logical structured Resource") from exc
            object.__setattr__(self, name, selected)
            refs.append(selected)
        if len(set(refs)) != len(refs):
            raise InvalidUsage("Usage logical Resources must be distinct")


class BatchMode(StrEnum):
    """Explicit atomicity profiles; partitioned is portable to ClickHouse."""

    ATOMIC = "atomic"
    PARTITIONED = "partitioned"


class RecordStatus(StrEnum):
    RECORDED = "recorded"
    REPLAYED = "replayed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RecordReceipt:
    event: UsageEventV1
    status: RecordStatus
    operation_result: OperationResult | None = None

    @property
    def replayed(self) -> bool:
        return self.status is RecordStatus.REPLAYED

    @property
    def correlation(self) -> UsageCorrelation:
        if self.operation_result is None:
            return self.event.correlation
        return self.event.correlation.with_result(self.operation_result)


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    event_id: str
    status: RecordStatus
    fingerprint: str
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "eventId": self.event_id,
            "status": self.status.value,
            "fingerprint": self.fingerprint,
            "errorCode": self.error_code,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BatchItemResult:
        status = value.get("status")
        if not isinstance(status, str):
            raise InvalidUsageResult("batch item status is invalid")
        return cls(
            token(value.get("eventId"), "event_id"),
            RecordStatus(status),
            cast(str, value.get("fingerprint")),
            cast(str | None, value.get("errorCode")),
        )


@dataclass(frozen=True, slots=True)
class BatchReceipt:
    batch_id: str
    mode: BatchMode
    scope: UsageScope
    fingerprint: str
    items: tuple[BatchItemResult, ...]
    replayed: bool = False

    @property
    def complete(self) -> bool:
        return all(item.status is not RecordStatus.FAILED for item in self.items)


@dataclass(frozen=True, slots=True)
class UsageCheckpoint:
    checkpoint_id: str
    scope: UsageScope
    watermark: datetime
    revision: int
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "checkpoint_id", logical_name(self.checkpoint_id, "checkpoint_id"))
        object.__setattr__(self, "scope", UsageScope.parse(self.scope))
        object.__setattr__(self, "watermark", utc_datetime(self.watermark, "watermark"))
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise InvalidUsage("checkpoint revision must be a positive integer")
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at, "updated_at"))

    def to_dict(self) -> dict[str, object]:
        content: dict[str, object] = {
            "checkpointId": self.checkpoint_id,
            "scope": self.scope.to_dict(),
            "scopeFingerprint": self.scope.fingerprint,
            "watermark": iso_datetime(self.watermark),
            "revision": self.revision,
            "updatedAt": iso_datetime(self.updated_at),
        }
        content["fingerprint"] = fingerprint(content)
        return content

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> UsageCheckpoint:
        scope = value.get("scope")
        if not isinstance(scope, Mapping):
            raise InvalidUsageResult("checkpoint scope is not a mapping")
        return cls(
            cast(str, value.get("checkpointId")),
            UsageScope(cast(Mapping[str, str], scope)),
            parse_datetime(value.get("watermark"), "watermark"),
            cast(int, value.get("revision")),
            parse_datetime(value.get("updatedAt"), "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class AggregationClaim:
    claim_id: str
    scope: UsageScope
    owner: str
    expires_at: datetime
    revision: int
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", logical_name(self.claim_id, "claim_id"))
        object.__setattr__(self, "scope", UsageScope.parse(self.scope))
        object.__setattr__(self, "owner", bounded_text(self.owner, "claim owner", 256))
        object.__setattr__(self, "expires_at", utc_datetime(self.expires_at, "expires_at"))
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise InvalidUsage("claim revision must be a positive integer")
        object.__setattr__(self, "updated_at", utc_datetime(self.updated_at, "updated_at"))

    def to_dict(self) -> dict[str, object]:
        content: dict[str, object] = {
            "claimId": self.claim_id,
            "scope": self.scope.to_dict(),
            "scopeFingerprint": self.scope.fingerprint,
            "owner": self.owner,
            "expiresAt": iso_datetime(self.expires_at),
            "revision": self.revision,
            "updatedAt": iso_datetime(self.updated_at),
        }
        content["fingerprint"] = fingerprint(content)
        return content

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AggregationClaim:
        scope = value.get("scope")
        if not isinstance(scope, Mapping):
            raise InvalidUsageResult("claim scope is not a mapping")
        return cls(
            cast(str, value.get("claimId")),
            UsageScope(cast(Mapping[str, str], scope)),
            cast(str, value.get("owner")),
            parse_datetime(value.get("expiresAt"), "expires_at"),
            cast(int, value.get("revision")),
            parse_datetime(value.get("updatedAt"), "updated_at"),
        )


def _single_record(data: object) -> Mapping[str, object] | None:
    if data is None:
        return None
    selected = data
    if isinstance(data, Mapping):
        for key in ("item", "record", "data"):
            if key in data:
                selected = data[key]
                break
    if selected is None:
        return None
    if not isinstance(selected, Mapping):
        raise InvalidUsageResult("Meridian get returned an invalid Usage record")
    return MappingProxyType(dict(selected))


def _storage_version(record: Mapping[str, object] | None, fallback: int = 0) -> str | int:
    if record is None:
        return 0
    value = record.get("_version", record.get("storageVersion", fallback))
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise InvalidUsageResult("conditional Usage record returned an invalid version")
    return value


@contextmanager
def _idempotency_context(kind: str, identity: str) -> Iterator[None]:
    context = current_context(required=False)
    if context is None:
        yield
        return
    key = fingerprint({"kind": kind, "identity": identity})
    selected = replace(context, idempotency_key=key)
    with bind_context(selected):
        yield


class UsageRepository:
    """Persistence boundary that accepts only a Meridian Expression executor."""

    def __init__(
        self,
        executor: MeridianExecutor,
        resources: UsageResources | None = None,
    ) -> None:
        if not callable(getattr(executor, "execute", None)):
            raise TypeError("executor must implement Meridian.execute(Expression)")
        self._executor = executor
        self.resources = resources or UsageResources()
        self.queries = UsageQueries(
            executor,
            self.resources.events,
            self.resources.aggregates,
        )
        self._surface = StructuredCatalogSurface()

    def _execute(self, expression: Expression, *, kind: str, identity: str) -> OperationResult:
        with _idempotency_context(kind, identity):
            return self._executor.execute(expression)

    def _get(
        self,
        resource: ResourceRef,
        where: Mapping[str, object],
        *,
        identity: str,
    ) -> Mapping[str, object] | None:
        expression = self._surface.get(resource=resource.to_dict(), where=where)
        try:
            result = self._execute(expression, kind="get", identity=identity)
        except NotFoundError:
            return None
        return _single_record(result.data)

    def _put(
        self,
        resource: ResourceRef,
        data: Mapping[str, object],
        *,
        identity: str,
        expected_version: str | int | None = None,
    ) -> OperationResult:
        expression = self._surface.put(
            resource=resource.to_dict(),
            data=data,
            expected_version=expected_version,
        )
        return self._execute(expression, kind="put", identity=identity)

    def get_meter(self, meter_id: str, version: int) -> MeterV1:
        selected_id = logical_name(meter_id, "meter_id")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidUsage("meter version must be a positive integer")
        meter_ref = f"{selected_id}@{version}"
        record = self._get(
            self.resources.meters,
            {"meterId": selected_id, "meterVersion": version},
            identity=meter_ref,
        )
        if record is None:
            raise UnknownMeter(meter_ref)
        return MeterV1.from_mapping(record)

    def register_meter(self, meter: MeterV1) -> tuple[MeterV1, bool]:
        if not isinstance(meter, MeterV1):
            raise TypeError("meter must be MeterV1")
        existing = self._get(
            self.resources.meters,
            {"meterId": meter.meter_id, "meterVersion": meter.version},
            identity=meter.ref,
        )
        if existing is not None:
            persisted = MeterV1.from_mapping(existing)
            if persisted.fingerprint != meter.fingerprint:
                raise UsageConflict(meter.ref, kind="meter")
            return persisted, True
        try:
            self._put(
                self.resources.meters,
                meter.to_dict(),
                identity=meter.ref,
                expected_version=0,
            )
        except ConflictError:
            persisted = self.get_meter(meter.meter_id, meter.version)
            if persisted.fingerprint != meter.fingerprint:
                raise UsageConflict(meter.ref, kind="meter") from None
            return persisted, True
        return meter, False

    def get_event(self, scope: UsageScope, event_id: str) -> UsageEventV1 | None:
        selected_scope = UsageScope.parse(scope)
        selected_id = token(event_id, "event_id")
        record = self._get(
            self.resources.events,
            {"scopeFingerprint": selected_scope.fingerprint, "eventId": selected_id},
            identity=f"{selected_scope.fingerprint}/{selected_id}",
        )
        return None if record is None else UsageEventV1.from_mapping(record)

    def _validate_correction(self, event: UsageEventV1) -> None:
        if event.correction_of is None:
            return
        target = self.get_event(event.scope, event.correction_of)
        if target is None:
            raise InvalidCorrection(
                f"correction target {event.correction_of!r} does not exist in the same scope"
            )
        same_domain = (
            event.subject_id == target.subject_id
            and event.meter_id == target.meter_id
            and event.meter_version == target.meter_version
            and event.window == target.window
            and event.dimensions == target.dimensions
        )
        if not same_domain:
            raise InvalidCorrection(
                "corrections must preserve subject, meter version, window, and dimensions"
            )

    def normalize_event(
        self,
        event: UsageEventV1,
        *,
        meter: MeterV1 | None = None,
    ) -> UsageEventV1:
        if not isinstance(event, UsageEventV1):
            raise TypeError("event must be UsageEventV1")
        selected_meter = meter or self.get_meter(event.meter_id, event.meter_version)
        return event.with_captured_correlation().normalized(selected_meter)

    def record(
        self,
        event: UsageEventV1,
        *,
        meter: MeterV1 | None = None,
    ) -> RecordReceipt:
        normalized = self.normalize_event(event, meter=meter)
        self._validate_correction(normalized)
        existing = self.get_event(normalized.scope, normalized.event_id)
        if existing is not None:
            if existing.fingerprint != normalized.fingerprint:
                raise UsageConflict(normalized.identity, kind="event")
            return RecordReceipt(existing, RecordStatus.REPLAYED)
        try:
            result = self._put(
                self.resources.events,
                normalized.to_dict(),
                identity=normalized.identity,
            )
        except ConflictError:
            persisted = self.get_event(normalized.scope, normalized.event_id)
            if persisted is None or persisted.fingerprint != normalized.fingerprint:
                raise UsageConflict(normalized.identity, kind="event") from None
            return RecordReceipt(persisted, RecordStatus.REPLAYED)
        return RecordReceipt(normalized, RecordStatus.RECORDED, result)

    def _batch_record(
        self,
        scope: UsageScope,
        batch_id: str,
    ) -> Mapping[str, object] | None:
        return self._get(
            self.resources.batches,
            {"scopeFingerprint": scope.fingerprint, "batchId": batch_id},
            identity=f"{scope.fingerprint}/{batch_id}",
        )

    def _replayed_batch(
        self,
        record: Mapping[str, object],
        *,
        expected_fingerprint: str,
    ) -> BatchReceipt:
        persisted_fingerprint = record.get("batchFingerprint")
        batch_id = token(record.get("batchId"), "batch_id")
        if persisted_fingerprint != expected_fingerprint:
            raise UsageConflict(batch_id, kind="batch")
        scope = record.get("scope")
        items = record.get("items")
        if (
            not isinstance(scope, Mapping)
            or not isinstance(items, Sequence)
            or isinstance(items, str | bytes | bytearray)
        ):
            raise InvalidUsageResult("batch manifest has an invalid shape")
        raw_mode = record.get("mode")
        if not isinstance(raw_mode, str):
            raise InvalidUsageResult("batch manifest mode is invalid")
        return BatchReceipt(
            batch_id,
            BatchMode(raw_mode),
            UsageScope(cast(Mapping[str, str], scope)),
            expected_fingerprint,
            tuple(BatchItemResult.from_mapping(cast(Mapping[str, object], item)) for item in items),
            replayed=True,
        )

    def _normalize_batch(
        self,
        events: Sequence[UsageEventV1],
    ) -> tuple[list[UsageEventV1], dict[tuple[str, int], MeterV1]]:
        if not 1 <= len(events) <= _MAX_BATCH:
            raise InvalidUsage(f"usage batches must contain between 1 and {_MAX_BATCH} events")
        meters: dict[tuple[str, int], MeterV1] = {}
        normalized: list[UsageEventV1] = []
        for event in events:
            if not isinstance(event, UsageEventV1):
                raise TypeError("usage batches must contain UsageEventV1 values")
            key = (event.meter_id, event.meter_version)
            if key not in meters:
                meters[key] = self.get_meter(*key)
            normalized.append(self.normalize_event(event, meter=meters[key]))
        scope = normalized[0].scope
        if any(item.scope != scope for item in normalized):
            raise InvalidUsage(
                "a usage batch must contain one scope to preserve physical isolation",
                requirement="usage.batch.scope",
            )
        seen_ids: set[str] = set()
        for item in normalized:
            if item.event_id in seen_ids:
                raise InvalidUsage(
                    f"usage batch contains duplicate event id {item.event_id!r}",
                    requirement="usage.batch.unique-event-ids",
                )
            seen_ids.add(item.event_id)
        return normalized, meters

    def _record_atomic_batch(
        self,
        events: Sequence[UsageEventV1],
        meters: Mapping[tuple[str, int], MeterV1],
    ) -> list[BatchItemResult]:
        transaction = getattr(self._executor, "transaction", None)
        if not callable(transaction):
            raise InvalidUsage(
                "atomic usage batches require a transaction-capable Meridian runtime",
                requirement="usage.batch.atomic-capability",
            )
        items: list[BatchItemResult] = []
        with transaction(self.resources.events):
            for event in events:
                receipt = self.record(
                    event,
                    meter=meters[(event.meter_id, event.meter_version)],
                )
                items.append(BatchItemResult(event.event_id, receipt.status, event.fingerprint))
        return items

    def _record_partitioned_batch(
        self,
        events: Sequence[UsageEventV1],
        meters: Mapping[tuple[str, int], MeterV1],
    ) -> list[BatchItemResult]:
        items: list[BatchItemResult] = []
        for event in events:
            try:
                receipt = self.record(
                    event,
                    meter=meters[(event.meter_id, event.meter_version)],
                )
            except MeridianError as exc:
                items.append(
                    BatchItemResult(
                        event.event_id,
                        RecordStatus.FAILED,
                        event.fingerprint,
                        str(exc.code),
                    )
                )
            else:
                items.append(BatchItemResult(event.event_id, receipt.status, event.fingerprint))
        return items

    def record_batch(
        self,
        events: Sequence[UsageEventV1],
        *,
        batch_id: str | None = None,
        mode: BatchMode | str = BatchMode.PARTITIONED,
    ) -> BatchReceipt:
        try:
            selected_mode = BatchMode(mode)
        except (TypeError, ValueError) as exc:
            raise InvalidUsage(
                "usage batch mode must be atomic or partitioned",
                requirement="usage.batch.mode",
            ) from exc
        normalized, meters = self._normalize_batch(events)
        scope = normalized[0].scope
        batch_fingerprint = fingerprint(
            {
                "mode": selected_mode.value,
                "eventFingerprints": [item.fingerprint for item in normalized],
            }
        )
        selected_batch_id = (
            token(batch_id, "batch_id")
            if batch_id is not None
            else f"batch-{batch_fingerprint.removeprefix('sha256:')}"
        )
        existing = self._batch_record(scope, selected_batch_id)
        if existing is not None:
            return self._replayed_batch(
                existing,
                expected_fingerprint=batch_fingerprint,
            )
        if selected_mode is BatchMode.ATOMIC:
            items = self._record_atomic_batch(normalized, meters)
        else:
            items = self._record_partitioned_batch(normalized, meters)
        manifest: dict[str, object] = {
            "schemaVersion": "1.0.0",
            "batchId": selected_batch_id,
            "scope": scope.to_dict(),
            "scopeFingerprint": scope.fingerprint,
            "mode": selected_mode.value,
            "batchFingerprint": batch_fingerprint,
            "items": [item.to_dict() for item in items],
            "recordedAt": iso_datetime(datetime.now(UTC)),
        }
        try:
            self._put(
                self.resources.batches,
                manifest,
                identity=f"{scope.fingerprint}/{selected_batch_id}",
                expected_version=0,
            )
        except ConflictError:
            persisted = self._batch_record(scope, selected_batch_id)
            if persisted is None:
                raise
            return self._replayed_batch(
                persisted,
                expected_fingerprint=batch_fingerprint,
            )
        return BatchReceipt(
            selected_batch_id,
            selected_mode,
            scope,
            batch_fingerprint,
            tuple(items),
        )

    def put_aggregate(self, aggregate: UsageAggregateV1) -> tuple[UsageAggregateV1, bool]:
        if not isinstance(aggregate, UsageAggregateV1):
            raise TypeError("aggregate must be UsageAggregateV1")
        existing = self._get(
            self.resources.aggregates,
            {
                "scopeFingerprint": aggregate.scope.fingerprint,
                "aggregateVersionId": aggregate.version_id,
            },
            identity=f"{aggregate.scope.fingerprint}/{aggregate.version_id}",
        )
        if existing is not None:
            persisted = UsageAggregateV1.from_mapping(existing)
            if persisted.fingerprint != aggregate.fingerprint:
                raise UsageConflict(aggregate.version_id, kind="aggregate")
            return persisted, True
        try:
            self._put(
                self.resources.aggregates,
                aggregate.to_dict(),
                identity=f"{aggregate.scope.fingerprint}/{aggregate.version_id}",
            )
        except ConflictError:
            record = self._get(
                self.resources.aggregates,
                {
                    "scopeFingerprint": aggregate.scope.fingerprint,
                    "aggregateVersionId": aggregate.version_id,
                },
                identity=aggregate.version_id,
            )
            if record is None:
                raise
            persisted = UsageAggregateV1.from_mapping(record)
            if persisted.fingerprint != aggregate.fingerprint:
                raise UsageConflict(aggregate.version_id, kind="aggregate") from None
            return persisted, True
        return aggregate, False

    def latest_aggregate(
        self,
        scope: UsageScope,
        aggregate_id: str,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> UsageAggregateV1 | None:
        query = self.queries.aggregates(
            scope,
            window_start,
            window_end,
            where={"aggregateId": token(aggregate_id, "aggregate_id")},
        )
        query = replace(
            query,
            order_by=(
                UsageOrder("aggregateRevision", "desc"),
                UsageOrder("aggregateVersionId", "desc"),
            ),
            limit=1,
        )
        result = query.execute()
        if not result.items:
            return None
        return UsageAggregateV1.from_mapping(result.items[0])

    def scan_events(
        self, query: UsageQuery, *, max_pages: int = 10_000
    ) -> tuple[UsageEventV1, ...]:
        if query.resource != self.resources.events:
            raise InvalidUsage("scan_events requires this repository's events Resource")
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise InvalidUsage("max_pages must be a positive integer")
        events: list[UsageEventV1] = []
        current = query
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            page = current.execute()
            events.extend(UsageEventV1.from_mapping(item) for item in page.items)
            if page.cursor is None:
                return tuple(events)
            if page.cursor in seen_cursors:
                raise InvalidUsageResult("Usage pagination returned a repeated cursor")
            seen_cursors.add(page.cursor)
            current = current.page(cursor=page.cursor)
        raise InvalidUsageResult("Usage pagination exceeded the configured page budget")

    def get_checkpoint(
        self,
        scope: UsageScope,
        checkpoint_id: str,
    ) -> UsageCheckpoint | None:
        selected_scope = UsageScope.parse(scope)
        selected_id = logical_name(checkpoint_id, "checkpoint_id")
        record = self._get(
            self.resources.checkpoints,
            {"scopeFingerprint": selected_scope.fingerprint, "checkpointId": selected_id},
            identity=f"{selected_scope.fingerprint}/{selected_id}",
        )
        return None if record is None else UsageCheckpoint.from_mapping(record)

    def advance_checkpoint(
        self,
        scope: UsageScope,
        checkpoint_id: str,
        watermark: datetime,
        *,
        expected_revision: int,
        now: datetime | None = None,
    ) -> UsageCheckpoint:
        selected_scope = UsageScope.parse(scope)
        selected_id = logical_name(checkpoint_id, "checkpoint_id")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise InvalidUsage("expected checkpoint revision must be non-negative")
        record = self._get(
            self.resources.checkpoints,
            {"scopeFingerprint": selected_scope.fingerprint, "checkpointId": selected_id},
            identity=f"{selected_scope.fingerprint}/{selected_id}",
        )
        current = None if record is None else UsageCheckpoint.from_mapping(record)
        current_revision = 0 if current is None else current.revision
        if current_revision != expected_revision:
            raise CheckpointConflict(selected_id)
        selected_watermark = utc_datetime(watermark, "watermark")
        if current is not None and selected_watermark < current.watermark:
            raise InvalidUsage("checkpoint watermarks cannot move backward")
        updated = UsageCheckpoint(
            selected_id,
            selected_scope,
            selected_watermark,
            current_revision + 1,
            utc_datetime(now or datetime.now(UTC), "updated_at"),
        )
        try:
            self._put(
                self.resources.checkpoints,
                updated.to_dict(),
                identity=f"{selected_scope.fingerprint}/{selected_id}",
                expected_version=_storage_version(record, current_revision),
            )
        except ConflictError as exc:
            raise CheckpointConflict(selected_id) from exc
        return updated

    def get_claim(self, scope: UsageScope, claim_id: str) -> AggregationClaim | None:
        selected_scope = UsageScope.parse(scope)
        selected_id = logical_name(claim_id, "claim_id")
        record = self._get(
            self.resources.claims,
            {"scopeFingerprint": selected_scope.fingerprint, "claimId": selected_id},
            identity=f"{selected_scope.fingerprint}/{selected_id}",
        )
        return None if record is None else AggregationClaim.from_mapping(record)

    def acquire_claim(
        self,
        scope: UsageScope,
        claim_id: str,
        owner: str,
        *,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> AggregationClaim:
        selected_scope = UsageScope.parse(scope)
        selected_id = logical_name(claim_id, "claim_id")
        selected_owner = bounded_text(owner, "claim owner", 256)
        selected_now = utc_datetime(now or datetime.now(UTC), "now")
        selected_expiry = utc_datetime(expires_at, "expires_at")
        if selected_expiry <= selected_now:
            raise InvalidUsage("claim expiry must be in the future")
        record = self._get(
            self.resources.claims,
            {"scopeFingerprint": selected_scope.fingerprint, "claimId": selected_id},
            identity=f"{selected_scope.fingerprint}/{selected_id}",
        )
        current = None if record is None else AggregationClaim.from_mapping(record)
        if (
            current is not None
            and current.expires_at > selected_now
            and current.owner != selected_owner
        ):
            raise ClaimUnavailable(selected_id)
        revision = 1 if current is None else current.revision + 1
        claim = AggregationClaim(
            selected_id,
            selected_scope,
            selected_owner,
            selected_expiry,
            revision,
            selected_now,
        )
        try:
            self._put(
                self.resources.claims,
                claim.to_dict(),
                identity=f"{selected_scope.fingerprint}/{selected_id}",
                expected_version=_storage_version(
                    record, 0 if current is None else current.revision
                ),
            )
        except ConflictError as exc:
            raise ClaimUnavailable(selected_id) from exc
        return claim

    def release_claim(
        self,
        claim: AggregationClaim,
        *,
        now: datetime | None = None,
    ) -> AggregationClaim:
        selected_now = utc_datetime(now or datetime.now(UTC), "now")
        record = self._get(
            self.resources.claims,
            {
                "scopeFingerprint": claim.scope.fingerprint,
                "claimId": claim.claim_id,
            },
            identity=f"{claim.scope.fingerprint}/{claim.claim_id}",
        )
        current = None if record is None else AggregationClaim.from_mapping(record)
        if current is None or current.revision != claim.revision or current.owner != claim.owner:
            raise ClaimUnavailable(claim.claim_id)
        released = AggregationClaim(
            claim.claim_id,
            claim.scope,
            claim.owner,
            selected_now,
            claim.revision + 1,
            selected_now,
        )
        try:
            self._put(
                self.resources.claims,
                released.to_dict(),
                identity=f"{claim.scope.fingerprint}/{claim.claim_id}",
                expected_version=_storage_version(record, current.revision),
            )
        except ConflictError as exc:
            raise ClaimUnavailable(claim.claim_id) from exc
        return released


class UsageRecorder:
    """Small publisher-facing facade; services remain external consumers."""

    def __init__(self, repository: UsageRepository) -> None:
        if not isinstance(repository, UsageRepository):
            raise TypeError("repository must be UsageRepository")
        self._repository = repository

    def record(self, event: UsageEventV1) -> RecordReceipt:
        return self._repository.record(event)

    def record_batch(
        self,
        events: Sequence[UsageEventV1],
        *,
        batch_id: str | None = None,
        mode: BatchMode | str = BatchMode.PARTITIONED,
    ) -> BatchReceipt:
        return self._repository.record_batch(events, batch_id=batch_id, mode=mode)


__all__ = [
    "AggregationClaim",
    "BatchItemResult",
    "BatchMode",
    "BatchReceipt",
    "RecordReceipt",
    "RecordStatus",
    "UsageCheckpoint",
    "UsageRecorder",
    "UsageRepository",
    "UsageResources",
]
