"""Strict JSON helpers used by every persisted or CLI-facing artifact.

Python's default JSON encoder emits ``Infinity`` and ``NaN`` even though they
are not valid JSON.  The UI and Node consumers for this project require
standards-compliant JSON, so non-finite diagnostic values become JSON null and
``allow_nan`` is always disabled.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def json_safe(value: Any) -> Any:
    """Return a recursively standard-JSON-compatible representation."""

    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, complex):
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            return None
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def dumps_json(value: Any, *, indent: int = 2) -> str:
    return json.dumps(
        json_safe(value), ensure_ascii=False, indent=indent, allow_nan=False
    )


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r} is forbidden")


def loads_json(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_nonstandard_constant)


def load_json(path: str | Path) -> Any:
    return loads_json(Path(path).read_text(encoding="utf-8"))


def dump_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.write_text(dumps_json(value) + "\n", encoding="utf-8")
