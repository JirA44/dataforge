from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.models import DriftComparisonCreate
from dataforge.store import DataForgeStore


SCHEMA = {
    "fields": {
        "id": {"type": "integer", "required": True},
        "name": {"type": "string", "required": False},
    },
    "allow_extra": True,
}


class DataForgeDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "drift.sqlite3")
        self.source = self.store.create_source(name="fixture", kind="test")
        self.dataset = self.store.create_dataset(name="drift-dataset", schema_spec=SCHEMA)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def version(self, records):
        return self.store.create_version(
            dataset_id=self.dataset["id"],
            source_id=self.source["id"],
            records=records,
        )

    def test_stable_report_is_idempotent_and_audited_once(self) -> None:
        records = [{"id": index, "name": f"row-{index}"} for index in range(10)]
        baseline = self.version(records)
        candidate = self.version(list(reversed(records)))

        first = self.store.create_drift_report(
            baseline_version_id=baseline["id"],
            candidate_version_id=candidate["id"],
            actor="drift-test",
        )
        second = self.store.create_drift_report(
            baseline_version_id=baseline["id"],
            candidate_version_id=candidate["id"],
            actor="should-not-create-a-second-event",
        )

        self.assertEqual("STABLE", first["verdict"])
        self.assertEqual(first, second)
        self.assertEqual(64, len(first["report_hash"]))
        audit = self.store.list_audit(action="DRIFT_REPORT_CREATED")
        self.assertEqual(1, len(audit))
        self.assertEqual("drift-test", audit[0]["actor"])

    def test_schema_and_missingness_drift_is_computed_from_content(self) -> None:
        baseline = self.version([{"id": index, "name": "ok"} for index in range(10)])
        candidate = self.version(
            [
                {"id": str(index), "name": None, "segment": "new"}
                for index in range(10)
            ]
        )
        report = self.store.create_drift_report(
            baseline_version_id=baseline["id"], candidate_version_id=candidate["id"]
        )

        self.assertEqual("DRIFTED", report["verdict"])
        schema = report["metrics"]["schema"]
        self.assertEqual(["segment"], schema["added_fields"])
        self.assertEqual("id", schema["type_changes"][0]["field"])
        self.assertIn(
            "missing_rate_change_exceeds_threshold", report["metrics"]["drift_triggers"]
        )

    def test_empty_baseline_is_insufficient_not_stable(self) -> None:
        baseline = self.version([])
        candidate = self.version([{"id": 1, "name": "Ada"}])
        report = self.store.create_drift_report(
            baseline_version_id=baseline["id"], candidate_version_id=candidate["id"]
        )

        self.assertEqual("INSUFFICIENT", report["verdict"])
        self.assertIn(
            "baseline_has_no_rows", report["metrics"]["insufficient_reasons"]
        )

    def test_report_is_protected_by_immutable_triggers(self) -> None:
        baseline = self.version([{"id": 1}])
        candidate = self.version([{"id": 2}])
        report = self.store.create_drift_report(
            baseline_version_id=baseline["id"], candidate_version_id=candidate["id"]
        )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE drift_reports SET verdict = 'DRIFTED' WHERE id = ?",
                    (report["id"],),
                )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("DELETE FROM drift_reports WHERE id = ?", (report["id"],))

    def test_drift_input_forbids_client_verdict_and_unknown_fields(self) -> None:
        with self.assertRaises(PydanticValidationError) as context:
            DriftComparisonCreate.model_validate(
                {
                    "candidate_version_id": "candidate",
                    "verdict": "STABLE",
                    "threshold": 99,
                }
            )
        errors = context.exception.errors()
        self.assertEqual(2, len(errors))
        self.assertEqual({"extra_forbidden"}, {error["type"] for error in errors})


if __name__ == "__main__":
    unittest.main()
