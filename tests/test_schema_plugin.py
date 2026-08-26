# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import timedelta
from importlib.metadata import entry_points

import pytest

from meridian_storage import Meridian, ResourceRef, RuntimeState
from meridian_storage.adapters.clickhouse.schema import (
    ClickHouseSchemaCompiler,
    RecordProfile,
)
from meridian_storage.plugins.usage import (
    InvalidUsage,
    RetentionInput,
    UsagePluginFactory,
    UsageResources,
    UsageSchemaProvider,
    aggregate_schema,
    event_schema,
    usage_schemas,
)
from meridian_storage.semantics import (
    RelationalProfile,
    SchemaDocument,
    TimeSeriesProfile,
)
from meridian_storage.spi import PluginFactory, SchemaProvider


def test_schema_provider_is_discoverable_and_bundle_is_complete() -> None:
    provider = UsageSchemaProvider()
    assert isinstance(provider, SchemaProvider)
    bundle = provider.load()
    assert provider.provider_id == "usage"
    assert provider.provider_contract_version == "1.0.0"
    assert {item.ref.name for item in bundle.schemas} == {
        "meters",
        "events",
        "aggregates",
        "batches",
        "checkpoints",
        "claims",
    }
    assert {item.ref.name for item in bundle.resources} == {
        "meters",
        "events",
        "aggregates",
        "batches",
        "checkpoints",
        "claims",
    }
    assert {item.ref.catalog for item in bundle.resources} == {"structured"}
    assert bundle.extensions["catalogsOwned"] == ()
    assert bundle.extensions["catalogsUsed"] == ("structured",)
    assert bundle.fingerprint.startswith("sha256:")
    discovered = {
        point.name: point.load()
        for point in entry_points(group="meridian_storage.schemas")
        if point.name == "usage"
    }
    assert discovered == {"usage": UsageSchemaProvider}


def test_semantics_documents_round_trip_and_profiles_are_correct() -> None:
    for document in usage_schemas():
        restored = SchemaDocument.from_definition(
            catalog=document.ref.catalog,
            namespace=document.ref.namespace,
            name=document.ref.name,
            version=document.ref.version or "",
            definition=document.to_dict(),
        )
        assert restored == document
        assert all(not field.mutable for field in document.fields)
        assert document.fingerprint.startswith("sha256:")
    assert isinstance(event_schema().profile, TimeSeriesProfile)
    assert isinstance(aggregate_schema().profile, TimeSeriesProfile)
    assert all(
        isinstance(document.profile, RelationalProfile)
        for document in usage_schemas()
        if document.ref.name in {"meters", "batches", "checkpoints", "claims"}
    )


@pytest.mark.integration
def test_released_clickhouse_compiles_usage_event_and_aggregate_layouts() -> None:
    bundle = UsageSchemaProvider().load()
    resources = {item.ref.name: item for item in bundle.resources}
    compiler = ClickHouseSchemaCompiler()
    for document in (event_schema(), aggregate_schema()):
        resource = resources[document.ref.name]
        compiled = compiler.compile(
            database="meridian",
            resource=resource.ref,
            resource_fingerprint=resource.fingerprint,
            schema=document,
            record_profile=RecordProfile.USAGE,
        )
        assert compiled.layout.record_profile is RecordProfile.USAGE
        assert compiled.layout.timestamp_field == "windowStart"
        assert compiled.layout.layout_fingerprint.startswith("sha256:")
        assert "CREATE TABLE IF NOT EXISTS" in compiled.create_table_sql
        assert "_meridian_scope_fingerprint" in compiled.create_table_sql
        assert "Decimal(76, 18)" in compiled.create_table_sql


def test_plugin_factory_manifest_and_entry_point() -> None:
    factory = UsagePluginFactory()
    assert isinstance(factory, PluginFactory)
    manifest = factory.manifest()
    assert manifest.plugin_id == "usage"
    assert manifest.plugin_version == "1.0.0"
    assert manifest.extensions["distribution"] == "meridian-plugin-usage"
    assert manifest.extensions["catalog"] == "structured"
    assert manifest.extensions["service"] == "false"
    assert manifest.extensions["privateDatabase"] == "false"
    discovered = {
        point.name: point.load()
        for point in entry_points(group="meridian_storage.plugins")
        if point.name == "usage"
    }
    assert discovered == {"usage": UsagePluginFactory}


def test_plugin_rejects_unready_runtime() -> None:
    class UnreadyMeridian(Meridian):
        @property
        def state(self) -> RuntimeState:
            return RuntimeState.NEW

    runtime = UnreadyMeridian.__new__(UnreadyMeridian)
    with pytest.raises(InvalidUsage, match="ready"):
        UsagePluginFactory().create(runtime)


def test_resources_and_retention_are_logical_inputs_only() -> None:
    resources = UsageResources()
    retention = RetentionInput(
        resources.events,
        "usage-events",
        timedelta(days=90),
        correction_grace=timedelta(days=7),
    )
    assert retention.to_dict() == {
        "formatVersion": "meridian.usage.retention-input.v1",
        "resource": {
            "catalog": "structured",
            "namespace": "usage",
            "name": "events",
        },
        "policyLabel": "usage-events",
        "retainSeconds": 7_776_000,
        "correctionGraceSeconds": 604_800,
        "legalHoldLabel": None,
    }
    with pytest.raises(InvalidUsage):
        RetentionInput(
            ResourceRef("structured", "usage", "events"),
            "usage-events",
            timedelta(0),
        )
