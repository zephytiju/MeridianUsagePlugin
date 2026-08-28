# SPDX-License-Identifier: Apache-2.0
"""Released Semantics schemas and logical Resource bundle for Usage V1."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import cast

from meridian_storage.registry.resources import (
    CapabilityRequirement,
    NamespaceDefinition,
    ResourceBundle,
    ResourceDefinition,
)
from meridian_storage.semantics import (
    PROFILE_EXTENSION_KEY,
    CatalogName,
    FieldDefinition,
    FrozenJson,
    IndexDefinition,
    LogicalKind,
    LogicalType,
    RelationalProfile,
    SchemaDocument,
    SchemaReference,
    SemanticKind,
    TimeSeriesProfile,
)

from ._version import __version__
from .repository import UsageResources

_CONTRACT_VERSION = "1.0.0"
_DECIMAL = LogicalType(LogicalKind.DECIMAL, precision=76, scale=18)


def _field(
    name: str,
    kind: LogicalKind | LogicalType,
    *,
    nullable: bool = False,
) -> FieldDefinition:
    logical_type = kind if isinstance(kind, LogicalType) else LogicalType(kind)
    return FieldDefinition(name, logical_type, nullable=nullable, mutable=False)


def _document(
    name: str,
    fields: Iterable[FieldDefinition],
    identity: tuple[str, ...],
    profile: TimeSeriesProfile | RelationalProfile,
    *,
    consistency: str,
    retention_label: str,
    indexes: tuple[IndexDefinition, ...] = (),
) -> SchemaDocument:
    return SchemaDocument(
        ref=SchemaReference(CatalogName.STRUCTURED, "usage", name, _CONTRACT_VERSION),
        semantic_kind=SemanticKind(profile.kind),
        fields=tuple(fields),
        identity=identity,
        indexes=indexes,
        consistency=consistency,
        retention_label=retention_label,
        extensions=cast(
            Mapping[str, FrozenJson],
            {
                PROFILE_EXTENSION_KEY: profile.to_dict(),
                "org.meridian.usage/contract": _CONTRACT_VERSION,
            },
        ),
        compatibility={"mode": "backward"},
    )


def meter_schema() -> SchemaDocument:
    return _document(
        "meters",
        (
            _field("schemaVersion", LogicalKind.STRING),
            _field("meterId", LogicalKind.STRING),
            _field("meterVersion", LogicalKind.INT64),
            _field("quantity", LogicalKind.STRING),
            _field("canonicalUnit", LogicalKind.STRING),
            _field("transforms", LogicalKind.JSON),
            _field("dimensions", LogicalKind.JSON),
            _field("precision", LogicalKind.INT64),
            _field("scale", LogicalKind.INT64),
            _field("eventTimeToleranceSeconds", LogicalKind.INT64),
            _field("activeFrom", LogicalKind.UTC_TIMESTAMP),
            _field("retiredAt", LogicalKind.UTC_TIMESTAMP, nullable=True),
            _field("description", LogicalKind.STRING, nullable=True),
            _field("fingerprint", LogicalKind.STRING),
        ),
        ("meterId", "meterVersion"),
        RelationalProfile(unique_fields=(("meterId", "meterVersion"),)),
        consistency="strong",
        retention_label="usage-control",
        indexes=(IndexDefinition("meter-version", "btree", ("meterId", "meterVersion"), True),),
    )


def event_schema() -> SchemaDocument:
    profile = TimeSeriesProfile(
        timestamp_field="windowStart",
        series_identity=("eventId",),
        dimensions=(
            "scopeFingerprint",
            "meterId",
            "meterVersion",
            "subjectId",
            "dimensionFingerprint",
        ),
        measurements=("value",),
        exemplar_field="correlation",
    )
    return _document(
        "events",
        (
            _field("schemaVersion", LogicalKind.STRING),
            _field("eventId", LogicalKind.STRING),
            _field("idempotencyKey", LogicalKind.STRING),
            _field("scope", LogicalKind.JSON),
            _field("scopeFingerprint", LogicalKind.STRING),
            _field("subjectId", LogicalKind.STRING),
            _field("meterId", LogicalKind.STRING),
            _field("meterVersion", LogicalKind.INT64),
            _field("windowStart", LogicalKind.UTC_TIMESTAMP),
            _field("windowEnd", LogicalKind.UTC_TIMESTAMP),
            _field("value", _DECIMAL),
            _field("unit", LogicalKind.STRING),
            _field("dimensions", LogicalKind.JSON),
            _field("dimensionFingerprint", LogicalKind.STRING),
            _field("source", LogicalKind.STRING),
            _field("recordedAt", LogicalKind.UTC_TIMESTAMP),
            _field("correctionOf", LogicalKind.STRING, nullable=True),
            _field("correctionReason", LogicalKind.STRING, nullable=True),
            _field("correlation", LogicalKind.JSON),
            _field("provenance", LogicalKind.JSON),
            _field("originalValue", _DECIMAL, nullable=True),
            _field("originalUnit", LogicalKind.STRING, nullable=True),
            _field("fingerprint", LogicalKind.STRING),
        ),
        ("scopeFingerprint", "eventId"),
        profile,
        consistency="eventual",
        retention_label="usage-events",
        indexes=(
            IndexDefinition("event-identity", "btree", ("scopeFingerprint", "eventId"), True),
            IndexDefinition("event-window", "time-series", ("windowStart",)),
            IndexDefinition("correction-target", "hash", ("correctionOf",)),
        ),
    )


def aggregate_schema() -> SchemaDocument:
    profile = TimeSeriesProfile(
        timestamp_field="windowStart",
        series_identity=("aggregateVersionId",),
        dimensions=(
            "scopeFingerprint",
            "meterId",
            "meterVersion",
            "dimensionFingerprint",
        ),
        measurements=("total", "eventCount"),
        exemplar_field="correlation",
    )
    return _document(
        "aggregates",
        (
            _field("schemaVersion", LogicalKind.STRING),
            _field("aggregateId", LogicalKind.STRING),
            _field("aggregateRevision", LogicalKind.INT64),
            _field("aggregateVersionId", LogicalKind.STRING),
            _field("scope", LogicalKind.JSON),
            _field("scopeFingerprint", LogicalKind.STRING),
            _field("meterId", LogicalKind.STRING),
            _field("meterVersion", LogicalKind.INT64),
            _field("windowStart", LogicalKind.UTC_TIMESTAMP),
            _field("windowEnd", LogicalKind.UTC_TIMESTAMP),
            _field("dimensions", LogicalKind.JSON),
            _field("dimensionFingerprint", LogicalKind.STRING),
            _field("total", _DECIMAL),
            _field("eventCount", LogicalKind.INT64),
            _field("watermark", LogicalKind.UTC_TIMESTAMP),
            _field("sourceFingerprint", LogicalKind.STRING),
            _field("createdAt", LogicalKind.UTC_TIMESTAMP),
            _field("supersedes", LogicalKind.STRING, nullable=True),
            _field("algorithm", LogicalKind.STRING),
            _field("correlation", LogicalKind.JSON),
            _field("fingerprint", LogicalKind.STRING),
        ),
        ("scopeFingerprint", "aggregateVersionId"),
        profile,
        consistency="eventual",
        retention_label="usage-aggregates",
        indexes=(
            IndexDefinition(
                "aggregate-version",
                "btree",
                ("scopeFingerprint", "aggregateVersionId"),
                True,
            ),
            IndexDefinition("aggregate-window", "time-series", ("windowStart",)),
            IndexDefinition("aggregate-base", "hash", ("aggregateId",)),
        ),
    )


def batch_schema() -> SchemaDocument:
    return _document(
        "batches",
        (
            _field("schemaVersion", LogicalKind.STRING),
            _field("batchId", LogicalKind.STRING),
            _field("scope", LogicalKind.JSON),
            _field("scopeFingerprint", LogicalKind.STRING),
            _field("mode", LogicalKind.STRING),
            _field("batchFingerprint", LogicalKind.STRING),
            _field("items", LogicalKind.JSON),
            _field("recordedAt", LogicalKind.UTC_TIMESTAMP),
        ),
        ("scopeFingerprint", "batchId"),
        RelationalProfile(unique_fields=(("scopeFingerprint", "batchId"),)),
        consistency="strong",
        retention_label="usage-control",
    )


def checkpoint_schema() -> SchemaDocument:
    return _document(
        "checkpoints",
        (
            _field("checkpointId", LogicalKind.STRING),
            _field("scope", LogicalKind.JSON),
            _field("scopeFingerprint", LogicalKind.STRING),
            _field("watermark", LogicalKind.UTC_TIMESTAMP),
            _field("revision", LogicalKind.INT64),
            _field("updatedAt", LogicalKind.UTC_TIMESTAMP),
            _field("fingerprint", LogicalKind.STRING),
        ),
        ("scopeFingerprint", "checkpointId"),
        RelationalProfile(unique_fields=(("scopeFingerprint", "checkpointId"),)),
        consistency="strong",
        retention_label="usage-control",
    )


def claim_schema() -> SchemaDocument:
    return _document(
        "claims",
        (
            _field("claimId", LogicalKind.STRING),
            _field("scope", LogicalKind.JSON),
            _field("scopeFingerprint", LogicalKind.STRING),
            _field("owner", LogicalKind.STRING),
            _field("expiresAt", LogicalKind.UTC_TIMESTAMP),
            _field("revision", LogicalKind.INT64),
            _field("updatedAt", LogicalKind.UTC_TIMESTAMP),
            _field("fingerprint", LogicalKind.STRING),
        ),
        ("scopeFingerprint", "claimId"),
        RelationalProfile(unique_fields=(("scopeFingerprint", "claimId"),)),
        consistency="strong",
        retention_label="usage-control",
    )


def usage_schemas() -> tuple[SchemaDocument, ...]:
    return (
        meter_schema(),
        event_schema(),
        aggregate_schema(),
        batch_schema(),
        checkpoint_schema(),
        claim_schema(),
    )


def _requirements(*methods: str) -> tuple[CapabilityRequirement, ...]:
    return tuple(
        CapabilityRequirement(f"meridian.structured.{method}", "1.0.0") for method in methods
    )


class UsageSchemaProvider:
    @property
    def provider_id(self) -> str:
        return "usage"

    @property
    def provider_contract_version(self) -> str:
        return _CONTRACT_VERSION

    def load(self) -> ResourceBundle:
        resources = UsageResources()
        documents = {document.ref.name: document for document in usage_schemas()}
        definitions = tuple(document.to_core_definition() for document in documents.values())
        logical_resources = (
            ResourceDefinition(
                resources.meters,
                "relational",
                definitions[0].ref,
                labels={"plugin": "usage", "recordType": "meter"},
                requirements=_requirements("get", "put"),
                related_resources=(resources.events, resources.aggregates),
            ),
            ResourceDefinition(
                resources.events,
                "usage",
                definitions[1].ref,
                labels={"plugin": "usage", "recordType": "event"},
                requirements=_requirements("get", "put", "query"),
                related_resources=(resources.meters, resources.aggregates),
            ),
            ResourceDefinition(
                resources.aggregates,
                "usage",
                definitions[2].ref,
                labels={"plugin": "usage", "recordType": "aggregate"},
                requirements=_requirements("get", "put", "query"),
                related_resources=(resources.events, resources.meters),
            ),
            ResourceDefinition(
                resources.batches,
                "relational",
                definitions[3].ref,
                labels={"plugin": "usage", "recordType": "batch"},
                requirements=_requirements("get", "put"),
                related_resources=(resources.events,),
            ),
            ResourceDefinition(
                resources.checkpoints,
                "relational",
                definitions[4].ref,
                labels={"plugin": "usage", "recordType": "checkpoint"},
                requirements=_requirements("get", "put"),
                related_resources=(resources.events, resources.aggregates),
            ),
            ResourceDefinition(
                resources.claims,
                "relational",
                definitions[5].ref,
                labels={"plugin": "usage", "recordType": "claim"},
                requirements=_requirements("get", "put"),
                related_resources=(resources.checkpoints,),
            ),
        )
        return ResourceBundle(
            provider_id=self.provider_id,
            provider_version=__version__,
            provider_contract_version=self.provider_contract_version,
            namespaces=(
                NamespaceDefinition(
                    "structured",
                    "usage",
                    labels={"plugin": "usage", "lifecycleOwner": "platform"},
                ),
            ),
            schemas=definitions,
            resources=logical_resources,
            extensions={
                "distribution": "meridian-storage-plugin-usage",
                "catalogsOwned": [],
                "catalogsUsed": ["structured"],
                "design": {
                    "hldRevision": 56,
                    "catalogRevision": 70,
                    "adapterRevision": 24,
                    "kafkaStreamingRevision": 6,
                    "constructsRevision": 45,
                    "usageLldRevision": 19,
                },
            },
        )


__all__ = [
    "UsageSchemaProvider",
    "aggregate_schema",
    "batch_schema",
    "checkpoint_schema",
    "claim_schema",
    "event_schema",
    "meter_schema",
    "usage_schemas",
]
