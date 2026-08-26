# SPDX-License-Identifier: Apache-2.0
"""Require byte-for-byte reproducible wheel and sdist directories."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    arguments = parser.parse_args()
    first = _hashes(arguments.first)
    second = _hashes(arguments.second)
    if len(first) != 2 or first != second:
        raise SystemExit(f"distribution artifacts are not reproducible: {first!r} != {second!r}")
    print(json.dumps({"artifacts": first, "reproducible": True}, sort_keys=True))


if __name__ == "__main__":
    main()
