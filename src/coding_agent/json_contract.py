"""Strict JSON snapshots shared by Kernel-owned public contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, cast


def json_object_snapshot(value: object, *, label: str) -> dict[str, Any]:
    """Validate a JSON object recursively and return its decoded owned snapshot."""

    active_containers: set[int] = set()

    def normalize(item: object, path: str) -> object:
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active_containers:
                raise ValueError(f"{label} contains a recursive container at {path}")
            active_containers.add(identity)
            try:
                normalized: dict[str, object] = {}
                for key, nested in item.items():
                    if type(key) is not str:
                        raise ValueError(f"{label} keys must be strings at {path}")
                    normalized[key] = normalize(nested, f"{path}.{key}")
                return normalized
            finally:
                active_containers.remove(identity)
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in active_containers:
                raise ValueError(f"{label} contains a recursive container at {path}")
            active_containers.add(identity)
            try:
                return [normalize(nested, f"{path}[{index}]") for index, nested in enumerate(item)]
            finally:
                active_containers.remove(identity)
        if item is None or type(item) in {str, int, bool}:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{label} numbers must be finite at {path}")
            return item
        raise ValueError(f"{label} contains a non-JSON value at {path}")

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a dictionary")
    try:
        normalized = normalize(value, "$")
    except RecursionError as exc:
        raise ValueError(f"{label} nesting is too deep") from exc
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable: {exc}") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - top-level check owns this
        raise ValueError(f"{label} must be a dictionary")
    return cast(dict[str, Any], decoded)
