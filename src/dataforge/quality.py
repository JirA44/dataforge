"""Pure, deterministic dataset quality checks."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .hashing import canonical_json

QUALITY_RULES_VERSION = "1.0.0"
PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT"


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _check_missing(
    records: Sequence[Mapping[str, Any]], schema_spec: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    if not records:
        return {
            "name": "missing",
            "status": INSUFFICIENT,
            "reason": "No records to evaluate",
            "missing_values": 0,
            "checked_cells": 0,
            "missing_rate": None,
        }

    fields_spec = (schema_spec or {}).get("fields", {})
    if fields_spec:
        fields = sorted(
            name for name, definition in fields_spec.items() if definition.get("required", True)
        )
        basis = "declared_required_fields"
    else:
        fields = sorted({key for record in records for key in record.keys()})
        basis = "inferred_union_fields"

    if not fields:
        return {
            "name": "missing",
            "status": INSUFFICIENT,
            "reason": "No fields available to evaluate",
            "missing_values": 0,
            "checked_cells": 0,
            "missing_rate": None,
        }

    missing = sum(
        1
        for record in records
        for field in fields
        if field not in record or record[field] is None
    )
    checked = len(records) * len(fields)
    return {
        "name": "missing",
        "status": PASS if missing == 0 else FAIL,
        "basis": basis,
        "fields": fields,
        "missing_values": missing,
        "checked_cells": checked,
        "missing_rate": missing / checked,
    }


def _check_duplicates(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not records:
        return {
            "name": "duplicates",
            "status": INSUFFICIENT,
            "reason": "No records to evaluate",
            "duplicate_rows": 0,
            "duplicate_rate": None,
        }

    seen = set()
    duplicates = 0
    for record in records:
        fingerprint = canonical_json(record)
        if fingerprint in seen:
            duplicates += 1
        else:
            seen.add(fingerprint)
    return {
        "name": "duplicates",
        "status": PASS if duplicates == 0 else FAIL,
        "duplicate_rows": duplicates,
        "unique_rows": len(seen),
        "duplicate_rate": duplicates / len(records),
    }


def _check_schema(
    records: Sequence[Mapping[str, Any]], schema_spec: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    if not records:
        return {
            "name": "schema",
            "status": INSUFFICIENT,
            "reason": "No records to evaluate",
            "violations": [],
            "violation_count": 0,
        }
    if not schema_spec or not schema_spec.get("fields"):
        return {
            "name": "schema",
            "status": INSUFFICIENT,
            "reason": "A declared schema is required for verification",
            "violations": [],
            "violation_count": 0,
        }

    fields_spec = schema_spec["fields"]
    allow_extra = bool(schema_spec.get("allow_extra", False))
    violations: List[Dict[str, Any]] = []
    for row_index, record in enumerate(records):
        for field, definition in fields_spec.items():
            required = definition.get("required", True)
            if field not in record or record[field] is None:
                if required:
                    violations.append(
                        {"row": row_index, "field": field, "issue": "required_value_missing"}
                    )
                continue
            expected = definition["type"]
            if not _matches_type(record[field], expected):
                violations.append(
                    {
                        "row": row_index,
                        "field": field,
                        "issue": "type_mismatch",
                        "expected": expected,
                        "actual": type(record[field]).__name__,
                    }
                )
        if not allow_extra:
            for field in sorted(set(record) - set(fields_spec)):
                violations.append({"row": row_index, "field": field, "issue": "extra_field"})

    return {
        "name": "schema",
        "status": PASS if not violations else FAIL,
        "allow_extra": allow_extra,
        "violations": violations[:100],
        "violation_count": len(violations),
        "violations_truncated": len(violations) > 100,
    }


def evaluate_quality(
    records: Sequence[Mapping[str, Any]],
    schema_spec: Optional[Mapping[str, Any]],
    provenance_valid: bool,
    provenance_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculate all mandatory checks and a conservative overall verdict.

    A version is VERIFIED only when every check is PASS. Any FAIL produces
    REJECTED; an inconclusive check produces INSUFFICIENT.
    """

    checks = [
        _check_missing(records, schema_spec),
        _check_duplicates(records),
        _check_schema(records, schema_spec),
        {
            "name": "provenance",
            "status": PASS if provenance_valid else FAIL,
            "reason": provenance_reason or (
                "Content and provenance chain hashes are valid"
                if provenance_valid
                else "Content or provenance chain hash is invalid"
            ),
        },
    ]
    statuses = {check["status"] for check in checks}
    if FAIL in statuses:
        verdict = "REJECTED"
    elif INSUFFICIENT in statuses:
        verdict = "INSUFFICIENT"
    else:
        verdict = "VERIFIED"
    return {"rules_version": QUALITY_RULES_VERSION, "verdict": verdict, "checks": checks}

