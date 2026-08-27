"""Deterministic directional compatibility for immutable data contracts."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

COMPATIBILITY_RULES_VERSION = "1.0.0"


def _absence_allowed(rule: Mapping[str, Any]) -> bool:
    return not rule["required"] and rule["max_missing_rate"] >= 1.0


def _maximum(value: Optional[int]) -> float:
    return float("inf") if value is None else float(value)


def _reason(
    code: str,
    *,
    constraint: str,
    source: Any,
    target: Any,
    field: Optional[str] = None,
) -> Dict[str, Any]:
    result = {
        "code": code,
        "constraint": constraint,
        "source": source,
        "target": target,
    }
    if field is not None:
        result["field"] = field
    return result


def _directional_reasons(
    source: Mapping[str, Any], target: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    """Explain why the acceptance set of source is not a subset of target."""

    reasons: List[Dict[str, Any]] = []
    if source["allow_extra"] and not target["allow_extra"]:
        reasons.append(
            _reason(
                "extra_fields_newly_forbidden",
                constraint="allow_extra",
                source=True,
                target=False,
            )
        )
    if target["min_rows"] > source["min_rows"]:
        reasons.append(
            _reason(
                "minimum_rows_increased",
                constraint="min_rows",
                source=source["min_rows"],
                target=target["min_rows"],
            )
        )
    if _maximum(target.get("max_rows")) < _maximum(source.get("max_rows")):
        reasons.append(
            _reason(
                "maximum_rows_decreased",
                constraint="max_rows",
                source=source.get("max_rows"),
                target=target.get("max_rows"),
            )
        )
    if target["max_duplicate_rate"] < source["max_duplicate_rate"]:
        reasons.append(
            _reason(
                "maximum_duplicate_rate_decreased",
                constraint="max_duplicate_rate",
                source=source["max_duplicate_rate"],
                target=target["max_duplicate_rate"],
            )
        )

    source_fields = source["fields"]
    target_fields = target["fields"]
    for field in sorted(set(source_fields) | set(target_fields)):
        source_rule = source_fields.get(field)
        target_rule = target_fields.get(field)
        if source_rule is not None and target_rule is None:
            if not target["allow_extra"]:
                reasons.append(
                    _reason(
                        "source_field_forbidden_by_target",
                        field=field,
                        constraint="field_presence",
                        source="declared",
                        target="forbidden_extra",
                    )
                )
            continue
        if source_rule is None and target_rule is not None:
            if not _absence_allowed(target_rule):
                reasons.append(
                    _reason(
                        "target_requires_source_absent_field",
                        field=field,
                        constraint="field_presence",
                        source="not_declared",
                        target="required_or_missing_rate_below_1",
                    )
                )
            if source["allow_extra"]:
                reasons.append(
                    _reason(
                        "source_unconstrained_field_restricted_by_target",
                        field=field,
                        constraint="field_definition",
                        source="unconstrained_extra",
                        target="declared_constraints",
                    )
                )
            continue
        if source_rule is None or target_rule is None:  # pragma: no cover
            continue

        source_types = set(source_rule["types"])
        target_types = set(target_rule["types"])
        if not source_types.issubset(target_types):
            reasons.append(
                _reason(
                    "allowed_types_narrowed",
                    field=field,
                    constraint="types",
                    source=sorted(source_types),
                    target=sorted(target_types),
                )
            )
        if source_rule["nullable"] and not target_rule["nullable"]:
            reasons.append(
                _reason(
                    "nullability_removed",
                    field=field,
                    constraint="nullable",
                    source=True,
                    target=False,
                )
            )
        if target_rule["max_missing_rate"] < source_rule["max_missing_rate"]:
            reasons.append(
                _reason(
                    "maximum_missing_rate_decreased",
                    field=field,
                    constraint="max_missing_rate",
                    source=source_rule["max_missing_rate"],
                    target=target_rule["max_missing_rate"],
                )
            )
        if not source_rule["required"] and target_rule["required"]:
            reasons.append(
                _reason(
                    "field_became_required",
                    field=field,
                    constraint="required",
                    source=False,
                    target=True,
                )
            )
        if not source_rule["unique"] and target_rule["unique"]:
            reasons.append(
                _reason(
                    "uniqueness_added",
                    field=field,
                    constraint="unique",
                    source=False,
                    target=True,
                )
            )
    return reasons


def _constraint_change(
    *,
    scope: str,
    constraint: str,
    baseline: Any,
    candidate: Any,
    direction: str,
    field: Optional[str] = None,
) -> Dict[str, Any]:
    change = {
        "scope": scope,
        "constraint": constraint,
        "baseline": baseline,
        "candidate": candidate,
        "direction": direction,
    }
    if field is not None:
        change["field"] = field
    return change


def _changes(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> Dict[str, Any]:
    baseline_fields = baseline["fields"]
    candidate_fields = candidate["fields"]
    added = sorted(set(candidate_fields) - set(baseline_fields))
    removed = sorted(set(baseline_fields) - set(candidate_fields))
    common = sorted(set(baseline_fields) & set(candidate_fields))
    type_changes: List[Dict[str, Any]] = []
    required_changes: List[Dict[str, Any]] = []
    nullable_changes: List[Dict[str, Any]] = []
    tightened: List[Dict[str, Any]] = []
    relaxed: List[Dict[str, Any]] = []

    def add_constraint(
        scope: str,
        constraint: str,
        old: Any,
        new: Any,
        direction: str,
        field: Optional[str] = None,
    ) -> None:
        target = tightened if direction == "TIGHTENED" else relaxed
        target.append(
            _constraint_change(
                scope=scope,
                constraint=constraint,
                baseline=old,
                candidate=new,
                direction=direction,
                field=field,
            )
        )

    if baseline["allow_extra"] != candidate["allow_extra"]:
        add_constraint(
            "contract", "allow_extra", baseline["allow_extra"], candidate["allow_extra"],
            "RELAXED" if candidate["allow_extra"] else "TIGHTENED",
        )
    if baseline["min_rows"] != candidate["min_rows"]:
        add_constraint(
            "contract", "min_rows", baseline["min_rows"], candidate["min_rows"],
            "TIGHTENED" if candidate["min_rows"] > baseline["min_rows"] else "RELAXED",
        )
    if baseline.get("max_rows") != candidate.get("max_rows"):
        add_constraint(
            "contract", "max_rows", baseline.get("max_rows"), candidate.get("max_rows"),
            "TIGHTENED"
            if _maximum(candidate.get("max_rows")) < _maximum(baseline.get("max_rows"))
            else "RELAXED",
        )
    if baseline["max_duplicate_rate"] != candidate["max_duplicate_rate"]:
        add_constraint(
            "contract", "max_duplicate_rate", baseline["max_duplicate_rate"],
            candidate["max_duplicate_rate"],
            "TIGHTENED"
            if candidate["max_duplicate_rate"] < baseline["max_duplicate_rate"]
            else "RELAXED",
        )

    new_required_fields = []
    for field in added:
        if not _absence_allowed(candidate_fields[field]):
            new_required_fields.append(field)
            add_constraint(
                "field", "field_presence", "absent", "required_or_missing_rate_below_1",
                "TIGHTENED", field,
            )
    for field in removed:
        if not _absence_allowed(baseline_fields[field]):
            add_constraint(
                "field", "field_presence", "required_or_missing_rate_below_1", "absent",
                "RELAXED", field,
            )

    for field in common:
        old = baseline_fields[field]
        new = candidate_fields[field]
        old_types = set(old["types"])
        new_types = set(new["types"])
        if old_types != new_types:
            type_changes.append(
                {
                    "field": field,
                    "baseline_types": sorted(old_types),
                    "candidate_types": sorted(new_types),
                    "added_types": sorted(new_types - old_types),
                    "removed_types": sorted(old_types - new_types),
                }
            )
            if new_types - old_types:
                add_constraint(
                    "field", "types", sorted(old_types), sorted(new_types), "RELAXED", field
                )
            if old_types - new_types:
                add_constraint(
                    "field", "types", sorted(old_types), sorted(new_types), "TIGHTENED", field
                )
        if old["required"] != new["required"]:
            required_changes.append(
                {"field": field, "baseline": old["required"], "candidate": new["required"]}
            )
            add_constraint(
                "field", "required", old["required"], new["required"],
                "TIGHTENED" if new["required"] else "RELAXED", field,
            )
        if old["nullable"] != new["nullable"]:
            nullable_changes.append(
                {"field": field, "baseline": old["nullable"], "candidate": new["nullable"]}
            )
            add_constraint(
                "field", "nullable", old["nullable"], new["nullable"],
                "RELAXED" if new["nullable"] else "TIGHTENED", field,
            )
        if old["max_missing_rate"] != new["max_missing_rate"]:
            add_constraint(
                "field", "max_missing_rate", old["max_missing_rate"],
                new["max_missing_rate"],
                "TIGHTENED"
                if new["max_missing_rate"] < old["max_missing_rate"]
                else "RELAXED", field,
            )
        if old["unique"] != new["unique"]:
            add_constraint(
                "field", "unique", old["unique"], new["unique"],
                "TIGHTENED" if new["unique"] else "RELAXED", field,
            )

    return {
        "added_fields": added,
        "removed_fields": removed,
        "type_changes": type_changes,
        "required_changes": required_changes,
        "nullable_changes": nullable_changes,
        "new_required_fields": new_required_fields,
        "tightened_constraints": tightened,
        "relaxed_constraints": relaxed,
    }


def compare_contracts(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    same_dataset: bool,
) -> Dict[str, Any]:
    """Compare acceptance sets in both explicitly documented directions."""

    changes = _changes(baseline, candidate)
    if not same_dataset:
        return {
            "qualification": "INSUFFICIENT",
            "backward": {
                "compatible": None,
                "direction": "baseline_to_candidate",
                "reasons": [],
            },
            "forward": {
                "compatible": None,
                "direction": "candidate_to_baseline",
                "reasons": [],
            },
            "changes": changes,
            "insufficient_reasons": ["contracts_belong_to_different_datasets"],
            "rules_version": COMPATIBILITY_RULES_VERSION,
        }

    backward_reasons = _directional_reasons(baseline, candidate)
    forward_reasons = _directional_reasons(candidate, baseline)
    backward_compatible = not backward_reasons
    forward_compatible = not forward_reasons
    if backward_compatible and forward_compatible:
        qualification = "FULLY_COMPATIBLE"
    elif backward_compatible:
        qualification = "BACKWARD_COMPATIBLE"
    elif forward_compatible:
        qualification = "FORWARD_COMPATIBLE"
    else:
        qualification = "BREAKING"
    return {
        "qualification": qualification,
        "backward": {
            "compatible": backward_compatible,
            "direction": "baseline_to_candidate",
            "reasons": backward_reasons,
        },
        "forward": {
            "compatible": forward_compatible,
            "direction": "candidate_to_baseline",
            "reasons": forward_reasons,
        },
        "changes": changes,
        "insufficient_reasons": [],
        "rules_version": COMPATIBILITY_RULES_VERSION,
    }
