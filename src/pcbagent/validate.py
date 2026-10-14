"""A small JSON-Schema subset validator with error messages written for an LLM.

Supported keywords: type, properties, required, additionalProperties, items,
enum, const, minimum, maximum, exclusiveMinimum, minLength, maxLength,
minItems, maxItems, pattern, anyOf, oneOf, description (ignored), default
(ignored), title (ignored), $comment (ignored).

Why not the ``jsonschema`` package: the core stays dependency-free, and these
messages name the exact path and the fix ("spec.requirements[1].tolerance:
expected an object with 'pct' or 'abs'"), which is what a small local model
needs to repair its output in one try.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _is_type(value: Any, t: str) -> bool:
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def _describe(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return f"the number {value!r}"
    if isinstance(value, str):
        return f"the string {value[:40]!r}"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    if value is None:
        return "null"
    return type(value).__name__


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    _validate(value, schema, path, errors)
    return errors


def _validate(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    if "anyOf" in schema or "oneOf" in schema:
        options = schema.get("anyOf") or schema.get("oneOf")
        branch_errors = []
        for option in options:
            sub: list[str] = []
            _validate(value, option, path, sub)
            if not sub:
                break
            branch_errors.append(sub)
        else:
            hint = schema.get("description")
            best = min(branch_errors, key=len)
            if hint:
                errors.append(f"{path}: {hint}")
            else:
                errors.extend(best)
            return
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(value, x) for x in types):
            errors.append(f"{path}: expected {' or '.join(types)}, got {_describe(value)}")
            return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(x) for x in schema["enum"])
        errors.append(f"{path}: {_describe(value)} is not one of {allowed}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above the maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: {value} must be greater than {schema['exclusiveMinimum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: must not be empty" if schema["minLength"] == 1
                          else f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            hint = schema.get("patternHint") or f"must match {schema['pattern']}"
            errors.append(f"{path}: {value!r} {hint}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{i}]", errors)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required field '{key}'")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}", errors)
            elif extra is False:
                allowed = ", ".join(sorted(props)) or "none"
                errors.append(f"{path}: unknown field '{key}' (allowed: {allowed})")
            elif isinstance(extra, dict):
                _validate(item, extra, f"{path}.{key}", errors)
