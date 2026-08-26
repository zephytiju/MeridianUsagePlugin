# Contributing

Use Python 3.12 through 3.14. Create a virtual environment, install the exact
test extra, and run:

    python -m pip install pip==26.2
    python -m pip install -e '.[test]'
    ruff format --check .
    ruff check .
    mypy src
    python scripts/verify_contracts.py
    pytest

Every source file must retain its Apache-2.0 SPDX header. Public behavior
changes require tests, compatibility evidence, and a design update when they
alter an approved Meridian interface or architecture boundary.
