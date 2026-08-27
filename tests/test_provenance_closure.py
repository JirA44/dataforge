from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.errors import NotFoundError
from dataforge.models import ProvenanceClosureCreate
from dataforge.store import DataForgeStore


class DataForgeProvenanceClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "closure.sqlite3")
        self.source = self.store.create_source(name="closure-source", kind="test")
        self.counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def dataset(self, label="dataset"):
        self.counter += 1
        return self.store.create_dataset(name=f"{label}-{self.counter}")

    def version(self, dataset, value):
        return self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"value": value}],
        )

    def test_complete_closure_reconstructs_previous_chain_to_source(self) -> None:
        dataset = self.dataset()
        first = self.version(dataset, 1)
        second = self.version(dataset, 2)
        report = self.store.create_provenance_closure_report(version_ids=[second["id"]])
        self.assertEqual("COMPLETE", report["qualification"])
        self.assertEqual(sorted([first["id"], second["id"]]), report["closure_version_ids"])
        self.assertEqual([self.source["id"]], report["source_ids"])
        self.assertTrue(all(item["valid"] for item in report["integrity"]))
        self.assertTrue(all(chain["complete"] for chain in report["chains"]))

    def test_single_first_version_is_insufficient_without_ancestry_evidence(self) -> None:
        dataset = self.dataset()
        first = self.version(dataset, 1)
        report = self.store.create_provenance_closure_report(version_ids=[first["id"]])
        self.assertEqual("INSUFFICIENT", report["qualification"])
        self.assertEqual(1, report["summary"]["closure_version_count"])
        self.assertEqual([], report["breaks"])

    def test_unused_newer_version_makes_older_target_partial_and_orphaned(self) -> None:
        dataset = self.dataset()
        first = self.version(dataset, 1)
        second = self.version(dataset, 2)
        third = self.version(dataset, 3)
        report = self.store.create_provenance_closure_report(version_ids=[second["id"]])
        self.assertEqual("PARTIAL", report["qualification"])
        self.assertEqual([third["id"]], report["unused_versions"])
        self.assertEqual([third["id"]], report["orphan_versions"])

    def test_disconnected_selected_versions_are_incompatible(self) -> None:
        first = self.version(self.dataset("first"), 1)
        second = self.version(self.dataset("second"), 2)
        report = self.store.create_provenance_closure_report(
            version_ids=[first["id"], second["id"]]
        )
        self.assertEqual("INCOMPATIBLE", report["qualification"])
        self.assertEqual(2, report["summary"]["connected_component_count"])

    def test_cycle_is_detected_as_broken(self) -> None:
        first = self.version(self.dataset("first"), 1)
        second = self.version(self.dataset("second"), 2)
        self.store.create_lineage_link(
            upstream_version_id=first["id"],
            downstream_version_id=second["id"],
            relation_type="DERIVED_FROM",
        )
        self.store.create_lineage_link(
            upstream_version_id=second["id"],
            downstream_version_id=first["id"],
            relation_type="DERIVED_FROM",
        )
        report = self.store.create_provenance_closure_report(version_ids=[second["id"]])
        self.assertEqual("BROKEN", report["qualification"])
        self.assertEqual(1, len(report["cycles"]))
        self.assertEqual(report["cycles"][0][0], report["cycles"][0][-1])

    def test_recalculated_content_hash_mismatch_is_broken(self) -> None:
        dataset = self.dataset()
        version = self.version(dataset, 1)
        with self.store.db.connect() as connection:
            connection.execute("DROP TRIGGER immutable_versions_update")
            connection.execute(
                "UPDATE dataset_versions SET content_hash = ? WHERE id = ?",
                ("0" * 64, version["id"]),
            )
        report = self.store.create_provenance_closure_report(version_ids=[version["id"]])
        self.assertEqual("BROKEN", report["qualification"])
        self.assertIn("content_hash_mismatch", {item["code"] for item in report["breaks"]})

    def test_missing_lineage_reference_is_detected_without_invention(self) -> None:
        dataset = self.dataset()
        version = self.version(dataset, 1)
        with self.store.db.connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO lineage_links
                  (id, upstream_version_id, downstream_version_id, relation_type,
                   link_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "broken-link",
                    "missing-upstream",
                    version["id"],
                    "DERIVED_FROM",
                    "f" * 64,
                    "2026-08-24T00:00:00Z",
                ),
            )
        report = self.store.create_provenance_closure_report(version_ids=[version["id"]])
        self.assertEqual("BROKEN", report["qualification"])
        self.assertEqual("missing-upstream", report["missing_references"][0]["details"]["reference_id"])
        self.assertNotIn("missing-upstream", report["closure_version_ids"])
        self.assertFalse(any("missing-upstream" in chain["version_path"] for chain in report["chains"]))

    def test_order_independent_snapshot_is_idempotent_and_audited_once(self) -> None:
        dataset = self.dataset()
        first = self.version(dataset, 1)
        second = self.version(dataset, 2)
        first_report = self.store.create_provenance_closure_report(
            version_ids=[second["id"], first["id"]], actor="first"
        )
        second_report = self.store.create_provenance_closure_report(
            version_ids=[first["id"], second["id"]], actor="second"
        )
        self.assertEqual(first_report, second_report)
        self.assertEqual(sorted([first["id"], second["id"]]), first_report["requested_version_ids"])
        self.assertEqual(64, len(first_report["snapshot_hash"]))
        self.assertEqual(64, len(first_report["report_hash"]))
        audit = self.store.list_audit(action="PROVENANCE_CLOSURE_REPORT_CREATED")
        self.assertEqual(1, len(audit))
        self.assertEqual("first", audit[0]["actor"])

    def test_report_is_immutable_and_listed(self) -> None:
        dataset = self.dataset()
        version = self.version(dataset, 1)
        report = self.store.create_provenance_closure_report(version_ids=[version["id"]])
        self.assertEqual([report], self.store.list_provenance_closure_reports())
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE provenance_closure_reports SET qualification = 'COMPLETE' WHERE id = ?",
                    (report["id"],),
                )

    def test_strict_input_bounds_duplicates_and_forbids_client_verdict(self) -> None:
        invalid_payloads = [
            {"version_ids": []},
            {"version_ids": ["same", "same"]},
            {"version_ids": [str(index) for index in range(51)]},
            {"version_ids": ["valid"], "qualification": "COMPLETE", "chains": []},
        ]
        for payload in invalid_payloads:
            with self.assertRaises(PydanticValidationError):
                ProvenanceClosureCreate.model_validate(payload)
        with self.assertRaises(NotFoundError):
            self.store.create_provenance_closure_report(version_ids=["missing"])


if __name__ == "__main__":
    unittest.main()
