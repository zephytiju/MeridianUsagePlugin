# SPDX-License-Identifier: Apache-2.0
"""Required atomic Usage/Evidence writes and a released ClickHouse evidence consumer."""

import os
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import clickhouse_connect
import pytest

from integration.decimal_cases import CASES, make_case
from integration.evidence_support import EVIDENCE_REF, ReceiptSchemaProvider
from integration.postgres_backend import USAGE_RESOURCES, LiveBackend
from meridian_storage import OperationContext
from meridian_storage.adapters.clickhouse import (
    ClickHouseAdapterFactory,
    ClickHouseMigrator,
    ClickHouseSchemaCompiler,
    ClickHouseSettings,
    capability_manifest,
    plan_initial_migration,
)
from meridian_storage.errors import MeridianError
from meridian_storage.evidence import EvidenceCatalogProvider, EvidenceCatalogSurface
from meridian_storage.plugins.usage import UsageEventV1, UsageRepository
from meridian_storage.runtime import BindingConfig
from meridian_storage.spi import (
    AdapterCreateContext,
    ExecutionRequest,
    PhysicalResource,
    SecretValue,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def receipts_backend():
    dsn = os.environ.get("USAGE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.fail("USAGE_TEST_POSTGRES_DSN is required")
    backend = LiveBackend(dsn, evidence=True)
    try:
        yield backend
    finally:
        backend.close()


def receipt_record(event):
    return {
        "evidenceId": event.fingerprint,
        "observedTime": event.recorded_at.isoformat(),
        "payload": event.to_dict(),
        "count": 1,
    }


def write_receipt(runtime, meter, event, *, invalid=False):
    repository = UsageRepository(runtime, USAGE_RESOURCES)
    with runtime.transaction(USAGE_RESOURCES.events):
        receipt = repository.record(event, meter=meter)
        runtime.execute(
            runtime.catalog("evidence").append(
                resource=EVIDENCE_REF,
                data={} if invalid else receipt_record(receipt.event),
                require_atomic=True,
            )
        )
    return receipt


@pytest.mark.parametrize("invalid", [False, True])
def test_required_usage_evidence_commits_or_rolls_back_together(receipts_backend, invalid):
    runtime = receipts_backend.start()
    meter, event = make_case(CASES[0])
    context = OperationContext("test:usage", tenant="usage", scope={"runtime": uuid4().hex})
    with runtime.context(context):
        if invalid:
            with pytest.raises(MeridianError):
                write_receipt(runtime, meter, event, invalid=True)
        else:
            write_receipt(runtime, meter, event)
    runtime.close()
    fresh = receipts_backend.start()
    with fresh.context(context):
        actual = UsageRepository(fresh, USAGE_RESOURCES).get_event(event.scope, event.event_id)
        rows = fresh.execute(fresh.catalog("evidence").query(resource=EVIDENCE_REF)).data["items"]
        if invalid:
            assert actual is None
            assert not rows
        else:
            assert actual.to_dict() == rows[0]["payload"]
            assert len(rows) == 1
            assert write_receipt(fresh, meter, event).replayed
            replay = fresh.execute(fresh.catalog("evidence").query(resource=EVIDENCE_REF))
            assert len(replay.data["items"]) == 1


def test_committed_usage_evidence_clickhouse_replay_paging_and_isolation(receipts_backend):
    port = os.environ.get("USAGE_TEST_CLICKHOUSE_PORT")
    selected = os.environ.get("USAGE_TEST_CLICKHOUSE_RELEASE")
    if not port or not selected:
        pytest.fail("USAGE_TEST_CLICKHOUSE_PORT and USAGE_TEST_CLICKHOUSE_RELEASE are required")
    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=int(port),
        username="usage",
        password="usage-test",  # noqa: S106 - isolated test service
        tz_mode="aware",
    )
    database = "usage_evidence_" + uuid4().hex[:12]
    client.command(f"CREATE DATABASE {database}")
    adapter = session = None
    try:
        runtime = receipts_backend.start()
        context = OperationContext("test:usage", tenant="usage", scope={"runtime": uuid4().hex})
        meter, event = make_case(CASES[0])
        with runtime.context(context):
            for index in range(3):
                write_receipt(runtime, meter, replace(event, event_id=f"event-{index}"))
            rows = runtime.execute(runtime.catalog("evidence").query(resource=EVIDENCE_REF))
        records = rows.data["items"]
        records = [{key: row[key] for key in receipt_record(event)} for row in records]
        resource = replace(ReceiptSchemaProvider().load().resources[0], profile="analytical")
        compiled = ClickHouseSchemaCompiler().compile(
            database=database,
            resource=EVIDENCE_REF,
            resource_fingerprint=resource.fingerprint,
            schema=ReceiptSchemaProvider().documents()[0],
            record_profile="analytical",
        )
        raw = receipts_backend.config.bindings[0].to_dict()
        raw.update(
            id="clickhouse-evidence",
            adapterId="meridian.storage.clickhouse",
            engineProfile="clickhouse-standalone",
            engineVersion=selected,
            endpoint=f"http://127.0.0.1:{port}",
            physicalNamespace=database,
            requiredPhysicalFingerprint=None,
            settings={"layouts": [compiled.layout.to_dict()]},
        )
        initial = BindingConfig.from_mapping(raw, "bindings[0]")
        settings = ClickHouseSettings.from_binding(initial)
        manifest = capability_manifest(settings, selected)
        binding = replace(initial, required_capability_fingerprint=manifest.fingerprint)
        ClickHouseMigrator(client, settings).apply(plan_initial_migration("usage", (compiled,)))
        adapter = ClickHouseAdapterFactory().create(
            AdapterCreateContext(binding, SecretValue(b"usage"), SecretValue(b"usage-test"))
        )
        adapter.open()
        probe = adapter.probe()
        assert probe.observed_engine_version == client.command("SELECT version()")
        physical = PhysicalResource(
            EVIDENCE_REF, resource.fingerprint, compiled.layout.schema_fingerprint, resource.profile
        )
        assert adapter.verify_physical((physical,)).fingerprint.startswith("sha256:")
        session = adapter.open_session(transactional=False)
        catalog = EvidenceCatalogSurface()

        def request(expression, ctx=context):
            return ExecutionRequest(
                EvidenceCatalogProvider().normalize(expression),
                ctx,
                "usage-evidence",
                "usage-evidence",
                binding.id,
                1,
                manifest.fingerprint,
                1,
            )

        _verify_clickhouse_records(session, request, catalog, event, meter, records, context)
        with pytest.raises(MeridianError):
            adapter.open_session(transactional=True)
    finally:
        if session is not None:
            session.close()
        if adapter is not None:
            adapter.close()
        client.command(f"DROP DATABASE {database} SYNC")
        client.close()


def _verify_clickhouse_records(session, request, catalog, event, meter, records, context):
    append = request(catalog.append(resource=EVIDENCE_REF, data=records))
    first = session.execute(append)
    assert session.execute(append).data["batchId"] == first.data["batchId"]
    bounds = {
        "observedTime": {
            "gte": (event.recorded_at - timedelta(seconds=1)).isoformat(),
            "lt": (event.recorded_at + timedelta(seconds=1)).isoformat(),
        }
    }
    query = catalog.query(resource=EVIDENCE_REF, where=bounds, limit=2)
    page1 = session.execute(request(query)).data
    assert len(page1["items"]) == 2
    assert page1["cursor"]
    page2 = session.execute(
        request(catalog.query(resource=EVIDENCE_REF, where=bounds, limit=2, cursor=page1["cursor"]))
    ).data
    found = [*page1["items"], *page2["items"]]
    assert len(found) == 3
    assert {row["evidenceId"] for row in found} == {row["evidenceId"] for row in records}
    for row in found:
        restored = UsageEventV1.from_mapping(row["payload"])
        assert restored.fingerprint == row["evidenceId"]
        assert restored.value == event.normalized(meter).value
    assert not session.execute(request(query, replace(context, tenant="other"))).data["items"]
    with pytest.raises(MeridianError):
        session.execute(request(catalog.query(resource=EVIDENCE_REF)))
