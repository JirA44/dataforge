from __future__ import annotations

import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.models import ContractCompatibilityCreate
from dataforge.store import DataForgeStore


BASE = {
    "name": "contract",
    "fields": {
        "id": {
            "types": ["integer"],
            "required": True,
            "nullable": False,
            "max_missing_rate": 0.0,
            "unique": True,
        }
    },
    "allow_extra": False,
    "min_rows": 1,
    "max_rows": 100,
    "max_duplicate_rate": 0.0,
}


class DataForgeContractCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "compatibility.sqlite3")
        self.dataset = self.store.create_dataset(name="compatibility-dataset")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def contract(self, definition=None, dataset_id=None):
        payload = deepcopy(definition or BASE)
        return self.store.create_data_contract(
            dataset_id=dataset_id or self.dataset["id"], **payload
        )

    def report(self, baseline, candidate, actor="test"):
        return self.store.create_contract_compatibility_report(
            baseline_contract_id=baseline["id"],
            candidate_contract_id=candidate["id"],
            actor=actor,
        )

    def test_equivalent_acceptance_sets_are_fully_compatible(self) -> None:
        baseline = self.contract()
        candidate_definition = deepcopy(BASE)
        candidate_definition["name"] = "renamed-contract"
        candidate = self.contract(candidate_definition)
        report = self.report(baseline, candidate)
        self.assertEqual("FULLY_COMPATIBLE", report["qualification"])
        self.assertTrue(report["backward"]["compatible"])
        self.assertTrue(report["forward"]["compatible"])
        self.assertEqual([], report["backward"]["reasons"])

    def test_type_relaxation_is_backward_only(self) -> None:
        baseline = self.contract()
        candidate_definition = deepcopy(BASE)
        candidate_definition["fields"]["id"]["types"] = ["integer", "string"]
        candidate = self.contract(candidate_definition)
        report = self.report(baseline, candidate)
        self.assertEqual("BACKWARD_COMPATIBLE", report["qualification"])
        self.assertTrue(report["backward"]["compatible"])
        self.assertFalse(report["forward"]["compatible"])
        self.assertEqual(["string"], report["changes"]["type_changes"][0]["added_types"])
        self.assertIn(
            "types",
            {item["constraint"] for item in report["changes"]["relaxed_constraints"]},
        )

    def test_type_tightening_is_forward_only(self) -> None:
        baseline_definition = deepcopy(BASE)
        baseline_definition["fields"]["id"]["types"] = ["integer", "string"]
        baseline = self.contract(baseline_definition)
        candidate = self.contract()
        report = self.report(baseline, candidate)
        self.assertEqual("FORWARD_COMPATIBLE", report["qualification"])
        self.assertFalse(report["backward"]["compatible"])
        self.assertTrue(report["forward"]["compatible"])
        self.assertIn(
            "allowed_types_narrowed",
            {reason["code"] for reason in report["backward"]["reasons"]},
        )

    def test_replacement_required_field_is_breaking_with_complete_diff(self) -> None:
        baseline_definition = deepcopy(BASE)
        baseline_definition["fields"]["old"] = {
            "types": ["string"],
            "required": True,
            "nullable": True,
            "max_missing_rate": 0.2,
            "unique": False,
        }
        baseline_definition["allow_extra"] = True
        baseline_definition["max_duplicate_rate"] = 0.5
        baseline = self.contract(baseline_definition)

        candidate_definition = deepcopy(BASE)
        candidate_definition["fields"]["id"].update(
            {"nullable": True, "max_missing_rate": 0.1, "unique": False}
        )
        candidate_definition["fields"]["new"] = {
            "types": ["number"],
            "required": True,
            "nullable": False,
            "max_missing_rate": 0.0,
            "unique": False,
        }
        candidate_definition["min_rows"] = 2
        candidate_definition["max_rows"] = 50
        candidate_definition["max_duplicate_rate"] = 0.1
        candidate = self.contract(candidate_definition)

        report = self.report(baseline, candidate)
        self.assertEqual("BREAKING", report["qualification"])
        changes = report["changes"]
        self.assertEqual(["new"], changes["added_fields"])
        self.assertEqual(["old"], changes["removed_fields"])
        self.assertEqual(["new"], changes["new_required_fields"])
        self.assertTrue(changes["tightened_constraints"])
        self.assertTrue(changes["relaxed_constraints"])
        self.assertIn("id", {item["field"] for item in changes["nullable_changes"]})
        self.assertFalse(report["backward"]["compatible"])
        self.assertFalse(report["forward"]["compatible"])

    def test_cross_dataset_comparison_is_insufficient(self) -> None:
        baseline = self.contract()
        other_dataset = self.store.create_dataset(name="other-contract-dataset")
        candidate = self.contract(dataset_id=other_dataset["id"])
        report = self.report(baseline, candidate)
        self.assertEqual("INSUFFICIENT", report["qualification"])
        self.assertIsNone(report["backward"]["compatible"])
        self.assertIsNone(report["forward"]["compatible"])
        self.assertEqual(
            ["contracts_belong_to_different_datasets"], report["insufficient_reasons"]
        )

    def test_snapshots_and_report_are_hashed_idempotently_and_audited_once(self) -> None:
        baseline = self.contract()
        candidate = self.contract()
        first = self.report(baseline, candidate, actor="first")
        second = self.report(baseline, candidate, actor="second")
        self.assertEqual(first, second)
        for key in (
            "baseline_snapshot_hash",
            "candidate_snapshot_hash",
            "evidence_hash",
            "report_hash",
        ):
            self.assertEqual(64, len(first[key]))
        audit = self.store.list_audit(action="CONTRACT_COMPATIBILITY_REPORT_CREATED")
        self.assertEqual(1, len(audit))
        self.assertEqual("first", audit[0]["actor"])
        self.assertEqual(
            [first], self.store.list_contract_compatibility_reports(baseline["id"])
        )

    def test_compatibility_report_is_immutable(self) -> None:
        baseline = self.contract()
        candidate = self.contract()
        report = self.report(baseline, candidate)
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE contract_compatibility_reports "
                    "SET qualification = 'BREAKING' WHERE id = ?",
                    (report["id"],),
                )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM contract_compatibility_reports WHERE id = ?",
                    (report["id"],),
                )

    def test_strict_input_forbids_client_verdict_diff_and_results(self) -> None:
        with self.assertRaises(PydanticValidationError) as context:
            ContractCompatibilityCreate.model_validate(
                {
                    "baseline_contract_id": "baseline",
                    "candidate_contract_id": "candidate",
                    "qualification": "FULLY_COMPATIBLE",
                    "changes": {},
                    "backward": {"compatible": True},
                }
            )
        self.assertEqual(
            3,
            len(
                [
                    error
                    for error in context.exception.errors()
                    if error["type"] == "extra_forbidden"
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
