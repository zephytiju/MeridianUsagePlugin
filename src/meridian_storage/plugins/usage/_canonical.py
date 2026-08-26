# SPDX-License-Identifier: Apache-2.0
"""Small deterministic validators and canonical encoders for Usage records."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, cast

from .errors import InvalidUsage

type JsonScalar = str | bool | int | float | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")
_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.%/*^()+-]{0,63}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


def bounded_text(value: object, name: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InvalidUsage(f"{name} must be a non-empty bounded printable string")
    return value


def logical_name(value: object, name: str) -> str:
    selected = bounded_text(value, name, 128)
    if _NAME.fullmatch(selected) is None:
        raise InvalidUsage(f"{name} must be a bounded logical name")
    return selected


def token(value: object, name: str) -> str:
    selected = bounded_text(value, name)
    if _TOKEN.fullmatch(selected) is None:
        raise InvalidUsage(f"{name} must be a bounded contract token")
    return selected


def unit_name(value: object) -> str:
    selected = bounded_text(value, "unit", 64)
    if _UNIT.fullmatch(selected) is None:
        raise InvalidUsage("unit must use the bounded portable unit grammar")
    return selected


def utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidUsage(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def iso_datetime(value: datetime) -> str:
    return utc_datetime(value, "timestamp").isoformat().replace("+00:00", "Z")


def parse_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return utc_datetime(value, name)
    if not isinstance(value, str):
        raise InvalidUsage(f"{name} must be an RFC 3339 timestamp")
    try:
        return utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except (OverflowError, ValueError) as exc:
        raise InvalidUsage(f"{name} must be an RFC 3339 timestamp") from exc


def decimal_value(value: object, name: str = "value") -> Decimal:
    if isinstance(value, bool | float):
        raise InvalidUsage(f"{name} must be Decimal, int, or a decimal string")
    try:
        selected = value if isinstance(value, Decimal) else Decimal(cast(str | int, value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidUsage(f"{name} must be an exact finite Decimal") from exc
    if not selected.is_finite():
        raise InvalidUsage(f"{name} must be an exact finite Decimal")
    if len(selected.as_tuple().digits) > 1000 or abs(selected.adjusted()) > 1000:
        raise InvalidUsage(f"{name} exceeds the bounded Decimal input domain")
    return Decimal(0) if selected.is_zero() else selected


def decimal_text(value: Decimal) -> str:
    selected = decimal_value(value)
    return format(selected, "f")


def string_map(
    values: Mapping[Any, Any],
    name: str,
    *,
    maximum_entries: int = 32,
    key_names: bool = True,
    value_maximum: int = 1024,
) -> Mapping[str, str]:
    if not isinstance(values, Mapping) or len(values) > maximum_entries:
        raise InvalidUsage(f"{name} must be a mapping with at most {maximum_entries} entries")
    result: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = (
            logical_name(raw_key, f"{name} key")
            if key_names
            else bounded_text(raw_key, f"{name} key", 128)
        )
        result[key] = bounded_text(raw_value, f"{name} value", value_maximum)
    return MappingProxyType(dict(sorted(result.items())))


def json_value(value: object, name: str, *, depth: int = 0) -> JsonValue:
    if depth > 8:
        raise InvalidUsage(f"{name} exceeds the maximum nesting depth")
    if value is None or isinstance(value, str | bool | int):
        if isinstance(value, str):
            bounded_text(value, name, 4096)
        elif (
            isinstance(value, int) and not isinstance(value, bool) and not -(2**63) <= value < 2**63
        ):
            raise InvalidUsage(f"{name} integer must fit signed 64-bit values")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidUsage(f"{name} floating-point value must be finite")
        return value
    if isinstance(value, Mapping):
        if len(value) > 64:
            raise InvalidUsage(f"{name} object has too many entries")
        result: dict[str, JsonValue] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = bounded_text(raw_key, f"{name} key", 128)
            result[key] = json_value(item, name, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        if len(value) > 256:
            raise InvalidUsage(f"{name} array has too many entries")
        return [json_value(item, name, depth=depth + 1) for item in value]
    raise InvalidUsage(f"{name} contains a non-JSON value")


def canonical_bytes(value: object) -> bytes:
    normalized = json_value(value, "canonical value")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def fingerprint(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_bytes(value)).hexdigest()}"


def require_fingerprint(value: object, name: str = "fingerprint") -> str:
    selected = bounded_text(value, name, 71)
    if _FINGERPRINT.fullmatch(selected) is None:
        raise InvalidUsage(f"{name} must be a sha256 fingerprint")
    return selected


__all__ = [
    "JsonValue",
    "bounded_text",
    "canonical_bytes",
    "decimal_text",
    "decimal_value",
    "fingerprint",
    "iso_datetime",
    "json_value",
    "logical_name",
    "parse_datetime",
    "require_fingerprint",
    "string_map",
    "token",
    "unit_name",
    "utc_datetime",
]
