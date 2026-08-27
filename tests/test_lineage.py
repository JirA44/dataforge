from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.errors import NotFoundError, ValidationError
from dataforge.models import ImpactReportCreate, LineageLinkCreate
from dataforge.store import DataForgeStore


class DataForgeLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "lineage.sqlite3")
        self.source = self.store.create_source(name="lineage-source", kind="test")
        self.counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def version(self, label: str):
        self.counter += 1
        dataset = self.store.create_dataset(name=f"{label}-{self.counter}")
        version = self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"id": self.counter, "label": label}],
        )
        return dataset, version

    def link(self, upstream, downstream, relation_type="DERIVED_FROM"):
        return self.store.create_lineage_link(
            upstream_version_id=upstream["id"],
            downstream_version_id=downstream["id"],
            relation_type=relation_type,
        )

    def test_link_is_hashed_idempotent_and_audited_once(self) -> None:
        _, upstream = self.version("upstream")
        _, downstream = self.version("downstream")
        first = self.store.create_lineage_link(
            upstream_version_id=upstream["id"],
            downstream_version_id=downstream["id"],
            relation_type="TRANSFORMED_FROM",
            actor="first",
        )
        second = self.store.create_lineage_link(
            upstream_version_id=upstream["id"],
            downstream_version_id=downstream["id"],
            relation_type="TRANSFORMED_FROM",
            actor="second",
        )
        self.assertEqual(first, second)
        self.assertEqual(64, len(first["link_hash"]))
        self.assertEqual([first], self.store.list_lineage_links(upstream_version_id=upstream["id"]))
        audit = self.store.list_audit(action="LINEAGE_LINK_CREATED")
        self.assertEqual(1, len(audit))
        self.assertEqual("first", audit[0]["actor"])

    def test_self_link_and_missing_versions_are_rejected(self) -> None:
        _, version = self.version("same")
        with self.assertRaises(ValidationError):
            self.link(version, version)
        with self.assertRaises(NotFoundError):
            self.store.create_lineage_link(
                upstream_version_id="missing",
                downstream_version_id=version["id"],
                relation_type="COPIED_FROM",
            )

    def test_isolated_report_has_no_affected_versions(self) -> None:
        _, changed = self.version("isolated")
        report = self.store.create_impact_report(
            changed_version_id=changed["id"], max_depth=10
        )
        self.assertEqual("ISOLATED", report["qualification"])
        self.assertEqual([], report["paths"])
        self.assertEqual(0, report["summary"]["affected_version_count"])
        self.assertEqual(64, len(report["evidence_hash"]))
        self.assertEqual(64, len(report["report_hash"]))

    def test_contained_report_respects_depth_and_returns_paths(self) -> None:
        _, root = self.version("root")
        _, child = self.version("child")
        _, grandchild = self.version("grandchild")
        self.link(root, child, "FILTERED_FROM")
        self.link(child, grandchild, "AGGREGATED_FROM")
        depth_one = self.store.create_impact_report(
            changed_version_id=root["id"], max_depth=1
        )
        self.assertEqual("CONTAINED", depth_one["qualification"])
        self.assertEqual([child["id"]], [item["version_id"] for item in depth_one["affected_versions"]])
        depth_two = self.store.create_impact_report(
            changed_version_id=root["id"], max_depth=2
        )
        self.assertEqual("CONTAINED", depth_two["qualification"])
        self.assertEqual(2, depth_two["summary"]["affected_version_count"])
        grandchild_path = next(
            path for path in depth_two["paths"] if path["target_version_id"] == grandchild["id"]
        )
        self.assertEqual([root["id"], child["id"], grandchild["id"]], grandchild_path["version_path"])
        self.assertEqual(2, grandchild_path["depth"])

    def test_three_distinct_downstream_versions_are_propagated(self) -> None:
        _, root = self.version("root")
        downstream = [self.version(f"child-{index}")[1] for index in range(3)]
        for version in downstream:
            self.link(root, version)
        report = self.store.create_impact_report(
            changed_version_id=root["id"], max_depth=1
        )
        self.assertEqual("PROPAGATED", report["qualification"])
        self.assertEqual(3, report["summary"]["propagated_threshold"])
        self.assertEqual(3, len(report["affected_versions"]))
        self.assertEqual(3, len(report["affected_datasets"]))

    def test_reachable_cycle_has_priority_over_propagation(self) -> None:
        _, first = self.version("first")
        _, second = self.version("second")
        self.link(first, second)
        self.link(second, first)
        report = self.store.create_impact_report(
            changed_version_id=first["id"], max_depth=3
        )
        self.assertEqual("CYCLE_DETECTED", report["qualification"])
        self.assertEqual(1, report["summary"]["cycle_count"])
        self.assertEqual(report["cycle_paths"][0][0], report["cycle_paths"][0][-1])

    def test_latest_available_contract_compliance_is_included(self) -> None:
        _, upstream = self.version("upstream")
        downstream_dataset, downstream = self.version("downstream")
        self.link(upstream, downstream)
        contract = self.store.create_data_contract(
            dataset_id=downstream_dataset["id"],
            name="downstream-contract",
            fields={
                "id": {
                    "types": ["integer"],
                    "required": True,
                    "nullable": False,
                    "max_missing_rate": 0.0,
                    "unique": True,
                },
                "label": {
                    "types": ["string"],
                    "required": True,
                    "nullable": False,
                    "max_missing_rate": 0.0,
                    "unique": False,
                },
            },
        )
        contract_report = self.store.create_contract_report(
            contract_id=contract["id"], version_id=downstream["id"]
        )
        report = self.store.create_impact_report(
            changed_version_id=upstream["id"], max_depth=1
        )
        compliance = report["affected_versions"][0]["contract_compliance"]
        self.assertEqual("AVAILABLE", compliance["status"])
        self.assertEqual("COMPATIBLE", compliance["verdict"])
        self.assertEqual(contract_report["id"], compliance["contract_report_id"])

    def test_report_is_idempotent_and_tables_are_immutable(self) -> None:
        _, upstream = self.version("upstream")
        _, downstream = self.version("downstream")
        link = self.link(upstream, downstream)
        first = self.store.create_impact_report(
            changed_version_id=upstream["id"], max_depth=2, actor="first"
        )
        second = self.store.create_impact_report(
            changed_version_id=upstream["id"], max_depth=2, actor="second"
        )
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.store.list_audit(action="IMPACT_REPORT_CREATED")))
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("DELETE FROM lineage_links WHERE id = ?", (link["id"],))
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE impact_reports SET qualification = 'ISOLATED' WHERE id = ?",
                    (first["id"],),
                )

    def test_strict_inputs_forbid_client_results_and_bound_depth(self) -> None:
        with self.assertRaises(PydanticValidationError):
            ImpactReportCreate.model_validate(
                {
                    "changed_version_id": "changed",
                    "max_depth": 2,
                    "qualification": "ISOLATED",
                    "paths": [],
                }
            )
        for invalid_depth in (0, 11):
            with self.assertRaises(PydanticValidationError):
                ImpactReportCreate.model_validate(
                    {"changed_version_id": "changed", "max_depth": invalid_depth}
                )
        with self.assertRaises(PydanticValidationError):
            LineageLinkCreate.model_validate(
                {
                    "upstream_version_id": "up",
                    "downstream_version_id": "down",
                    "relation_type": "UNKNOWN",
                    "link_hash": "client-value",
                }
            )


if __name__ == "__main__":
    unittest.main()
