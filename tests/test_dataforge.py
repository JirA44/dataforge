from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dataforge.errors import ConflictError, ValidationError
from dataforge.store import DataForgeStore


SCHEMA = {
    "fields": {
        "id": {"type": "integer", "required": True},
        "name": {"type": "string", "required": True},
        "score": {"type": "number", "required": False},
    },
    "allow_extra": False,
}


class DataForgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.store = DataForgeStore(self.db_path)
        self.source = self.store.create_source(
            name="fixture.csv",
            kind="file",
            uri="file:///fixtures/fixture.csv",
            metadata={"owner": "tests"},
            actor="unit-test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_dataset(self, name: str = "customers", schema=SCHEMA):
        return self.store.create_dataset(
            name=name, schema_spec=schema, description="Test dataset", actor="unit-test"
        )

    @staticmethod
    def check(quality, name):
        return next(item for item in quality["checks"] if item["name"] == name)

    def test_clean_declared_dataset_is_verified(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace", "score": 9.5}],
            actor="unit-test",
        )
        self.assertEqual("VERIFIED", version["quality"]["verdict"])
        self.assertEqual("PASS", self.check(version["quality"], "missing")["status"])
        self.assertEqual("PASS", self.check(version["quality"], "duplicates")["status"])
        self.assertEqual("PASS", self.check(version["quality"], "schema")["status"])
        self.assertEqual("PASS", self.check(version["quality"], "provenance")["status"])

    def test_missing_required_value_is_rejected_and_calculated(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"id": 1, "name": None}],
        )
        missing = self.check(version["quality"], "missing")
        self.assertEqual("REJECTED", version["quality"]["verdict"])
        self.assertEqual("FAIL", missing["status"])
        self.assertEqual(1, missing["missing_values"])
        self.assertEqual(0.5, missing["missing_rate"])

    def test_duplicate_rows_are_rejected_and_counted(self) -> None:
        dataset = self.create_dataset()
        record = {"id": 1, "name": "Ada"}
        version = self.store.create_version(
            dataset_id=dataset["id"], source_id=self.source["id"], records=[record, record]
        )
        duplicates = self.check(version["quality"], "duplicates")
        self.assertEqual("REJECTED", version["quality"]["verdict"])
        self.assertEqual(1, duplicates["duplicate_rows"])
        self.assertEqual(0.5, duplicates["duplicate_rate"])

    def test_schema_type_and_extra_fields_are_rejected(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"id": "not-an-integer", "name": "Ada", "unknown": True}],
        )
        schema = self.check(version["quality"], "schema")
        self.assertEqual("REJECTED", version["quality"]["verdict"])
        self.assertEqual(2, schema["violation_count"])
        self.assertEqual({"type_mismatch", "extra_field"}, {v["issue"] for v in schema["violations"]})

    def test_absent_declared_schema_is_insufficient_not_verified(self) -> None:
        dataset = self.create_dataset(name="untyped", schema=None)
        version = self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"id": 1, "name": "Ada"}],
        )
        self.assertEqual("INSUFFICIENT", version["quality"]["verdict"])
        self.assertEqual("INSUFFICIENT", self.check(version["quality"], "schema")["status"])

    def test_empty_dataset_is_insufficient_not_verified(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"], source_id=self.source["id"], records=[]
        )
        self.assertEqual("INSUFFICIENT", version["quality"]["verdict"])
        self.assertEqual("INSUFFICIENT", self.check(version["quality"], "missing")["status"])
        self.assertEqual("INSUFFICIENT", self.check(version["quality"], "duplicates")["status"])

    def test_versions_are_numbered_and_provenance_is_chained(self) -> None:
        dataset = self.create_dataset()
        first = self.store.create_version(
            dataset_id=dataset["id"], source_id=self.source["id"], records=[{"id": 1, "name": "Ada"}]
        )
        second = self.store.create_version(
            dataset_id=dataset["id"], source_id=self.source["id"], records=[{"id": 2, "name": "Grace"}]
        )
        self.assertEqual(1, first["version_number"])
        self.assertEqual(2, second["version_number"])
        self.assertEqual(first["id"], second["previous_version_id"])
        self.assertEqual(first["provenance_hash"], second["provenance"]["previous_provenance_hash"])
        integrity = self.store.verify_provenance(second["id"])
        self.assertTrue(integrity["valid"])
        self.assertEqual(2, integrity["checked_versions"])
        self.assertEqual([], integrity["issues"])

    def test_sqlite_triggers_block_version_update_and_delete(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"], source_id=self.source["id"], records=[{"id": 1, "name": "Ada"}]
        )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE dataset_versions SET record_count = 999 WHERE id = ?", (version["id"],)
                )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("DELETE FROM dataset_versions WHERE id = ?", (version["id"],))

    def test_audit_is_append_only_and_records_actor(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"],
            source_id=self.source["id"],
            records=[{"id": 1, "name": "Ada"}],
            actor="quality-bot",
        )
        audit = self.store.list_audit(resource_id=version["id"])
        self.assertEqual(1, len(audit))
        self.assertEqual("VERSION_CREATED", audit[0]["action"])
        self.assertEqual("quality-bot", audit[0]["actor"])
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM audit_log")

    def test_recheck_adds_evaluation_without_mutating_version(self) -> None:
        dataset = self.create_dataset()
        version = self.store.create_version(
            dataset_id=dataset["id"], source_id=self.source["id"], records=[{"id": 1, "name": "Ada"}]
        )
        evaluation = self.store.run_quality_checks(version["id"], actor="rechecker")
        self.assertEqual("VERIFIED", evaluation["verdict"])
        with self.store.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM quality_evaluations WHERE version_id = ?", (version["id"],)
            ).fetchone()[0]
        self.assertEqual(2, count)
        self.assertEqual(version["content_hash"], self.store.get_version(version["id"])["content_hash"])

    def test_invalid_schema_and_duplicate_names_return_domain_errors(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create_dataset(
                name="bad", schema_spec={"fields": {"x": {"type": "made-up"}}}
            )
        self.create_dataset()
        with self.assertRaises(ConflictError):
            self.create_dataset()


if __name__ == "__main__":
    unittest.main()

