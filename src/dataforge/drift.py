"""Pure, deterministic drift comparison for immutable dataset versions."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from .hashing import canonical_json

DRIFT_RULES_VERSION = "1.0.1"
ROW_COUNT_RELATIVE_THRESHOLD = 0.10
MISSING_RATE_ABSOLUTE_THRESHOLD = 0.05
DUPLICATE_RATE_ABSOLUTE_THRESHOLD = 0.05


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def _observed_types(records: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    fields = sorted({field for record in records for field in record})
    return {
        field: sorted(
            {
                _value_type(record[field])
                for record in records
                if field in record and record[field] is not None
            }
        )
        for field in fields
    }


def _missing_rates(
    records: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> Dict[str, float | None]:
    if not records:
        return {field: None for field in fields}
    return {
        field: round(
            sum(1 for record in records if field not in record or record[field] is None)
            / len(records),
            12,
        )
        for field in fields
    }


def _duplicate_stats(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not records:
        return {"rows": 0, "unique_rows": 0, "duplicate_rows": 0, "duplicate_rate": None}
    fingerprints = [canonical_json(record) for record in records]
    unique = len(set(fingerprints))
    duplicate_rows = len(records) - unique
    return {
        "rows": len(records),
        "unique_rows": unique,
        "duplicate_rows": duplicate_rows,
        "duplicate_rate": round(duplicate_rows / len(records), 12),
    }


def compare_drift(
    baseline_records: Sequence[Mapping[str, Any]],
    candidate_records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return server-computed metrics and a conservative drift verdict.

    Thresholds are fixed in code, never accepted from the client. Empty versions or
    versions without observable fields are deliberately inconclusive.
    """

    baseline_types = _observed_types(baseline_records)
    candidate_types = _observed_types(candidate_records)
    baseline_fields = set(baseline_types)
    candidate_fields = set(candidate_types)
    all_fields = sorted(baseline_fields | candidate_fields)
    added_fields = sorted(candidate_fields - baseline_fields)
    removed_fields = sorted(baseline_fields - candidate_fields)
    type_changes = [
        {
            "field": field,
            "baseline_types": baseline_types[field],
            "candidate_types": candidate_types[field],
        }
        for field in sorted(baseline_fields & candidate_fields)
        if baseline_types[field] != candidate_types[field]
    ]

    baseline_missing = _missing_rates(baseline_records, all_fields)
    candidate_missing = _missing_rates(candidate_records, all_fields)
    missing_changes = []
    for field in all_fields:
        baseline_rate = baseline_missing[field]
        candidate_rate = candidate_missing[field]
        delta = (
            None
            if baseline_rate is None or candidate_rate is None
            else round(candidate_rate - baseline_rate, 12)
        )
        missing_changes.append(
            {
                "field": field,
                "baseline_rate": baseline_rate,
                "candidate_rate": candidate_rate,
                "delta": delta,
                "absolute_delta": None if delta is None else abs(delta),
            }
        )

    baseline_duplicates = _duplicate_stats(baseline_records)
    candidate_duplicates = _duplicate_stats(candidate_records)
    baseline_count = len(baseline_records)
    candidate_count = len(candidate_records)
    relative_row_change = (
        None
        if baseline_count == 0
        else round((candidate_count - baseline_count) / baseline_count, 12)
    )
    duplicate_rate_change = (
        None
        if baseline_duplicates["duplicate_rate"] is None
        or candidate_duplicates["duplicate_rate"] is None
        else round(
            candidate_duplicates["duplicate_rate"] - baseline_duplicates["duplicate_rate"],
            12,
        )
    )

    insufficient_reasons = []
    if not baseline_records:
        insufficient_reasons.append("baseline_has_no_rows")
    if not candidate_records:
        insufficient_reasons.append("candidate_has_no_rows")
    if not baseline_fields:
        insufficient_reasons.append("baseline_has_no_observable_fields")
    if not candidate_fields:
        insufficient_reasons.append("candidate_has_no_observable_fields")

    drift_triggers = []
    if added_fields:
        drift_triggers.append("fields_added")
    if removed_fields:
        drift_triggers.append("fields_removed")
    if type_changes:
        drift_triggers.append("observed_types_changed")
    if relative_row_change is not None and abs(relative_row_change) > ROW_COUNT_RELATIVE_THRESHOLD:
        drift_triggers.append("row_count_change_exceeds_threshold")
    if any(
        change["absolute_delta"] is not None
        and change["absolute_delta"] > MISSING_RATE_ABSOLUTE_THRESHOLD
        for change in missing_changes
    ):
        drift_triggers.append("missing_rate_change_exceeds_threshold")
    if (
        duplicate_rate_change is not None
        and abs(duplicate_rate_change) > DUPLICATE_RATE_ABSOLUTE_THRESHOLD
    ):
        drift_triggers.append("duplicate_rate_change_exceeds_threshold")

    if insufficient_reasons:
        verdict = "INSUFFICIENT"
    elif drift_triggers:
        verdict = "DRIFTED"
    else:
        verdict = "STABLE"

    return {
        "verdict": verdict,
        "metrics": {
            "schema": {
                "baseline_observed_types": baseline_types,
                "candidate_observed_types": candidate_types,
                "added_fields": added_fields,
                "removed_fields": removed_fields,
                "type_changes": type_changes,
            },
            "missingness": {"fields": missing_changes},
            "rows": {
                "baseline_count": baseline_count,
                "candidate_count": candidate_count,
                "absolute_change": candidate_count - baseline_count,
                "relative_change": relative_row_change,
            },
            "duplicates": {
                "baseline": baseline_duplicates,
                "candidate": candidate_duplicates,
                "rate_change": duplicate_rate_change,
                "absolute_rate_change": (
                    None if duplicate_rate_change is None else abs(duplicate_rate_change)
                ),
            },
            "thresholds": {
                "row_count_relative": ROW_COUNT_RELATIVE_THRESHOLD,
                "missing_rate_absolute": MISSING_RATE_ABSOLUTE_THRESHOLD,
                "duplicate_rate_absolute": DUPLICATE_RATE_ABSOLUTE_THRESHOLD,
            },
            "drift_triggers": drift_triggers,
            "insufficient_reasons": insufficient_reasons,
        },
        "rules_version": DRIFT_RULES_VERSION,
    }
