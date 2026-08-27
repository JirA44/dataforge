"""Deterministic evaluation of immutable dataset versions against data contracts."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Sequence

from .hashing import canonical_json

CONTRACT_RULES_VERSION = "1.0.0"


def observable_type(value: Any) -> str:
    """Return the JSON type name without treating booleans as integers."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unsupported"


def evaluate_contract(
    records: Sequence[Mapping[str, Any]], contract: Mapping[str, Any]
) -> Dict[str, Any]:
    """Compute evidence and violations; callers cannot influence the verdict."""

    row_count = len(records)
    violations: List[Dict[str, Any]] = []
    fields = contract["fields"]
    observed_fields = sorted({key for record in records for key in record})

    if row_count == 0:
        return {
            "verdict": "INSUFFICIENT",
            "violations": [],
            "insufficient_reasons": ["no_rows_to_evaluate"],
            "metrics": {
                "row_count": 0,
                "observed_fields": [],
                "duplicate_rows": 0,
                "duplicate_rate": None,
                "fields": {},
            },
            "rules_version": CONTRACT_RULES_VERSION,
        }

    if row_count < contract["min_rows"]:
        violations.append(
            {
                "code": "row_count_below_minimum",
                "observed": row_count,
                "expected": {"minimum": contract["min_rows"]},
            }
        )
    max_rows = contract.get("max_rows")
    if max_rows is not None and row_count > max_rows:
        violations.append(
            {
                "code": "row_count_above_maximum",
                "observed": row_count,
                "expected": {"maximum": max_rows},
            }
        )

    canonical_rows = [canonical_json(record) for record in records]
    duplicate_rows = row_count - len(set(canonical_rows))
    duplicate_rate = duplicate_rows / row_count
    if duplicate_rate > contract["max_duplicate_rate"]:
        violations.append(
            {
                "code": "duplicate_rate_exceeded",
                "observed": duplicate_rate,
                "expected": {"maximum": contract["max_duplicate_rate"]},
            }
        )

    if not contract["allow_extra"]:
        for extra in sorted(set(observed_fields) - set(fields)):
            violations.append(
                {"code": "extra_field", "field": extra, "observed": True, "expected": False}
            )

    field_metrics: Dict[str, Any] = {}
    for field_name in sorted(fields):
        rule = fields[field_name]
        present_values = [record[field_name] for record in records if field_name in record]
        null_count = sum(value is None for value in present_values)
        absent_count = row_count - len(present_values)
        missing_count = absent_count + null_count
        missing_rate = missing_count / row_count
        non_null_values = [value for value in present_values if value is not None]
        observed_types = sorted({observable_type(value) for value in non_null_values})

        field_metrics[field_name] = {
            "present_count": len(present_values),
            "absent_count": absent_count,
            "null_count": null_count,
            "missing_rate": missing_rate,
            "observed_types": observed_types,
            "non_null_count": len(non_null_values),
        }

        if rule["required"] and not present_values:
            violations.append(
                {
                    "code": "required_field_unobserved",
                    "field": field_name,
                    "observed": False,
                    "expected": True,
                }
            )
        if null_count and not rule["nullable"]:
            violations.append(
                {
                    "code": "null_not_allowed",
                    "field": field_name,
                    "observed": null_count,
                    "expected": 0,
                }
            )
        if missing_rate > rule["max_missing_rate"]:
            violations.append(
                {
                    "code": "missing_rate_exceeded",
                    "field": field_name,
                    "observed": missing_rate,
                    "expected": {"maximum": rule["max_missing_rate"]},
                }
            )

        invalid_types = sorted(set(observed_types) - set(rule["types"]))
        if invalid_types:
            violations.append(
                {
                    "code": "type_not_allowed",
                    "field": field_name,
                    "observed": invalid_types,
                    "expected": {"allowed": rule["types"]},
                }
            )

        if rule["unique"]:
            value_counts = Counter(canonical_json(value) for value in non_null_values)
            duplicate_values = sum(count - 1 for count in value_counts.values() if count > 1)
            field_metrics[field_name]["duplicate_values"] = duplicate_values
            if duplicate_values:
                violations.append(
                    {
                        "code": "uniqueness_violated",
                        "field": field_name,
                        "observed": duplicate_values,
                        "expected": 0,
                    }
                )

    return {
        "verdict": "VIOLATION" if violations else "COMPATIBLE",
        "violations": violations,
        "insufficient_reasons": [],
        "metrics": {
            "row_count": row_count,
            "observed_fields": observed_fields,
            "duplicate_rows": duplicate_rows,
            "duplicate_rate": duplicate_rate,
            "fields": field_metrics,
        },
        "rules_version": CONTRACT_RULES_VERSION,
    }
