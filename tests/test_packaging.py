# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.packaging
def test_built_distribution_has_one_package_and_complete_metadata(tmp_path: Path) -> None:
    environment = {**os.environ, "SOURCE_DATE_EPOCH": "1787702400"}
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    process = subprocess.run(  # noqa: S603
        [sys.executable, "scripts/verify_artifacts.py", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(process.stdout)
    assert evidence["status"] == "passed"
    assert evidence["package"] == "meridian-plugin-usage"
    assert {item["file"].split("-", 1)[0] for item in evidence["artifacts"]} == {
        "meridian_plugin_usage"
    }
