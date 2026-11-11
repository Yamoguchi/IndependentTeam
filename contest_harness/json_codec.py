"""JSON helpers shared by task inputs, model messages and artifacts."""

import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def strict_json_loads(raw: str):
    def unique(pairs):
        arguments = dict(pairs)
        if len(arguments) != len(pairs):
            raise ValueError("Duplicate argument name")
        return arguments

    def invalid_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
