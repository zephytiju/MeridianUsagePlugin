# SPDX-License-Identifier: Apache-2.0
"""Run from a coherent, separately installed release environment (no source override)."""

from __future__ import annotations

import json
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from integration.decimal_cases import CASES, make_case
from integration.postgres_backend import USAGE_RESOURCES, LiveBackend
from meridian_storage import OperationContext
from meridian_storage.plugins.usage import UsageConflict, UsageRepository


def installed_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def main():
    packages = {
        name: installed_version(name)
        for name in (
            "meridian-plugin-cost",
            "meridian-plugin-usage",
            "meridian-storage-core",
            "meridian-storage-semantics",
            "meridian-storage-postgresql",
        )
    }
    if packages["meridian-plugin-usage"] not in {"1.0.0", "1.0.2"}:
        raise AssertionError("run from an independently installed v1 release")
    if packages["meridian-plugin-cost"] is not None and not (
        packages["meridian-plugin-cost"] == packages["meridian-plugin-usage"] == "1.0.0"
    ):
        raise AssertionError("Cost 1.0.0 must retain its exact Usage 1.0.0 dependency")
    backend = LiveBackend(os.environ["USAGE_TEST_POSTGRES_DSN"])
    cases = []
    try:
        runtime = backend.start()
        repository = UsageRepository(runtime, USAGE_RESOURCES)
        for case in CASES:
            meter, event = make_case(case)
            ctx = OperationContext("test:publisher", tenant="synthetic", scope={"runtime": case[0]})
            with runtime.context(ctx):
                receipt = repository.record(event, meter=meter)
                readback = repository.get_event(event.scope, event.event_id)
                try:
                    replay = str(repository.record(event, meter=meter).status)
                except UsageConflict:
                    replay = "UsageConflict"
            cases.append(
                {
                    "name": case[0],
                    "meter": meter.to_dict(),
                    "input": event.to_dict(),
                    "normalized": receipt.event.to_dict(),
                    "legacyReadbackFingerprint": readback.fingerprint,
                    "legacyFingerprintEqual": readback.fingerprint == receipt.event.fingerprint,
                    "legacyReplay": replay,
                }
            )
        if [item["legacyFingerprintEqual"] for item in cases] != [
            False,
            False,
            False,
            False,
            True,
            True,
            False,
            False,
            True,
            False,
        ]:
            raise AssertionError("legacy result differs from the reproduction baseline")
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/legacy-decimal-events.json"
        if cases != json.loads(fixture.read_text())["cases"]:
            raise AssertionError("released v1 records differ from frozen compatibility fixtures")
    finally:
        backend.close()
    print(
        json.dumps(
            {"packages": packages, "kind": "real PostgreSQL v1 reproduction", "cases": cases},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
