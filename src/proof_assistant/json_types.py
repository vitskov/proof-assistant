"""Validated JSON value types for persistence and protocol boundaries."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | Mapping[str, JSONValue] | Sequence[JSONValue]
type JSONObject = dict[str, JSONValue]
type JSONArray = list[JSONValue]


class JSONTypeError(ValueError):
    """A decoded or supplied value is not representable as JSON."""


def as_json_value(value: object, *, path: str = "$") -> JSONValue:
    """Validate and narrow an arbitrary decoded value recursively."""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JSONTypeError(f"{path} contains non-finite float")
        return value
    if isinstance(value, Mapping):
        result: JSONObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise JSONTypeError(f"{path} contains a non-string object key")
            result[key] = as_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            as_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise JSONTypeError(f"{path} contains non-JSON value {type(value).__name__}")


def load_json(text: str) -> JSONValue:
    """Decode JSON without allowing ``Any`` to escape the validation seam."""

    decoded: object = json.loads(text)
    return as_json_value(decoded)


def json_object(value: object, *, path: str = "$") -> JSONObject:
    """Validate a value and require a JSON object at its root."""

    validated = as_json_value(value, path=path)
    if not isinstance(validated, dict):
        raise JSONTypeError(f"{path} must be a JSON object")
    return validated
