"""Deterministic reconstruction of persisted provenance closures."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from .hashing import hash_json, sha256_text
from .lineage import LINEAGE_RULES_VERSION

PROVENANCE_CLOSURE_RULES_VERSION = "1.0.0"


def _issue(code: str, resource_type: str, resource_id: str, **details: Any) -> Dict[str, Any]:
    return {
        "code": code,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": details,
    }


def _cycles(graph: Mapping[str, Sequence[Tuple[str, str]]], nodes: Set[str]) -> List[List[str]]:
    color: Dict[str, int] = {}
    stack: List[str] = []
    positions: Dict[str, int] = {}
    found: Set[Tuple[str, ...]] = set()

    def canonical(path: List[str]) -> Tuple[str, ...]:
        base = path[:-1]
        rotations = [tuple(base[index:] + base[:index]) for index in range(len(base))]
        chosen = min(rotations)
        return chosen + (chosen[0],)

    def visit(node: str) -> None:
        color[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for upstream, _ in graph.get(node, []):
            if upstream not in nodes:
                continue
            if color.get(upstream, 0) == 0:
                visit(upstream)
            elif color.get(upstream) == 1:
                found.add(canonical(stack[positions[upstream] :] + [upstream]))
        stack.pop()
        positions.pop(node)
        color[node] = 2

    for node in sorted(nodes):
        if color.get(node, 0) == 0:
            visit(node)
    return [list(path) for path in sorted(found)]


def build_provenance_closure(
    *,
    requested_version_ids: Sequence[str],
    versions: Mapping[str, Mapping[str, Any]],
    datasets: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Mapping[str, Any]],
    lineage_links: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Reconstruct and verify a closure without synthesizing missing evidence."""

    requested = sorted(requested_version_ids)
    upstream_graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    lineage_by_downstream: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for link in lineage_links:
        lineage_by_downstream[link["downstream_version_id"]].append(link)
        upstream_graph[link["downstream_version_id"]].append(
            (link["upstream_version_id"], f"lineage:{link['relation_type']}:{link['id']}")
        )
    for version_id, version in versions.items():
        previous = version.get("previous_version_id")
        if previous:
            upstream_graph[version_id].append((previous, "previous_version"))
    for edges in upstream_graph.values():
        edges.sort()

    closure: Set[str] = set()
    missing_references: List[Dict[str, Any]] = []
    queue = deque(requested)
    while queue:
        version_id = queue.popleft()
        if version_id in closure:
            continue
        if version_id not in versions:
            missing_references.append(
                _issue("requested_version_missing", "dataset_version", version_id)
            )
            continue
        closure.add(version_id)
        version = versions[version_id]
        for ref_type, ref_id in (
            ("dataset", version["dataset_id"]),
            ("source", version["source_id"]),
        ):
            collection = datasets if ref_type == "dataset" else sources
            if ref_id not in collection:
                missing_references.append(
                    _issue(
                        f"{ref_type}_reference_missing",
                        "dataset_version",
                        version_id,
                        reference_id=ref_id,
                    )
                )
        for upstream, edge_type in upstream_graph.get(version_id, []):
            if upstream not in versions:
                missing_references.append(
                    _issue(
                        "upstream_version_missing",
                        "dataset_version",
                        version_id,
                        reference_id=upstream,
                        edge_type=edge_type,
                    )
                )
            else:
                queue.append(upstream)

    cycles = _cycles(upstream_graph, closure)
    breaks: List[Dict[str, Any]] = []
    integrity: List[Dict[str, Any]] = []

    involved_dataset_ids = sorted(
        {versions[version_id]["dataset_id"] for version_id in closure if version_id in versions}
    )
    involved_source_ids = sorted(
        {versions[version_id]["source_id"] for version_id in closure if version_id in versions}
    )

    for source_id in involved_source_ids:
        source = sources.get(source_id)
        if source is None:
            continue
        try:
            metadata = json.loads(source["metadata_json"])
            descriptor = {
                "name": source["name"],
                "kind": source["kind"],
                "uri": source["uri"],
                "description": source["description"],
                "metadata": metadata,
            }
            issues = [] if hash_json(descriptor) == source["source_hash"] else ["source_hash_mismatch"]
        except (json.JSONDecodeError, TypeError):
            issues = ["invalid_stored_json"]
        valid = not issues
        integrity.append({"resource_type": "source", "resource_id": source_id, "valid": valid, "issues": issues})
        for code in issues:
            breaks.append(_issue(code, "source", source_id))

    for dataset_id in involved_dataset_ids:
        dataset = datasets.get(dataset_id)
        if dataset is None:
            continue
        try:
            schema = json.loads(dataset["schema_json"]) if dataset["schema_json"] else None
            descriptor = {
                "name": dataset["name"],
                "description": dataset["description"],
                "schema": schema,
            }
            issues = [] if hash_json(descriptor) == dataset["dataset_hash"] else ["dataset_hash_mismatch"]
        except (json.JSONDecodeError, TypeError):
            issues = ["invalid_stored_json"]
        valid = not issues
        integrity.append({"resource_type": "dataset", "resource_id": dataset_id, "valid": valid, "issues": issues})
        for code in issues:
            breaks.append(_issue(code, "dataset", dataset_id))

    for version_id in sorted(closure):
        version = versions[version_id]
        issues: List[str] = []
        try:
            manifest = json.loads(version["manifest_json"])
            records = json.loads(version["records_json"])
        except (json.JSONDecodeError, TypeError):
            manifest = {}
            records = []
            issues.append("invalid_stored_json")
        if sha256_text(version["records_json"]) != version["content_hash"]:
            issues.append("content_hash_mismatch")
        if sha256_text(version["manifest_json"]) != version["provenance_hash"]:
            issues.append("provenance_hash_mismatch")
        if len(records) != version["record_count"]:
            issues.append("record_count_mismatch")
        expected = {
            "version_id": version_id,
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
        if dataset is not None and manifest.get("dataset_hash") != dataset["dataset_hash"]:
            issues.append("manifest_dataset_hash_mismatch")
        if source is not None and manifest.get("source_hash") != source["source_hash"]:
            issues.append("manifest_source_hash_mismatch")
        previous_id = version["previous_version_id"]
        if version["version_number"] > 1 and not previous_id:
            issues.append("previous_version_chain_missing")
        if previous_id and previous_id in versions:
            previous = versions[previous_id]
            if previous["dataset_id"] != version["dataset_id"]:
                issues.append("previous_version_dataset_mismatch")
            if previous["version_number"] != version["version_number"] - 1:
                issues.append("previous_version_number_break")
            if manifest.get("previous_provenance_hash") != previous["provenance_hash"]:
                issues.append("previous_provenance_hash_mismatch")
        integrity.append(
            {
                "resource_type": "dataset_version",
                "resource_id": version_id,
                "valid": not issues,
                "issues": sorted(set(issues)),
            }
        )
        for code in sorted(set(issues)):
            breaks.append(_issue(code, "dataset_version", version_id))

    involved_links = sorted(
        (
            link
            for link in lineage_links
            if link["downstream_version_id"] in closure
        ),
        key=lambda link: link["id"],
    )
    for link in involved_links:
        issues: List[str] = []
        upstream = versions.get(link["upstream_version_id"])
        downstream = versions.get(link["downstream_version_id"])
        if upstream is None or downstream is None:
            issues.append("lineage_endpoint_missing")
        else:
            expected_hash = hash_json(
                {
                    "format": "dataforge.lineage-link/1.0",
                    "upstream_version_id": upstream["id"],
                    "upstream_content_hash": upstream["content_hash"],
                    "downstream_version_id": downstream["id"],
                    "downstream_content_hash": downstream["content_hash"],
                    "relation_type": link["relation_type"],
                    "rules_version": LINEAGE_RULES_VERSION,
                }
            )
            if expected_hash != link["link_hash"]:
                issues.append("lineage_link_hash_mismatch")
        integrity.append(
            {
                "resource_type": "lineage_link",
                "resource_id": link["id"],
                "valid": not issues,
                "issues": issues,
            }
        )
        for code in issues:
            breaks.append(_issue(code, "lineage_link", link["id"]))

    chains: List[Dict[str, Any]] = []
    for target in requested:
        if target not in versions:
            continue
        path_queue = deque([(target, [target], [])])
        while path_queue:
            current, version_path, edge_path = path_queue.popleft()
            current_version = versions[current]
            source_id = current_version["source_id"]
            chains.append(
                {
                    "target_version_id": target,
                    "version_path": version_path,
                    "edge_path": edge_path,
                    "source_id": source_id if source_id in sources else None,
                    "complete": source_id in sources,
                }
            )
            for upstream, edge_type in upstream_graph.get(current, []):
                if upstream not in versions or upstream in version_path:
                    continue
                path_queue.append((upstream, version_path + [upstream], edge_path + [edge_type]))
    chains.sort(key=lambda chain: (chain["target_version_id"], chain["version_path"], chain["edge_path"]))

    all_versions_in_datasets = {
        version_id
        for version_id, version in versions.items()
        if version["dataset_id"] in involved_dataset_ids
    }
    unused_versions = sorted(all_versions_in_datasets - closure)
    closure_adjacency = {
        node
        for version_id in closure
        for node, _ in upstream_graph.get(version_id, [])
    }
    orphan_versions = sorted(
        version_id
        for version_id in unused_versions
        if version_id not in closure_adjacency
        and not any(
            upstream == version_id
            for edges in upstream_graph.values()
            for upstream, _ in edges
            if upstream in closure
        )
    )

    undirected: Dict[str, Set[str]] = defaultdict(set)
    for downstream, edges in upstream_graph.items():
        if downstream not in closure:
            continue
        for upstream, _ in edges:
            if upstream in closure:
                undirected[downstream].add(upstream)
                undirected[upstream].add(downstream)
    component_count = 0
    unseen = set(requested) & closure
    while unseen:
        component_count += 1
        start = min(unseen)
        reachable = {start}
        component_queue = deque([start])
        while component_queue:
            node = component_queue.popleft()
            for neighbor in undirected.get(node, set()):
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    component_queue.append(neighbor)
        unseen -= reachable

    target_has_ancestry = bool(upstream_graph.get(requested[0])) if len(requested) == 1 else True
    if missing_references or breaks or cycles:
        qualification = "BROKEN"
    elif len(requested) > 1 and component_count > 1:
        qualification = "INCOMPATIBLE"
    elif len(requested) == 1 and not target_has_ancestry:
        qualification = "INSUFFICIENT"
    elif unused_versions or orphan_versions:
        qualification = "PARTIAL"
    else:
        qualification = "COMPLETE"

    snapshot = {
        "requested_version_ids": requested,
        "versions": [
            {
                "id": version_id,
                "content_hash": versions[version_id]["content_hash"],
                "provenance_hash": versions[version_id]["provenance_hash"],
            }
            for version_id in sorted(closure)
        ],
        "datasets": [
            {"id": dataset_id, "dataset_hash": datasets[dataset_id]["dataset_hash"]}
            for dataset_id in involved_dataset_ids
            if dataset_id in datasets
        ],
        "sources": [
            {"id": source_id, "source_hash": sources[source_id]["source_hash"]}
            for source_id in involved_source_ids
            if source_id in sources
        ],
        "lineage_links": [
            {"id": link["id"], "link_hash": link["link_hash"]} for link in involved_links
        ],
    }
    return {
        "qualification": qualification,
        "requested_version_ids": requested,
        "closure_version_ids": sorted(closure),
        "dataset_ids": involved_dataset_ids,
        "source_ids": involved_source_ids,
        "lineage_link_ids": [link["id"] for link in involved_links],
        "chains": chains,
        "missing_references": sorted(missing_references, key=lambda item: (item["code"], item["resource_id"])),
        "orphan_versions": orphan_versions,
        "cycles": cycles,
        "breaks": sorted(breaks, key=lambda item: (item["resource_type"], item["resource_id"], item["code"])),
        "unused_versions": unused_versions,
        "integrity": sorted(integrity, key=lambda item: (item["resource_type"], item["resource_id"])),
        "summary": {
            "requested_version_count": len(requested),
            "closure_version_count": len(closure),
            "dataset_count": len(involved_dataset_ids),
            "source_count": len(involved_source_ids),
            "lineage_link_count": len(involved_links),
            "connected_component_count": component_count,
        },
        "snapshot_hash": hash_json(snapshot),
        "rules_version": PROVENANCE_CLOSURE_RULES_VERSION,
    }
