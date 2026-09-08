# SPDX-License-Identifier: Apache-2.0
"""Consumer-owned Evidence envelope for immutable public Usage records."""

from meridian_storage import ResourceRef
from meridian_storage.registry import NamespaceDefinition, ResourceBundle, ResourceDefinition
from meridian_storage.semantics import (
    PROFILE_EXTENSION_KEY,
    CatalogName,
    FieldDefinition,
    LogicalKind,
    LogicalType,
    SchemaDocument,
    SchemaReference,
    SemanticKind,
    TimeSeriesProfile,
)

EVIDENCE_REF = ResourceRef("evidence", "usage_test", "receipts")


class ReceiptSchemaProvider:
    provider_id = "usage.test-receipts"
    provider_contract_version = "1.0.0"

    def documents(self):
        return (
            SchemaDocument(
                ref=SchemaReference(CatalogName.EVIDENCE, "usage_test", "receipts", "1.0.0"),
                semantic_kind=SemanticKind.TIME_SERIES,
                fields=tuple(
                    FieldDefinition(name, LogicalType(kind), mutable=False)
                    for name, kind in {
                        "evidenceId": LogicalKind.STRING,
                        "observedTime": LogicalKind.UTC_TIMESTAMP,
                        "payload": LogicalKind.JSON,
                        "count": LogicalKind.INT64,
                    }.items()
                ),
                identity=("evidenceId",),
                consistency="eventual",
                extensions={
                    PROFILE_EXTENSION_KEY: TimeSeriesProfile(
                        "observedTime", ("evidenceId",), (), ("count",)
                    ).to_dict()
                },
            ),
        )

    def load(self):
        schema = self.documents()[0].to_core_definition()
        return ResourceBundle(
            self.provider_id,
            "1.0.0",
            self.provider_contract_version,
            namespaces=(NamespaceDefinition("evidence", "usage_test"),),
            schemas=(schema,),
            resources=(
                ResourceDefinition(
                    EVIDENCE_REF,
                    "append-only-evidence",
                    schema=schema.ref,
                    required_scope=("runtime",),
                ),
            ),
        )
