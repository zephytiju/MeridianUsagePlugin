# SPDX-License-Identifier: Apache-2.0
"""Fixed synthetic cases shared with the independently installed v1 reproducer."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from meridian_storage.plugins.usage import (
    MeterV1,
    UnitTransform,
    UsageCorrelation,
    UsageEventV1,
    UsageScope,
    UsageWindow,
)

CASES = (
    ("default12-input0", None, "12", False),
    ("default12-input12", None, "12.000000000000", False),
    ("default12-input18", None, "12.000000000000000000", False),
    ("scale18-input12", 18, "12.000000000000", False),
    ("scale18-input18-control", 18, "12.000000000000000000", False),
    ("zero", None, "-0.000000", False),
    ("exact-conversion", None, "12000.000", True),
    ("scale0", 0, "12.00", False),
    ("precision76", 18, "9" * 58 + "." + "9" * 18, False),
    ("original-digit-limit", 18, "0." + "0" * 17 + "1" + "0" * 999, False),
)


def make_case(case):
    name, scale, value, conversion = case
    meter = MeterV1(
        "test.requests",
        1,
        "requests",
        "request",
        **({} if scale is None else {"scale": scale, "precision": 76 if scale == 18 else 38}),
        transforms={"input": UnitTransform(Decimal("0.001"))} if conversion else {},
    )
    end = datetime(2026, 9, 5, 12, tzinfo=UTC)
    event = UsageEventV1(
        name,
        UsageScope({"tenant": "synthetic", "runtime": "decimal-test"}),
        "subject",
        meter.meter_id,
        1,
        UsageWindow(end - timedelta(minutes=1), end),
        Decimal(value),
        "input" if conversion else "request",
        recorded_at=end,
        correlation=UsageCorrelation(request_id="decimal-test"),
    )
    return meter, event
