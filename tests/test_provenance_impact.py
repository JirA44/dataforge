from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.errors import NotFoundError
from dataforge.models import ProvenanceImpactDossierCreate
from dataforge.store import DataForgeStore


class DataForgeProvenanceImpactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "impact.sqlite3")
        self.source = self.store.create_source(name="impact-source", kind="test")
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

    def link(self, upstream, downstream):
        return self.store.create_lineage_link(
            upstream_version_id=upstream["id"],
            downstream_version_id=downstream["id"],
            relation_type="DERIVED_FROM",
        )

    def test_contained_impact_follows_next_version_and_worst_branch(self) -> None:
        dataset = self.dataset()
        first = self.version(dataset, 1)
        second = self.version(dataset, 2)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[first["id"]], dataset_ids=[]
        )
        self.assertEqual("CONTAINED", dossier["qualification"])
        self.assertEqual([second["id"]], [item["version_id"] for item in dossier["affected"]])
        self.assertEqual(1, dossier["worst_branch"]["depth"])
        self.assertEqual([first["id"], second["id"]], dossier["worst_branch"]["version_path"])

    def test_dataset_selection_expands_to_versions_and_external_downstream(self) -> None:
        selected_dataset = self.dataset("selected")
        selected_version = self.version(selected_dataset, 1)
        downstream_dataset = self.dataset("downstream")
        downstream = self.version(downstream_dataset, 2)
        self.link(selected_version, downstream)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[], dataset_ids=[selected_dataset["id"]]
        )
        self.assertEqual([selected_version["id"]], dossier["seed_version_ids"])
        self.assertEqual([downstream["id"]], [item["version_id"] for item in dossier["affected"]])

    def test_five_downstream_versions_are_widespread(self) -> None:
        root = self.version(self.dataset("root"), 0)
        downstream = [self.version(self.dataset("leaf"), index) for index in range(5)]
        for item in downstream:
            self.link(root, item)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[root["id"]], dataset_ids=[]
        )
        self.assertEqual("WIDESPREAD", dossier["qualification"])
        self.assertEqual(5, dossier["summary"]["affected_version_count"])
        self.assertEqual(5, dossier["summary"]["widespread_version_threshold"])

    def test_depth_four_is_widespread_and_worst_path_is_deterministic(self) -> None:
        versions = [self.version(self.dataset("chain"), index) for index in range(5)]
        for upstream, downstream in zip(versions, versions[1:]):
            self.link(upstream, downstream)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[versions[0]["id"]], dataset_ids=[]
        )
        self.assertEqual("WIDESPREAD", dossier["qualification"])
        self.assertEqual(4, dossier["summary"]["maximum_depth"])
        self.assertEqual([item["id"] for item in versions], dossier["worst_branch"]["version_path"])

    def test_isolated_selection_is_insufficient(self) -> None:
        version = self.version(self.dataset(), 1)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[version["id"]], dataset_ids=[]
        )
        self.assertEqual("INSUFFICIENT", dossier["qualification"])
        self.assertIn("no_downstream_impact_observed", dossier["insufficient_reasons"])
        self.assertIsNone(dossier["worst_branch"])

    def test_cycle_is_exposed_and_incompatible(self) -> None:
        first = self.version(self.dataset("cycle"), 1)
        second = self.version(self.dataset("cycle"), 2)
        self.link(first, second)
        self.link(second, first)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[first["id"]], dataset_ids=[]
        )
        self.assertEqual("INCOMPATIBLE", dossier["qualification"])
        self.assertTrue(dossier["cycles"])

    def test_corrupted_version_hash_is_incompatible(self) -> None:
        dataset = self.dataset()
        first = self.version(dataset, 1)
        self.version(dataset, 2)
        with self.store.db.connect() as connection:
            connection.execute("DROP TRIGGER immutable_versions_update")
            connection.execute(
                "UPDATE dataset_versions SET content_hash=? WHERE id=?",
                ("0" * 64, first["id"]),
            )
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[first["id"]], dataset_ids=[]
        )
        self.assertEqual("INCOMPATIBLE", dossier["qualification"])
        self.assertIn("content_hash_mismatch", {item["code"] for item in dossier["breaks"]})

    def test_orphan_lineage_reference_is_exposed_without_invention(self) -> None:
        version = self.version(self.dataset(), 1)
        with self.store.db.connect() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO lineage_links
                   (id,upstream_version_id,downstream_version_id,relation_type,link_hash,created_at)
                   VALUES(?,?,?,?,?,?)""",
                ("orphan-link", version["id"], "missing", "DERIVED_FROM", "f" * 64, "2026-08-24T00:00:00Z"),
            )
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[version["id"]], dataset_ids=[]
        )
        self.assertEqual("INCOMPATIBLE", dossier["qualification"])
        self.assertEqual("missing", dossier["orphan_references"][0]["details"]["reference_id"])
        self.assertNotIn("missing", [item["version_id"] for item in dossier["affected"]])

    def test_order_independent_snapshot_is_idempotent_and_audited_once(self) -> None:
        first = self.version(self.dataset("seed"), 1)
        second = self.version(self.dataset("seed"), 2)
        downstream = self.version(self.dataset("target"), 3)
        self.link(first, downstream)
        self.link(second, downstream)
        created = self.store.create_provenance_impact_dossier(
            version_ids=[second["id"], first["id"]], dataset_ids=[], actor="first"
        )
        replay = self.store.create_provenance_impact_dossier(
            version_ids=[first["id"], second["id"]], dataset_ids=[], actor="second"
        )
        self.assertEqual(created, replay)
        self.assertEqual(64, len(created["snapshot_hash"]))
        self.assertEqual(64, len(created["dossier_hash"]))
        audit = self.store.list_audit(action="PROVENANCE_IMPACT_DOSSIER_CREATED")
        self.assertEqual(1, len(audit))
        self.assertEqual("first", audit[0]["actor"])

    def test_dossier_is_immutable_gettable_and_listed(self) -> None:
        version = self.version(self.dataset(), 1)
        dossier = self.store.create_provenance_impact_dossier(
            version_ids=[version["id"]], dataset_ids=[]
        )
        self.assertEqual(dossier, self.store.get_provenance_impact_dossier(dossier["id"]))
        self.assertEqual([dossier], self.store.list_provenance_impact_dossiers())
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE provenance_impact_dossiers SET qualification='CONTAINED' WHERE id=?",
                    (dossier["id"],),
                )

    def test_strict_selection_bounds_client_results_and_missing_ids(self) -> None:
        invalid = [
            {},
            {"version_ids": [], "dataset_ids": []},
            {"version_ids": ["same", "same"]},
            {"version_ids": [str(index) for index in range(51)]},
            {"version_ids": ["v"], "qualification": "CONTAINED"},
            {"dataset_ids": ["d"], "summary": {"affected_version_count": 99}},
        ]
        for payload in invalid:
            with self.assertRaises(PydanticValidationError):
                ProvenanceImpactDossierCreate.model_validate(payload)
        with self.assertRaises(NotFoundError):
            self.store.create_provenance_impact_dossier(
                version_ids=["missing"], dataset_ids=[]
            )


if __name__ == "__main__":
    unittest.main()
