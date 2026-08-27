from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from dataforge.models import ContractCheckCreate, DataContractCreate
from dataforge.store import DataForgeStore


CONTRACT = {
    "name": "customers-v1",
    "fields": {
        "id": {
            "types": ["integer"],
            "required": True,
            "nullable": False,
            "max_missing_rate": 0.0,
            "unique": True,
        },
        "name": {
            "types": ["string"],
            "required": True,
            "nullable": False,
            "max_missing_rate": 0.1,
            "unique": False,
        },
    },
    "allow_extra": False,
    "min_rows": 1,
    "max_rows": 100,
    "max_duplicate_rate": 0.0,
}


class DataForgeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = DataForgeStore(Path(self.temp_dir.name) / "contracts.sqlite3")
        self.source = self.store.create_source(name="contract-input", kind="test")
        self.dataset = self.store.create_dataset(name="contract-customers")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def contract(self, **changes):
        definition = dict(CONTRACT)
        definition.update(changes)
        return self.store.create_data_contract(dataset_id=self.dataset["id"], **definition)

    def version(self, records):
        return self.store.create_version(
            dataset_id=self.dataset["id"], source_id=self.source["id"], records=records
        )

    def test_compatible_report_is_computed_and_hashed(self) -> None:
        contract = self.contract()
        version = self.version([{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}])
        report = self.store.create_contract_report(
            contract_id=contract["id"], version_id=version["id"]
        )
        self.assertEqual("COMPATIBLE", report["verdict"])
        self.assertEqual([], report["violations"])
        self.assertEqual(64, len(report["report_hash"]))
        self.assertEqual(["integer"], report["metrics"]["fields"]["id"]["observed_types"])

    def test_schema_missing_uniqueness_and_full_row_duplicates_are_violations(self) -> None:
        contract = self.contract()
        records = [
            {"id": 1, "name": None, "extra": True},
            {"id": 1, "name": None, "extra": True},
            {"id": "bad"},
        ]
        version = self.version(records)
        report = self.store.create_contract_report(
            contract_id=contract["id"], version_id=version["id"]
        )
        self.assertEqual("VIOLATION", report["verdict"])
        codes = {violation["code"] for violation in report["violations"]}
        self.assertTrue(
            {
                "extra_field",
                "type_not_allowed",
                "null_not_allowed",
                "missing_rate_exceeded",
                "uniqueness_violated",
                "duplicate_rate_exceeded",
            }.issubset(codes)
        )

    def test_empty_version_is_insufficient_not_compatible(self) -> None:
        contract = self.contract(min_rows=0)
        version = self.version([])
        report = self.store.create_contract_report(
            contract_id=contract["id"], version_id=version["id"]
        )
        self.assertEqual("INSUFFICIENT", report["verdict"])
        self.assertEqual(["no_rows_to_evaluate"], report["insufficient_reasons"])
        self.assertEqual([], report["violations"])

    def test_contract_versions_are_numbered_and_chained(self) -> None:
        first = self.contract()
        second = self.contract(name="customers-v2", max_duplicate_rate=0.05)
        self.assertEqual(1, first["version_number"])
        self.assertEqual(2, second["version_number"])
        self.assertEqual(first["id"], second["previous_contract_id"])
        self.assertNotEqual(first["contract_hash"], second["contract_hash"])
        self.assertEqual([2, 1], [item["version_number"] for item in self.store.list_data_contracts(self.dataset["id"])])

    def test_same_immutable_inputs_return_same_report_and_one_audit_event(self) -> None:
        contract = self.contract()
        version = self.version([{"id": 1, "name": "Ada"}])
        first = self.store.create_contract_report(
            contract_id=contract["id"], version_id=version["id"], actor="first"
        )
        second = self.store.create_contract_report(
            contract_id=contract["id"], version_id=version["id"], actor="second"
        )
        self.assertEqual(first, second)
        audit = self.store.list_audit(action="CONTRACT_REPORT_CREATED")
        self.assertEqual(1, len(audit))
        self.assertEqual("first", audit[0]["actor"])

    def test_contract_and_report_are_immutable_and_audit_is_append_only(self) -> None:
        contract = self.contract()
        version = self.version([{"id": 1, "name": "Ada"}])
        report = self.store.create_contract_report(
            contract_id=contract["id"], version_id=version["id"]
        )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE data_contracts SET name = 'changed' WHERE id = ?", (contract["id"],)
                )
        with self.store.db.connect() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("DELETE FROM contract_reports WHERE id = ?", (report["id"],))

    def test_contract_parameters_are_bounded_and_extra_fields_forbidden(self) -> None:
        with self.assertRaises(PydanticValidationError):
            DataContractCreate.model_validate({**CONTRACT, "max_duplicate_rate": 1.1})
        with self.assertRaises(PydanticValidationError):
            DataContractCreate.model_validate({**CONTRACT, "server_verdict": "COMPATIBLE"})

    def test_client_cannot_submit_report_verdict_or_violations(self) -> None:
        with self.assertRaises(PydanticValidationError) as context:
            ContractCheckCreate.model_validate(
                {"version_id": "candidate", "verdict": "COMPATIBLE", "violations": []}
            )
        self.assertEqual(
            ["extra_forbidden", "extra_forbidden"],
            sorted(error["type"] for error in context.exception.errors()),
        )

    def test_contract_cannot_check_version_from_another_dataset(self) -> None:
        contract = self.contract()
        other = self.store.create_dataset(name="other")
        version = self.store.create_version(
            dataset_id=other["id"], source_id=self.source["id"], records=[{"id": 1}]
        )
        from dataforge.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.store.create_contract_report(
                contract_id=contract["id"], version_id=version["id"]
            )


if __name__ == "__main__":
    unittest.main()
