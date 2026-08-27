"""Chronological attribution of downstream lineage evolution."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from .hashing import canonical_json, hash_json, sha256_text
from .lineage import LINEAGE_RULES_VERSION

LINEAGE_EVOLUTION_RULES_VERSION = "1.0.0"


def _issue(code: str, resource_type: str, resource_id: str, **details: Any) -> Dict[str, Any]:
    return {"code": code, "resource_type": resource_type, "resource_id": resource_id, "details": details}


def _cycles(graph: Mapping[str, Sequence[Tuple[str, str]]], nodes: Set[str]) -> List[List[str]]:
    color: Dict[str, int] = {}
    stack: List[str] = []
    positions: Dict[str, int] = {}
    found: Set[Tuple[str, ...]] = set()

    def canonical(path: List[str]) -> Tuple[str, ...]:
        body = path[:-1]
        rotations = [tuple(body[index:] + body[:index]) for index in range(len(body))]
        chosen = min(rotations)
        return chosen + (chosen[0],)

    def visit(node: str) -> None:
        color[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for downstream, _ in graph.get(node, []):
            if downstream not in nodes:
                continue
            if color.get(downstream, 0) == 0:
                visit(downstream)
            elif color.get(downstream) == 1:
                found.add(canonical(stack[positions[downstream] :] + [downstream]))
        stack.pop()
        positions.pop(node)
        color[node] = 2

    for node in sorted(nodes):
        if color.get(node, 0) == 0:
            visit(node)
    return [list(item) for item in sorted(found)]


def _verify_version(version: Mapping[str, Any], datasets: Mapping[str, Mapping[str, Any]], sources: Mapping[str, Mapping[str, Any]], versions: Mapping[str, Mapping[str, Any]] | None = None) -> List[str]:
    issues: List[str] = []
    try:
        records = json.loads(version["records_json"])
        manifest = json.loads(version["manifest_json"])
    except (json.JSONDecodeError, TypeError):
        records, manifest = [], {}
        issues.append("invalid_stored_json")
    if sha256_text(version["records_json"]) != version["content_hash"]:
        issues.append("content_hash_mismatch")
    if sha256_text(version["manifest_json"]) != version["provenance_hash"]:
        issues.append("provenance_hash_mismatch")
    if len(records) != version["record_count"]:
        issues.append("record_count_mismatch")
    expected = {
        "version_id": version["id"],
        "dataset_id": version["dataset_id"],
        "source_id": version["source_id"],
        "version_number": version["version_number"],
        "record_count": version["record_count"],
        "content_hash": version["content_hash"],
        "previous_version_id": version["previous_version_id"],
        "created_at": version["created_at"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            issues.append(f"manifest_{key}_mismatch")
    dataset = datasets.get(version["dataset_id"])
    source = sources.get(version["source_id"])
    if dataset is None:
        issues.append("dataset_reference_missing")
    elif manifest.get("dataset_hash") != dataset["dataset_hash"]:
        issues.append("manifest_dataset_hash_mismatch")
    if source is None:
        issues.append("source_reference_missing")
    elif manifest.get("source_hash") != source["source_hash"]:
        issues.append("manifest_source_hash_mismatch")
    previous = version.get("previous_version_id")
    if previous and versions is not None:
        previous_version = versions.get(previous)
        if previous_version is None:
            issues.append("previous_version_reference_missing")
        else:
            if previous_version["dataset_id"] != version["dataset_id"]:
                issues.append("previous_version_dataset_mismatch")
            if previous_version["version_number"] != version["version_number"] - 1:
                issues.append("previous_version_number_break")
            if manifest.get("previous_provenance_hash") != previous_version["provenance_hash"]:
                issues.append("previous_provenance_hash_mismatch")
    return sorted(set(issues))


def build_lineage_evolution(
    *,
    selected_version_ids: Sequence[str],
    versions: Mapping[str, Mapping[str, Any]],
    datasets: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Mapping[str, Any]],
    lineage_links: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    selected = [versions[item] for item in selected_version_ids]
    chronological = sorted(selected, key=lambda item: (item["version_number"], item["created_at"], item["id"]))
    chronological_ids = [item["id"] for item in chronological]
    dataset_ids = {item["dataset_id"] for item in chronological}
    compatibility_issues = [] if len(dataset_ids) == 1 else ["selected_versions_belong_to_different_datasets"]

    links_by_upstream: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for link in lineage_links:
        links_by_upstream[link["upstream_version_id"]].append(link)
        graph[link["upstream_version_id"]].append((link["downstream_version_id"], link["id"]))
    for value in links_by_upstream.values():
        value.sort(key=lambda item: (item["downstream_version_id"], item["relation_type"], item["id"]))
    for value in graph.values():
        value.sort()

    states = []
    root_proof_issues = []
    for root in chronological:
        root_issues = _verify_version(root, datasets, sources, versions)
        root_proof_issues.extend(f"{root['id']}:{code}" for code in root_issues)
        reached = {root["id"]}
        depths = {root["id"]: 0}
        paths = {root["id"]: [root["id"]]}
        link_paths = {root["id"]: []}
        queue = deque([root["id"]])
        orphans = []
        breaks = []
        used_links: Dict[str, Mapping[str, Any]] = {}
        while queue:
            current = queue.popleft()
            for link in links_by_upstream.get(current, []):
                used_links[link["id"]] = link
                downstream_id = link["downstream_version_id"]
                if downstream_id not in versions:
                    orphans.append(_issue("downstream_version_missing", "lineage_link", link["id"], reference_id=downstream_id))
                    continue
                candidate = paths[current] + [downstream_id]
                candidate_depth = depths[current] + 1
                if downstream_id not in depths or candidate_depth < depths[downstream_id] or (
                    candidate_depth == depths[downstream_id] and candidate < paths[downstream_id]
                ):
                    reached.add(downstream_id)
                    depths[downstream_id] = candidate_depth
                    paths[downstream_id] = candidate
                    link_paths[downstream_id] = link_paths[current] + [link["id"]]
                    queue.append(downstream_id)

        for version_id in sorted(reached):
            for code in _verify_version(versions[version_id], datasets, sources, versions):
                breaks.append(_issue(code, "dataset_version", version_id))
        for link in sorted(used_links.values(), key=lambda item: item["id"]):
            upstream = versions.get(link["upstream_version_id"])
            downstream = versions.get(link["downstream_version_id"])
            if upstream is None or downstream is None:
                breaks.append(_issue("lineage_endpoint_missing", "lineage_link", link["id"]))
            else:
                expected = hash_json({"format": "dataforge.lineage-link/1.0", "upstream_version_id": upstream["id"], "upstream_content_hash": upstream["content_hash"], "downstream_version_id": downstream["id"], "downstream_content_hash": downstream["content_hash"], "relation_type": link["relation_type"], "rules_version": LINEAGE_RULES_VERSION})
                if expected != link["link_hash"]:
                    breaks.append(_issue("lineage_link_hash_mismatch", "lineage_link", link["id"]))

        downstream_ids = sorted(reached - {root["id"]})
        affected = [{"version_id": item, "dataset_id": versions[item]["dataset_id"], "depth": depths[item], "version_path": paths[item], "lineage_link_path": link_paths[item]} for item in downstream_ids]
        cycles = _cycles(graph, reached)
        states.append({
            "version_id": root["id"],
            "version_number": root["version_number"],
            "created_at": root["created_at"],
            "content_hash": root["content_hash"],
            "provenance_hash": root["provenance_hash"],
            "proof_valid": not root_issues,
            "downstream_version_ids": downstream_ids,
            "downstream_dataset_ids": sorted({versions[item]["dataset_id"] for item in downstream_ids}),
            "affected": affected,
            "orphan_references": sorted(orphans, key=canonical_json),
            "breaks": sorted(breaks, key=canonical_json),
            "cycles": cycles,
            "maximum_depth": max((item["depth"] for item in affected), default=0),
        })

    transitions = []
    for previous, current in zip(states, states[1:]):
        previous_set = set(previous["downstream_version_ids"])
        current_set = set(current["downstream_version_ids"])
        additions = sorted(current_set - previous_set)
        removals = sorted(previous_set - current_set)
        previous_breaks = {canonical_json(item): item for item in previous["breaks"]}
        current_breaks = {canonical_json(item): item for item in current["breaks"]}
        previous_orphans = {canonical_json(item): item for item in previous["orphan_references"]}
        current_orphans = {canonical_json(item): item for item in current["orphan_references"]}
        current_paths = {item["version_id"]: item for item in current["affected"]}
        previous_paths = {item["version_id"]: item for item in previous["affected"]}
        touched = ([{"action": "ADDED", **current_paths[item]} for item in additions] + [{"action": "REMOVED", **previous_paths[item]} for item in removals])
        new_breaks = [current_breaks[key] for key in sorted(set(current_breaks) - set(previous_breaks))]
        resolved_breaks = [previous_breaks[key] for key in sorted(set(previous_breaks) - set(current_breaks))]
        new_orphans = [current_orphans[key] for key in sorted(set(current_orphans) - set(previous_orphans))]
        resolved_orphans = [previous_orphans[key] for key in sorted(set(previous_orphans) - set(current_orphans))]
        severity = len(new_breaks) * 100 + len(new_orphans) * 50 + len(removals) * 10 + len(additions)
        transitions.append({"from_version_id": previous["version_id"], "to_version_id": current["version_id"], "added_dependency_version_ids": additions, "removed_dependency_version_ids": removals, "new_breaks": new_breaks, "resolved_breaks": resolved_breaks, "new_orphan_references": new_orphans, "resolved_orphan_references": resolved_orphans, "touched_branches": touched, "severity_score": severity})

    worst_transition = max(enumerate(transitions), key=lambda pair: (pair[1]["severity_score"], pair[0]))[1]
    any_downstream = any(state["downstream_version_ids"] for state in states)
    partial_signals = any(state["orphan_references"] or state["breaks"] or state["cycles"] for state in states)
    if compatibility_issues:
        qualification = "INCOMPATIBLE"
    elif root_proof_issues:
        qualification = "INSUFFICIENT"
    elif not any_downstream:
        qualification = "INSUFFICIENT"
        root_proof_issues.append("no_downstream_lineage_evidence")
    elif partial_signals:
        qualification = "PARTIAL"
    else:
        qualification = "EXPLAINED"

    snapshot_hash = hash_json({"chronological_version_ids": chronological_ids, "states": states})
    return {"selected_version_ids": sorted(selected_version_ids), "chronological_version_ids": chronological_ids, "dataset_id": next(iter(dataset_ids)) if len(dataset_ids) == 1 else None, "qualification": qualification, "states": states, "transitions": transitions, "worst_transition": worst_transition, "compatibility_issues": compatibility_issues, "insufficient_reasons": sorted(root_proof_issues), "summary": {"state_count": len(states), "transition_count": len(transitions), "added_dependency_count": sum(len(item["added_dependency_version_ids"]) for item in transitions), "removed_dependency_count": sum(len(item["removed_dependency_version_ids"]) for item in transitions), "new_break_count": sum(len(item["new_breaks"]) for item in transitions), "new_orphan_count": sum(len(item["new_orphan_references"]) for item in transitions)}, "snapshot_hash": snapshot_hash, "rules_version": LINEAGE_EVOLUTION_RULES_VERSION}
