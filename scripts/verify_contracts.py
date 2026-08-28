# SPDX-License-Identifier: Apache-2.0
"""Verify released contracts, locked design evidence, and deterministic goldens."""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from packaging.requirements import Requirement

from meridian_storage.plugins.usage import (
    DimensionSpec,
    MeterV1,
    UnitTransform,
    UsageAggregateV1,
    UsageEventV1,
    UsagePluginFactory,
    UsageSchemaProvider,
    UsageScope,
    UsageWindow,
    __version__,
    event_set_fingerprint,
    usage_schemas,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PINS = {
    "meridian-plugin-observability": "==1.0.0",
    "meridian-storage-core": "==1.0.0",
    "meridian-storage-evidence": "==1.0.0",
    "meridian-storage-query": "==1.0.0",
    "meridian-storage-semantics": "==1.0.0",
}
FORBIDDEN_IMPORTS = (
    "boto",
    "clickhouse_connect",
    "meridian_storage.adapters",
    "meridian_storage.spi.adapters",
    "psycopg",
    "sqlalchemy",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def _distribution_pins() -> dict[str, str]:
    distribution = metadata.distribution("meridian-storage-plugin-usage")
    result: dict[str, str] = {}
    for raw in distribution.requires or ():
        requirement = Requirement(raw)
        if requirement.name in EXPECTED_PINS and requirement.marker is None:
            result[requirement.name] = str(requirement.specifier)
    _require(result == EXPECTED_PINS, f"released Meridian pins differ: {result!r}")
    return result


def _verify_import_boundary() -> int:
    checked = 0
    for path in sorted((ROOT / "src").rglob("*.py")):
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = (node.module,)
            else:
                continue
            if any(name.startswith(FORBIDDEN_IMPORTS) for name in names):
                raise AssertionError(f"Usage source imports an Adapter, Engine, or client: {path}")
    return checked


def _golden_values() -> dict[str, object]:
    scope = UsageScope({"tenant": "acme", "environment": "test"})
    meter = MeterV1(
        "api.requests",
        1,
        "requests",
        "request",
        transforms={"kilorequest": UnitTransform(Decimal("1000"))},
        dimensions=(
            DimensionSpec("region", True, 100),
            DimensionSpec("plan"),
        ),
        precision=38,
        scale=6,
        active_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    event = UsageEventV1(
        "event-001",
        scope,
        "account-42",
        meter.meter_id,
        meter.version,
        UsageWindow(
            datetime(2026, 8, 26, 10, tzinfo=UTC),
            datetime(2026, 8, 26, 10, 1, tzinfo=UTC),
        ),
        Decimal("2"),
        "kilorequest",
        {"region": "us-west", "plan": "pro"},
        "publisher",
        datetime(2026, 8, 26, 10, 2, tzinfo=UTC),
    ).normalized(meter)
    aggregate = UsageAggregateV1(
        "aggregate-example",
        1,
        scope,
        meter.meter_id,
        meter.version,
        event.window,
        {"region": "us-west"},
        event.value,
        1,
        event.window.end,
        event_set_fingerprint((event,)),
        datetime(2026, 8, 26, 10, 3, tzinfo=UTC),
    )
    return {
        "aggregateFingerprint": aggregate.fingerprint,
        "bundleFingerprint": UsageSchemaProvider().load().fingerprint,
        "eventFingerprint": event.fingerprint,
        "eventSetFingerprint": event_set_fingerprint((event,)),
        "meterFingerprint": meter.fingerprint,
        "schemaFingerprints": {
            document.ref.name: document.fingerprint for document in usage_schemas()
        },
    }


def main() -> None:
    compatibility_text = (
        resources.files("meridian_storage.plugins.usage")
        .joinpath("compatibility.json")
        .read_text(encoding="utf-8")
    )
    compatibility = json.loads(compatibility_text)
    schema = _load_json(ROOT / "contracts" / "usage-plugin.v1.json")
    contract_path = ROOT / "contracts" / "conformance" / "plugin-contract.json"
    contract = _load_json(contract_path)
    golden_path = ROOT / "contracts" / "conformance" / "golden" / "fingerprints.json"
    golden = _load_json(golden_path)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)

    manifest = UsagePluginFactory().manifest()
    bundle = UsageSchemaProvider().load()
    _require(manifest.plugin_id == contract["plugin"]["id"], "plugin id differs")
    _require(manifest.plugin_version == contract["version"] == __version__, "version differs")
    _require(
        manifest.plugin_contract_version == contract["plugin"]["contract"],
        "plugin contract differs",
    )
    _require(manifest.core_contract == contract["plugin"]["core"], "Core range differs")
    _require(compatibility["catalogs"] == contract["catalogs"], "Catalog boundary differs")
    _require(compatibility["boundaries"] == contract["boundaries"], "ownership boundary differs")
    _require(compatibility["design"] == contract["design"], "locked design evidence differs")
    _require(
        bundle.extensions["design"] == compatibility["design"],
        "schema bundle locked design evidence differs",
    )
    _require(_golden_values() == golden, "deterministic Usage golden values differ")

    installed_versions = {
        name: metadata.version(name)
        for name in (
            "meridian-plugin-observability",
            "meridian-storage-core",
            "meridian-storage-evidence",
            "meridian-storage-query",
            "meridian-storage-semantics",
        )
    }
    _require(set(installed_versions.values()) == {"1.0.0"}, "released versions differ")
    pins = _distribution_pins()
    checked_source_files = _verify_import_boundary()
    _require(len(tuple(ROOT.glob("pyproject.toml"))) == 1, "repository must have one project")
    _require(
        not (ROOT / "src" / "meridian_storage" / "__init__.py").exists(),
        "distribution must not own the root PEP 420 namespace",
    )
    _require(
        not (ROOT / "src" / "meridian_storage" / "plugins" / "__init__.py").exists(),
        "distribution must not own the plugins PEP 420 namespace",
    )

    evidence = {
        "formatVersion": "meridian.usage.conformance.v1",
        "package": "meridian-storage-plugin-usage",
        "version": __version__,
        "contracts": {
            "pluginContractSha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            "goldenSha256": hashlib.sha256(golden_path.read_bytes()).hexdigest(),
        },
        "installedVersions": installed_versions,
        "pins": pins,
        "sourceFilesChecked": checked_source_files,
        "status": "passed",
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    output = {**evidence, "fingerprint": "sha256:" + hashlib.sha256(encoded).hexdigest()}
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
