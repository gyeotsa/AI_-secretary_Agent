"""Schema validation helpers shared by planners, routers and training exports."""
from __future__ import annotations

from typing import Any, Dict
import json
import re
from jsonschema import Draft202012Validator


class StructuredOutputError(ValueError):
    pass


def parse_json_object(value: Any, schema: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(value, dict):
        payload = value
    else:
        text = str(value or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"JSON 객체가 아닙니다: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise StructuredOutputError("최상위 출력은 JSON 객체여야 합니다.")
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise StructuredOutputError("; ".join(error.message for error in errors))
    return payload
