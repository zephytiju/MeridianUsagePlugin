# SPDX-License-Identifier: Apache-2.0
"""Exact storage checks and authenticated recovery of v1 decimal text.

V1 fingerprints include lexical decimal scale, which NUMERIC storage discards.
Recover only equivalent representations and authenticate the entire v1 payload;
never substitute a stored digest for a computed fingerprint.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import cast

from ._canonical import canonical_bytes, decimal_value, require_fingerprint
from .errors import DecimalOverflow, InvalidUsageResult


def storage_decimal(value: object, name: str) -> Decimal:
    selected = decimal_value(value, name)
    _, digits, exponent = selected.as_tuple()
    scale = cast(int, exponent)
    # Decimal.normalize() applies the ambient precision and can round first.
    while len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        scale += 1
    if max(0, -scale) > 18 or max(1, selected.adjusted() + 1) > 58:
        raise DecimalOverflow(f"{name} must fit the released Decimal(76, 18) Usage schema")
    return selected


def _representations(value: object, *, meter_value: bool) -> Iterator[str | None]:
    if value is None:
        yield None
        return
    selected = storage_decimal(value, "stored event decimal")
    if selected.is_zero():
        yield "0"
        return
    text = format(selected, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    minimum_scale = len(text.partition(".")[2])
    # Normalized values have a legal meter scale (0..18). Original values
    # retain the input's at-most-1000 coefficient digits, including padding.
    maximum_scale = 18 if meter_value else 1000 - selected.adjusted() - 1
    yield text
    if "." not in text:
        text += "."
    for _ in range(minimum_scale, maximum_scale):
        text += "0"
        yield text


def _encoded_decimal(value: str | None) -> bytes:
    return b"null" if value is None else b'"' + value.encode("ascii") + b'"'


def restore_event_decimals(
    content: Mapping[str, object], expected_fingerprint: object
) -> tuple[Decimal, Decimal | None]:
    """Recover lost scales only if a complete, unchanged v1 digest matches.

    There are at most 19 * 1000 candidates. Encode fixed fields once to keep
    corrupt records and unusually padded legal inputs bounded and inexpensive.
    The fragments use the same sorted keys and canonical encoder as v1.
    """
    expected = require_fingerprint(expected_fingerprint).removeprefix("sha256:")
    parts = [b"{"]
    for index, (key, value) in enumerate(sorted(content.items())):
        parts.append((b"," if index else b"") + canonical_bytes(key) + b":")
        if key in {"originalValue", "value"}:
            parts.append(b"")
        else:
            parts.append(canonical_bytes(value))
    parts.append(b"}")
    # The two omitted values are in lexical key order: originalValue, value.
    first = parts.index(b"")
    second = parts.index(b"", first + 1)
    prefix = hashlib.sha256(b"".join(parts[:first]))
    middle = b"".join(parts[first + 1 : second])
    suffix = b"".join(parts[second + 1 :])
    values = tuple(_representations(content["value"], meter_value=True))
    for original in _representations(content["originalValue"], meter_value=False):
        partial = prefix.copy()
        partial.update(_encoded_decimal(original) + middle)
        for value in values:
            digest = partial.copy()
            digest.update(_encoded_decimal(value) + suffix)
            if digest.hexdigest() == expected:
                return Decimal(cast(str, value)), None if original is None else Decimal(original)
    raise InvalidUsageResult("stored fingerprint does not match the complete event content")
