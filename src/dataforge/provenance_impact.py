"""Deterministic downstream provenance-impact dossiers."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from .hashing import hash_json, sha256_text
from .lineage import LINEAGE_RULES_VERSION

PROVENANCE_IMPACT_RULES_VERSION = "1.0.0"
WIDESPREAD_VERSION_THRESHOLD = 5
WIDESPREAD_DATASET_THRESHOLD = 3
WIDESPREAD_DEPTH_THRESHOLD = 4


def _issue(code: str, resource_type: str, resource_id: str, **details: Any) -> Dict[str, Any]:
    return {"code": code, "resource_type": resource_type, "resource_id": resource_id, "details": details}


def _find_cycles(graph: Mapping[str, Sequence[Tuple[str, str, str | None]]], nodes: Set[str]) -> List[List[str]]:
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
        for downstream, _, _ in graph.get(node, []):
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
    return [list(cycle) for cycle in sorted(found)]


def build_provenance_impact(
    *,
    selected_version_ids: Sequence[str],
    selected_dataset_ids: Sequence[str],
    versions: Mapping[str, Mapping[str, Any]],
    datasets: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Mapping[str, Any]],
    lineage_links: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Verify persisted evidence and traverse deterministic downstream references."""

    selected_versions = sorted(selected_version_ids)
    selected_datasets = sorted(selected_dataset_ids)
    dataset_seed_versions = sorted(
        version_id
        for version_id, version in versions.items()
        if version["dataset_id"] in set(selected_datasets)
    )
    seed_ids = sorted(set(selected_versions) | set(dataset_seed_versions))
    insufficient_reasons = [
        f"dataset_without_versions:{dataset_id}"
        for dataset_id in selected_datasets
        if not any(version["dataset_id"] == dataset_id for version in versions.values())
    ]

    graph: Dict[str, List[Tuple[str, str, str | None]]] = defaultdict(list)
    for version_id, version in versions.items():
        previous = version.get("previous_version_id")
        if previous:
            graph[previous].append((version_id, "NEXT_VERSION", None))
    for link in lineage_links:
        graph[link["upstream_version_id"]].append(
            (link["downstream_version_id"], f"LINEAGE:{link['relation_type']}", link["id"])
        )
    for edges in graph.values():
        edges.sort()

    depths = {version_id: 0 for version_id in seed_ids}
    paths = {version_id: [version_id] for version_id in seed_ids}
    edge_paths: Dict[str, List[str]] = {version_id: [] for version_id in seed_ids}
    link_paths: Dict[str, List[str]] = {version_id: [] for version_id in seed_ids}
    queue = deque(seed_ids)
    orphan_references: List[Dict[str, Any]] = []
    while queue:
        current = queue.popleft()
        for downstream, edge_type, link_id in graph.get(current, []):
            if downstream not in versions:
                orphan_references.append(
                    _issue("downstream_version_missing", "dataset_version", current, reference_id=downstream, edge_type=edge_type)
                )
                continue
            candidate_depth = depths[current] + 1
            candidate_path = paths[current] + [downstream]
            if downstream not in depths or candidate_depth < depths[downstream] or (
                candidate_depth == depths[downstream] and candidate_path < paths[downstream]
            ):
                depths[downstream] = candidate_depth
                paths[downstream] = candidate_path
                edge_paths[downstream] = edge_paths[current] + [edge_type]
                link_paths[downstream] = link_paths[current] + ([link_id] if link_id else [])
                queue.append(downstream)

    reached = set(depths)
    affected_ids = sorted(reached - set(seed_ids))
    involved_dataset_ids = sorted({versions[item]["dataset_id"] for item in reached})
    involved_source_ids = sorted({versions[item]["source_id"] for item in reached})
    integrity: List[Dict[str, Any]] = []
    breaks: List[Dict[str, Any]] = []

    for dataset_id in involved_dataset_ids:
        dataset = datasets.get(dataset_id)
        if dataset is None:
            orphan_references.append(_issue("dataset_missing", "dataset", dataset_id))
            continue
        try:
            schema = json.loads(dataset["schema_json"]) if dataset["schema_json"] else None
            valid = hash_json({"name": dataset["name"], "description": dataset["description"], "schema": schema}) == dataset["dataset_hash"]
            issues = [] if valid else ["dataset_hash_mismatch"]
        except (json.JSONDecodeError, TypeError):
            issues = ["invalid_stored_json"]
        integrity.append({"resource_type": "dataset", "resource_id": dataset_id, "valid": not issues, "issues": issues})
        breaks.extend(_issue(code, "dataset", dataset_id) for code in issues)

    for source_id in involved_source_ids:
        source = sources.get(source_id)
        if source is None:
            orphan_references.append(_issue("source_missing", "source", source_id))
            continue
        try:
            metadata = json.loads(source["metadata_json"])
            valid = hash_json({"name": source["name"], "kind": source["kind"], "uri": source["uri"], "description": source["description"], "metadata": metadata}) == source["source_hash"]
            issues = [] if valid else ["source_hash_mismatch"]
        except (json.JSONDecodeError, TypeError):
            issues = ["invalid_stored_json"]
        integrity.append({"resource_type": "source", "resource_id": source_id, "valid": not issues, "issues": issues})
        breaks.extend(_issue(code, "source", source_id) for code in issues)

    for version_id in sorted(reached):
        version = versions[version_id]
        issues: List[str] = []
        try:
            manifest = json.loads(version["manifest_json"])
            records = json.loads(version["records_json"])
        except (json.JSONDecodeError, TypeError):
            manifest, records = {}, []
            issues.append("invalid_stored_json")
        if sha256_text(version["records_json"]) != version["content_hash"]:
            issues.append("content_hash_mismatch")
        if sha256_text(version["manifest_json"]) != version["provenance_hash"]:
            issues.append("provenance_hash_mismatch")
        if len(records) != version["record_count"]:
            issues.append("record_count_mismatch")
        expected_manifest = {
            "version_id": version_id,
            "dataset_id": version["dataset_id"],
            "source_id": version["source_id"],
            "version_number": version["version_number"],
            "record_count": version["record_count"],
            "content_hash": version["content_hash"],
            "previous_version_id": version["previous_version_id"],
            "created_at": version["created_at"],
        }
        for key, expected_value in expected_manifest.items():
            if manifest.get(key) != expected_value:
                issues.append(f"manifest_{key}_mismatch")
        dataset = datasets.get(version["dataset_id"])
        source = sources.get(version["source_id"])
        if dataset is not None and manifest.get("dataset_hash") != dataset["dataset_hash"]:
            issues.append("manifest_dataset_hash_mismatch")
        if source is not None and manifest.get("source_hash") != source["source_hash"]:
            issues.append("manifest_source_hash_mismatch")
        if version["dataset_id"] not in datasets:
            orphan_references.append(_issue("dataset_reference_missing", "dataset_version", version_id, reference_id=version["dataset_id"]))
        if version["source_id"] not in sources:
            orphan_references.append(_issue("source_reference_missing", "dataset_version", version_id, reference_id=version["source_id"]))
        previous = version.get("previous_version_id")
        if previous and previous not in versions:
            orphan_references.append(_issue("previous_version_missing", "dataset_version", version_id, reference_id=previous))
        elif previous:
            previous_version = versions[previous]
            if previous_version["dataset_id"] != version["dataset_id"]:
                issues.append("previous_version_dataset_mismatch")
            if previous_version["version_number"] != version["version_number"] - 1:
                issues.append("previous_version_number_break")
            if manifest.get("previous_provenance_hash") != previous_version["provenance_hash"]:
                issues.append("previous_provenance_hash_mismatch")
        integrity.append({"resource_type": "dataset_version", "resource_id": version_id, "valid": not issues, "issues": sorted(set(issues))})
        breaks.extend(_issue(code, "dataset_version", version_id) for code in sorted(set(issues)))

    involved_links = sorted(
        [link for link in lineage_links if link["upstream_version_id"] in reached],
        key=lambda link: link["id"],
    )
    for link in involved_links:
        issues = []
        upstream = versions.get(link["upstream_version_id"])
        downstream = versions.get(link["downstream_version_id"])
        if upstream is None or downstream is None:
            issues.append("lineage_endpoint_missing")
        else:
            expected = hash_json({"format": "dataforge.lineage-link/1.0", "upstream_version_id": upstream["id"], "upstream_content_hash": upstream["content_hash"], "downstream_version_id": downstream["id"], "downstream_content_hash": downstream["content_hash"], "relation_type": link["relation_type"], "rules_version": LINEAGE_RULES_VERSION})
            if expected != link["link_hash"]:
                issues.append("lineage_link_hash_mismatch")
        integrity.append({"resource_type": "lineage_link", "resource_id": link["id"], "valid": not issues, "issues": issues})
        breaks.extend(_issue(code, "lineage_link", link["id"]) for code in issues)

    cycles = _find_cycles(graph, reached)
    affected = [
        {"version_id": item, "dataset_id": versions[item]["dataset_id"], "depth": depths[item], "version_path": paths[item], "edge_path": edge_paths[item], "lineage_link_path": link_paths[item]}
        for item in affected_ids
    ]
    branch_candidates = sorted(affected, key=lambda item: (-item["depth"], item["version_path"]))
    worst_branch = branch_candidates[0] if branch_candidates else None
    max_depth = max((item["depth"] for item in affected), default=0)
    affected_dataset_count = len({item["dataset_id"] for item in affected})
    if orphan_references or breaks or cycles:
        qualification = "INCOMPATIBLE"
    elif insufficient_reasons or not affected:
        qualification = "INSUFFICIENT"
        if not affected:
            insufficient_reasons.append("no_downstream_impact_observed")
    elif (
        len(affected) >= WIDESPREAD_VERSION_THRESHOLD
        or affected_dataset_count >= WIDESPREAD_DATASET_THRESHOLD
        or max_depth >= WIDESPREAD_DEPTH_THRESHOLD
    ):
        qualification = "WIDESPREAD"
    else:
        qualification = "CONTAINED"

    snapshot = {
        "selected_version_ids": selected_versions,
        "selected_dataset_ids": selected_datasets,
        "seed_version_ids": seed_ids,
        "versions": [{"id": item, "content_hash": versions[item]["content_hash"], "provenance_hash": versions[item]["provenance_hash"]} for item in sorted(reached)],
        "datasets": [{"id": item, "dataset_hash": datasets[item]["dataset_hash"]} for item in involved_dataset_ids if item in datasets],
        "sources": [{"id": item, "source_hash": sources[item]["source_hash"]} for item in involved_source_ids if item in sources],
        "lineage_links": [{"id": link["id"], "link_hash": link["link_hash"]} for link in involved_links],
        "integrity": sorted(integrity, key=lambda item: (item["resource_type"], item["resource_id"])),
    }
    return {
        "selected_version_ids": selected_versions,
        "selected_dataset_ids": selected_datasets,
        "seed_version_ids": seed_ids,
        "qualification": qualification,
        "affected": affected,
        "orphan_references": sorted(orphan_references, key=lambda item: (item["code"], item["resource_id"])),
        "cycles": cycles,
        "breaks": sorted(breaks, key=lambda item: (item["resource_type"], item["resource_id"], item["code"])),
        "integrity": sorted(integrity, key=lambda item: (item["resource_type"], item["resource_id"])),
        "worst_branch": worst_branch,
        "insufficient_reasons": sorted(set(insufficient_reasons)),
        "summary": {"seed_version_count": len(seed_ids), "affected_version_count": len(affected), "affected_dataset_count": affected_dataset_count, "maximum_depth": max_depth, "widespread_version_threshold": WIDESPREAD_VERSION_THRESHOLD, "widespread_dataset_threshold": WIDESPREAD_DATASET_THRESHOLD, "widespread_depth_threshold": WIDESPREAD_DEPTH_THRESHOLD},
        "snapshot_hash": hash_json(snapshot),
        "rules_version": PROVENANCE_IMPACT_RULES_VERSION,
    }
