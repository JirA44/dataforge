"""Deterministic downstream impact analysis for immutable lineage links."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

LINEAGE_RULES_VERSION = "1.0.0"
PROPAGATED_VERSION_THRESHOLD = 3
RELATION_TYPES = (
    "DERIVED_FROM",
    "TRANSFORMED_FROM",
    "FILTERED_FROM",
    "AGGREGATED_FROM",
    "JOINED_FROM",
    "COPIED_FROM",
)


def _canonical_cycle(nodes: Sequence[str]) -> Tuple[str, ...]:
    base = list(nodes[:-1])
    rotations = [tuple(base[index:] + base[:index]) for index in range(len(base))]
    smallest = min(rotations)
    return smallest + (smallest[0],)


def analyze_downstream(
    *,
    changed_version_id: str,
    max_depth: int,
    links: Sequence[Mapping[str, Any]],
    versions: Mapping[str, Mapping[str, Any]],
    compliance: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Traverse downstream with one deterministic shortest path per affected version."""

    adjacency: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for link in links:
        adjacency[link["upstream_version_id"]].append(link)
    for edges in adjacency.values():
        edges.sort(
            key=lambda edge: (
                edge["downstream_version_id"], edge["relation_type"], edge["id"]
            )
        )

    best_depth: Dict[str, int] = {changed_version_id: 0}
    path_by_version: Dict[str, Dict[str, Any]] = {}
    queue = deque([(changed_version_id, [changed_version_id], [], [])])
    while queue:
        current, version_path, relation_path, link_path = queue.popleft()
        current_depth = len(version_path) - 1
        if current_depth >= max_depth:
            continue
        for edge in adjacency.get(current, []):
            downstream = edge["downstream_version_id"]
            next_depth = current_depth + 1
            if downstream in best_depth and best_depth[downstream] <= next_depth:
                continue
            best_depth[downstream] = next_depth
            next_version_path = version_path + [downstream]
            next_relation_path = relation_path + [edge["relation_type"]]
            next_link_path = link_path + [edge["id"]]
            path_by_version[downstream] = {
                "target_version_id": downstream,
                "depth": next_depth,
                "version_path": next_version_path,
                "relation_path": next_relation_path,
                "link_path": next_link_path,
            }
            queue.append(
                (downstream, next_version_path, next_relation_path, next_link_path)
            )

    affected_ids = sorted(
        (version_id for version_id in best_depth if version_id != changed_version_id),
        key=lambda version_id: (best_depth[version_id], version_id),
    )
    induced_nodes: Set[str] = set(best_depth)

    color: Dict[str, int] = {}
    stack: List[str] = []
    stack_index: Dict[str, int] = {}
    canonical_cycles: Set[Tuple[str, ...]] = set()

    def visit(node: str) -> None:
        color[node] = 1
        stack_index[node] = len(stack)
        stack.append(node)
        for edge in adjacency.get(node, []):
            downstream = edge["downstream_version_id"]
            if downstream not in induced_nodes:
                continue
            if color.get(downstream, 0) == 0:
                visit(downstream)
            elif color.get(downstream) == 1:
                start = stack_index[downstream]
                canonical_cycles.add(_canonical_cycle(stack[start:] + [downstream]))
        stack.pop()
        stack_index.pop(node)
        color[node] = 2

    for node in sorted(induced_nodes):
        if color.get(node, 0) == 0:
            visit(node)

    affected_versions: List[Dict[str, Any]] = []
    dataset_summary: Dict[str, Dict[str, Any]] = {}
    for version_id in affected_ids:
        version = versions[version_id]
        dataset_id = version["dataset_id"]
        affected_versions.append(
            {
                "version_id": version_id,
                "dataset_id": dataset_id,
                "version_number": version["version_number"],
                "content_hash": version["content_hash"],
                "depth": best_depth[version_id],
                "contract_compliance": dict(
                    compliance.get(version_id, {"status": "NOT_AVAILABLE"})
                ),
            }
        )
        summary = dataset_summary.setdefault(
            dataset_id,
            {"dataset_id": dataset_id, "affected_version_count": 0, "min_depth": best_depth[version_id]},
        )
        summary["affected_version_count"] += 1
        summary["min_depth"] = min(summary["min_depth"], best_depth[version_id])

    cycle_paths = [list(cycle) for cycle in sorted(canonical_cycles)]
    if cycle_paths:
        qualification = "CYCLE_DETECTED"
    elif not affected_ids:
        qualification = "ISOLATED"
    elif len(affected_ids) >= PROPAGATED_VERSION_THRESHOLD:
        qualification = "PROPAGATED"
    else:
        qualification = "CONTAINED"

    return {
        "qualification": qualification,
        "paths": [path_by_version[version_id] for version_id in affected_ids],
        "affected_versions": affected_versions,
        "affected_datasets": [dataset_summary[key] for key in sorted(dataset_summary)],
        "cycle_paths": cycle_paths,
        "summary": {
            "affected_version_count": len(affected_ids),
            "affected_dataset_count": len(dataset_summary),
            "cycle_count": len(cycle_paths),
            "max_depth_requested": max_depth,
            "max_depth_reached": max(best_depth.values()),
            "propagated_threshold": PROPAGATED_VERSION_THRESHOLD,
        },
        "rules_version": LINEAGE_RULES_VERSION,
    }
