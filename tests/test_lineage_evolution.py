from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.errors import NotFoundError
from dataforge.models import LineageEvolutionDossierCreate
from dataforge.store import DataForgeStore


class DataForgeLineageEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "evolution.sqlite3")
        self.source = self.store.create_source(name="evolution-source", kind="test")
        self.counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def dataset(self, label="dataset"):
        self.counter += 1
        return self.store.create_dataset(name=f"{label}-{self.counter}")

    def version(self, dataset, value):
        return self.store.create_version(dataset_id=dataset["id"], source_id=self.source["id"], records=[{"value": value}])

    def link(self, upstream, downstream):
        return self.store.create_lineage_link(upstream_version_id=upstream["id"], downstream_version_id=downstream["id"], relation_type="DERIVED_FROM")

    def roots(self):
        dataset = self.dataset("root")
        return dataset, self.version(dataset, 1), self.version(dataset, 2)

    def test_explained_addition_is_server_ordered_and_attributed(self) -> None:
        dataset, first, second = self.roots()
        shared = self.version(self.dataset("shared"), 10)
        added = self.version(self.dataset("added"), 20)
        self.link(first, shared)
        self.link(second, shared)
        self.link(second, added)
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[second["id"], first["id"]])
        self.assertEqual("EXPLAINED", dossier["qualification"])
        self.assertEqual([first["id"], second["id"]], dossier["chronological_version_ids"])
        self.assertEqual([added["id"]], dossier["transitions"][0]["added_dependency_version_ids"])
        self.assertEqual("ADDED", dossier["transitions"][0]["touched_branches"][0]["action"])
        self.assertEqual(dataset["id"], dossier["dataset_id"])

    def test_explained_removal_is_attributed(self) -> None:
        _, first, second = self.roots()
        kept = self.version(self.dataset("kept"), 10)
        removed = self.version(self.dataset("removed"), 20)
        self.link(first, kept); self.link(first, removed); self.link(second, kept)
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual([removed["id"]], dossier["transitions"][0]["removed_dependency_version_ids"])
        self.assertEqual(1, dossier["summary"]["removed_dependency_count"])

    def test_no_downstream_proof_is_insufficient(self) -> None:
        _, first, second = self.roots()
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual("INSUFFICIENT", dossier["qualification"])
        self.assertIn("no_downstream_lineage_evidence", dossier["insufficient_reasons"])

    def test_versions_from_different_datasets_are_incompatible(self) -> None:
        first = self.version(self.dataset("one"), 1)
        second = self.version(self.dataset("two"), 2)
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual("INCOMPATIBLE", dossier["qualification"])
        self.assertIsNone(dossier["dataset_id"])

    def test_corrupted_selected_provenance_is_insufficient(self) -> None:
        _, first, second = self.roots()
        target = self.version(self.dataset("target"), 3)
        self.link(first, target); self.link(second, target)
        with self.store.db.connect() as connection:
            connection.execute("DROP TRIGGER immutable_versions_update")
            connection.execute("UPDATE dataset_versions SET content_hash=? WHERE id=?", ("0" * 64, second["id"]))
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual("INSUFFICIENT", dossier["qualification"])
        self.assertTrue(any(second["id"] in reason for reason in dossier["insufficient_reasons"]))

    def test_orphan_reference_can_be_resolved_and_remains_partial(self) -> None:
        _, first, second = self.roots()
        target = self.version(self.dataset("target"), 3)
        self.link(first, target); self.link(second, target)
        with self.store.db.connect() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("""INSERT INTO lineage_links
                (id,upstream_version_id,downstream_version_id,relation_type,link_hash,created_at)
                VALUES(?,?,?,?,?,?)""", ("orphan", first["id"], "missing", "DERIVED_FROM", "f" * 64, "2026-08-24T00:00:00Z"))
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual("PARTIAL", dossier["qualification"])
        transition = dossier["transitions"][0]
        self.assertTrue(transition["resolved_orphan_references"])
        self.assertEqual([], transition["new_orphan_references"])

    def test_new_downstream_break_is_partial_and_worst(self) -> None:
        _, first, second = self.roots()
        healthy = self.version(self.dataset("healthy"), 3)
        broken = self.version(self.dataset("broken"), 4)
        self.link(first, healthy); self.link(second, healthy); self.link(second, broken)
        with self.store.db.connect() as connection:
            connection.execute("DROP TRIGGER immutable_versions_update")
            connection.execute("UPDATE dataset_versions SET provenance_hash=? WHERE id=?", ("0" * 64, broken["id"]))
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual("PARTIAL", dossier["qualification"])
        self.assertTrue(dossier["worst_transition"]["new_breaks"])
        self.assertGreaterEqual(dossier["worst_transition"]["severity_score"], 100)

    def test_reachable_cycle_is_partial_and_exposed(self) -> None:
        _, first, second = self.roots()
        a = self.version(self.dataset("a"), 3); b = self.version(self.dataset("b"), 4)
        self.link(first, a); self.link(second, a); self.link(a, b); self.link(b, a)
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual("PARTIAL", dossier["qualification"])
        self.assertTrue(dossier["states"][0]["cycles"])

    def test_snapshot_is_order_independent_idempotent_and_audited_once(self) -> None:
        _, first, second = self.roots()
        target = self.version(self.dataset("target"), 3)
        self.link(first, target); self.link(second, target)
        created = self.store.create_lineage_evolution_dossier(version_ids=[second["id"], first["id"]], actor="first")
        replay = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]], actor="second")
        self.assertEqual(created, replay)
        self.assertEqual(64, len(created["snapshot_hash"])); self.assertEqual(64, len(created["dossier_hash"]))
        audit = self.store.list_audit(action="LINEAGE_EVOLUTION_DOSSIER_CREATED")
        self.assertEqual(1, len(audit)); self.assertEqual("first", audit[0]["actor"])

    def test_dossier_is_immutable_gettable_and_listed(self) -> None:
        _, first, second = self.roots()
        dossier = self.store.create_lineage_evolution_dossier(version_ids=[first["id"], second["id"]])
        self.assertEqual(dossier, self.store.get_lineage_evolution_dossier(dossier["id"]))
        self.assertEqual([dossier], self.store.list_lineage_evolution_dossiers())
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("UPDATE lineage_evolution_dossiers SET qualification='PARTIAL' WHERE id=?", (dossier["id"],))

    def test_strict_input_rejects_client_results_bounds_and_missing_version(self) -> None:
        invalid = [
            {"version_ids": ["one"]},
            {"version_ids": ["same", "same"]},
            {"version_ids": [str(index) for index in range(101)]},
            {"version_ids": ["one", "two"], "qualification": "EXPLAINED"},
            {"version_ids": ["one", "two"], "transitions": []},
        ]
        for payload in invalid:
            with self.assertRaises(PydanticValidationError):
                LineageEvolutionDossierCreate.model_validate(payload)
        with self.assertRaises(NotFoundError):
            self.store.create_lineage_evolution_dossier(version_ids=["missing", "also-missing"])


if __name__ == "__main__":
    unittest.main()
