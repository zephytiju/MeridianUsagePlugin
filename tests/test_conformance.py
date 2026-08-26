# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from meridian_storage.plugins.usage import (
    CheckpointConflict,
    ClaimUnavailable,
    InvalidUsage,
    UnknownMeter,
    UsageConflict,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.conformance
def test_contract_instance_validates_and_goldens_verify() -> None:
    schema = json.loads((ROOT / "contracts" / "usage-plugin.v1.json").read_text())
    contract = json.loads((ROOT / "contracts" / "conformance" / "plugin-contract.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)
    process = subprocess.run(  # noqa: S603
        [sys.executable, "scripts/verify_contracts.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(process.stdout)
    assert evidence["status"] == "passed"
    assert evidence["formatVersion"] == "meridian.usage.conformance.v1"
    assert evidence["fingerprint"].startswith("sha256:")
    assert evidence["sourceFilesChecked"] >= 12


@pytest.mark.conformance
@pytest.mark.parametrize(
    ("error", "code", "category", "requirement"),
    [
        (
            InvalidUsage("bad input", requirement="usage.example"),
            "MERIDIAN_USAGE_INVALID",
            "VALIDATION",
            "usage.example",
        ),
        (
            UnknownMeter("meter@1"),
            "MERIDIAN_USAGE_METER_NOT_FOUND",
            "NOT_FOUND",
            "usage.meter.exists",
        ),
        (
            UsageConflict("identity"),
            "MERIDIAN_USAGE_IMMUTABLE_CONFLICT",
            "CONFLICT",
            "usage.immutable.identity",
        ),
        (
            CheckpointConflict("checkpoint"),
            "MERIDIAN_USAGE_CHECKPOINT_CONFLICT",
            "CONFLICT",
            "usage.checkpoint.cas",
        ),
        (
            ClaimUnavailable("claim"),
            "MERIDIAN_USAGE_CLAIM_UNAVAILABLE",
            "TRANSIENT",
            "usage.aggregation.claim",
        ),
    ],
)
def test_stable_error_envelopes(error, code: str, category: str, requirement: str) -> None:
    payload = error.to_dict()
    assert payload["code"] == code
    assert payload["category"] == category
    assert payload["requirement"] == requirement
    assert "traceback" not in payload
    assert "cause" not in payload
