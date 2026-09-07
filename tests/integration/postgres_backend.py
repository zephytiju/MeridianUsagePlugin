# SPDX-License-Identifier: Apache-2.0
"""Isolated live-engine test provisioning; never imported by application modules."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from psycopg import connect, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from meridian_storage import Meridian, RuntimeConfig
from meridian_storage.adapters.postgresql._settings import PostgreSQLSettings
from meridian_storage.adapters.postgresql.descriptor import manifest
from meridian_storage.adapters.postgresql.migration import MigrationExecutor
from meridian_storage.adapters.postgresql.schema import SchemaCompiler
from meridian_storage.evidence import EvidenceCatalogProvider
from meridian_storage.plugins.usage import UsageResources, event_schema, meter_schema, usage_schemas
from meridian_storage.registry import (
    NamespaceDefinition,
    ResourceBundle,
    ResourceDefinition,
)
from meridian_storage.semantics import StructuredCatalogProvider
from meridian_storage.spi.adapters import SecretValue


class LocalTestSecrets:
    def __init__(self, values):
        self.values = values

    def resolve(self, reference):
        return SecretValue(self.values[reference.reference].encode())


USAGE_RESOURCES = UsageResources(
    meters="usage.runtime_test_meters", events="usage.runtime_test_events"
)


class UsageTestSchemaProvider:
    provider_id = "usage.decimal-test"
    provider_contract_version = "1.0.0"

    def __init__(self, *, include_state=False):
        self.include_state = include_state

    def documents(self):
        return usage_schemas() if self.include_state else (meter_schema(), event_schema())

    def load(self):
        # Published owning schemas; test deployment selects custom logical resources
        # through the public UsageResources contract. No schema/capability patching.
        docs = self.documents()
        schemas = tuple(d.to_core_definition() for d in docs)
        refs = tuple(getattr(USAGE_RESOURCES, d.ref.name) for d in docs)
        return ResourceBundle(
            self.provider_id,
            "1.0.0",
            "1.0.0",
            namespaces=(NamespaceDefinition("structured", "usage"),),
            schemas=schemas,
            resources=tuple(
                ResourceDefinition(
                    ref,
                    profile=doc.semantic_kind.value,
                    schema=schema.ref,
                    required_scope=("runtime",),
                )
                for ref, doc, schema in zip(refs, docs, schemas, strict=True)
            ),
        )


class LiveBackend:
    def __init__(self, dsn, *, include_state=False):
        self.dsn = dsn
        values = conninfo_to_dict(dsn)
        self.secrets = LocalTestSecrets(
            {
                "identity": values.pop("user", "postgres"),
                "credential": values.pop("password", "postgres"),
            }
        )
        self.usage_provider = UsageTestSchemaProvider(include_state=include_state)
        bundles = (self.usage_provider.load(),)
        all_resources = tuple(r for b in bundles for r in b.resources)
        schema_by_ref = {s.ref: s for b in bundles for s in b.schemas}
        self.namespace = "usage_decimal_" + uuid4().hex[:12]
        layouts = []
        for resource in all_resources:
            name = resource.ref.name
            doc = next(
                d
                for d in self.usage_provider.documents()
                if getattr(USAGE_RESOURCES, d.ref.name) == resource.ref
            )
            definition = doc.to_dict()
            fields = []
            for f in definition["fields"]:
                logical = f["logicalType"]
                fields.append(
                    {
                        "name": f["name"],
                        "column": f["name"].lower(),
                        "logicalType": logical["kind"] if isinstance(logical, dict) else logical,
                        **(
                            {"precision": logical["precision"], "scale": logical["scale"]}
                            if isinstance(logical, dict)
                            else {}
                        ),
                        "nullable": f["nullable"],
                        "mutable": f["mutable"],
                    }
                )
            layouts.append(
                {
                    "ref": resource.ref.canonical,
                    "table": name,
                    "profile": resource.profile,
                    "schemaFingerprint": schema_by_ref[resource.schema].fingerprint,
                    "resourceFingerprint": resource.fingerprint,
                    "fields": fields,
                    "identity": definition["identity"],
                    "indexes": [
                        {
                            "name": i["name"].lower().replace("-", "_"),
                            "kind": i["kind"],
                            "fields": i["fields"],
                            "unique": i["unique"],
                        }
                        for i in definition["indexes"]
                    ],
                    "relation": None,
                }
            )
        catalogs = (StructuredCatalogProvider(), EvidenceCatalogProvider())
        profile, version = "postgresql-postgis-local-single-primary", "16-postgis-3.4"
        config = {
            "formatVersion": "meridian-config.v1",
            "profile": "local-test",
            "catalogs": {
                "providers": [
                    {
                        "name": p.catalog_name,
                        "package": p.manifest().package_name,
                        "contract": p.manifest().catalog_contract_version,
                        "requiredFingerprint": p.manifest().fingerprint,
                    }
                    for p in catalogs
                ],
                "extensions": {},
            },
            "resources": {
                "pins": [
                    {
                        "ref": r.ref.to_dict(),
                        "providerId": next(b.provider_id for b in bundles if r in b.resources),
                        "requiredFingerprint": r.fingerprint,
                    }
                    for r in all_resources
                ],
                "extensions": {},
            },
            "schemas": {
                "providers": [
                    {
                        "id": b.provider_id,
                        "package": "meridian-plugin-usage",
                        "contract": "1.x",
                        "requiredFingerprint": b.fingerprint,
                    }
                    for b in bundles
                ],
                "live": {"enabled": False, "required": False, "providerId": None},
                "extensions": {},
            },
            "bindings": [
                {
                    "id": "runtime",
                    "adapterId": "postgresql",
                    "adapterContract": "1.0.0",
                    "engineProfile": profile,
                    "engineVersion": version,
                    "endpoint": make_conninfo(**values),
                    "serviceRef": None,
                    "physicalNamespace": self.namespace,
                    "tls": {
                        "mode": "disabled",
                        "serverName": None,
                        "caRef": None,
                        "clientCertificateRef": None,
                    },
                    "identityRef": {"provider": "test", "reference": "identity"},
                    "secretRef": {"provider": "test", "reference": "credential"},
                    "client": {
                        "minSize": 1,
                        "maxSize": 16,
                        "acquireTimeoutMs": 10000,
                        "idleTimeoutMs": 30000,
                        "operationTimeoutMs": 10000,
                        "maxResultBytes": 16777216,
                        "iteratorLifetimeMs": 10000,
                    },
                    "requiredCapabilityFingerprint": manifest(profile, version).fingerprint,
                    "requiredPhysicalFingerprint": None,
                    "compatibilityPins": {},
                    "settings": {
                        "formatVersion": "meridian.postgresql.settings.v1",
                        "applicationName": "usage-decimal-test",
                        "scopeKeys": ["runtime"],
                        "topology": {"expectedStandbys": 0},
                        "resources": layouts,
                    },
                    "extensions": {},
                }
            ],
            "placements": [
                {
                    "id": "runtime-placement",
                    "selector": {
                        "resources": [r.ref.to_dict() for r in all_resources],
                        "catalog": None,
                        "labels": {},
                    },
                    "bindingId": "runtime",
                    "extensions": {},
                }
            ],
            "validation": {
                "strict": True,
                "requirePhysicalFingerprints": False,
                "defaultOperationTimeoutMs": 10000,
                "idempotencyCacheEntries": 64,
                "retry": {"maxAttempts": 1, "baseDelayMs": 0, "maxDelayMs": 0, "jitterRatio": 0},
            },
            "telemetry": {
                "enabled": False,
                "serviceName": None,
                "suppressExporterRecursion": True,
                "attributes": {},
                "extensions": {},
            },
            "extensions": {},
        }
        parsed = RuntimeConfig.from_mapping(config)
        settings = PostgreSQLSettings.from_binding(parsed.bindings[0])
        plan = SchemaCompiler(settings).compile()
        with connect(dsn) as connection:
            MigrationExecutor(settings).apply(connection, plan)
        config["bindings"][0]["requiredPhysicalFingerprint"] = plan.physical_fingerprint
        config["validation"]["requirePhysicalFingerprints"] = True
        self.config = RuntimeConfig.from_mapping(config)
        self.runtimes = []

    def start(self):
        runtime = Meridian.from_config(
            self.config,
            schema_providers=(self.usage_provider,),
            secret_resolver=self.secrets,
        )
        runtime.start()
        self.runtimes.append(runtime)
        return runtime

    def close(self):
        for runtime in self.runtimes:
            runtime.close()
        # An isolated schema only: no shared schema or engine-wide cleanup.
        with connect(self.dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.namespace))
            )


@pytest.fixture(scope="session")
def postgres_backend():
    dsn = os.environ.get("USAGE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.fail("USAGE_TEST_POSTGRES_DSN is required; live acceptance cannot be skipped")
    backend = LiveBackend(dsn)
    yield backend
    backend.close()


@pytest.fixture(scope="session")
def mode_backend():
    dsn = os.environ.get("USAGE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.fail("USAGE_TEST_POSTGRES_DSN is required; live acceptance cannot be skipped")
    backend = LiveBackend(dsn, include_state=True)
    yield backend
    backend.close()
