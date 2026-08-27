from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# The module exposes a conventional global ASGI app. Keep its default DB outside the project.
os.environ.setdefault(
    "DATAFORGE_DB_PATH", str(Path(tempfile.gettempdir()) / "dataforge-api-import.sqlite3")
)

try:
    from fastapi.testclient import TestClient
    from dataforge.api import create_app
except ModuleNotFoundError:
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    FASTAPI_AVAILABLE = False
else:
    FASTAPI_AVAILABLE = True


@unittest.skipUnless(FASTAPI_AVAILABLE, "FastAPI test dependencies are not installed")
class DataForgeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        app = create_app(str(Path(self.temp_dir.name) / "api.sqlite3"))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_health_and_openapi(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "service": "dataforge", "version": "1.0.7"}, response.json())
        info = self.client.get("/info")
        self.assertEqual(200, info.status_code)
        self.assertEqual("1.0.7", info.json()["version"])
        self.assertEqual("V1.07", info.json()["release"])
        self.assertIn("bidirectional-contract-compatibility", info.json()["capabilities"])
        self.assertIn("provenance-closure-dossiers", info.json()["capabilities"])
        self.assertIn("downstream-provenance-impact-dossiers", info.json()["capabilities"])
        self.assertIn("chronological-lineage-evolution-attribution", info.json()["capabilities"])
        spec = self.client.get("/openapi.json")
        self.assertEqual(200, spec.status_code)
        self.assertEqual("1.0.7", spec.json()["info"]["version"])
        self.assertIn("/v1/datasets/{dataset_id}/versions", spec.json()["paths"])
        self.assertIn(
            "/v1/versions/{baseline_version_id}/drift-reports", spec.json()["paths"]
        )
        self.assertIn("/v1/datasets/{dataset_id}/contracts", spec.json()["paths"])
        self.assertIn("/v1/contracts/{contract_id}/reports", spec.json()["paths"])
        self.assertIn("/v1/lineage-links", spec.json()["paths"])
        self.assertIn("/v1/impact-reports", spec.json()["paths"])
        self.assertIn("/v1/contract-compatibility-reports", spec.json()["paths"])
        self.assertIn("/v1/provenance-closure-reports", spec.json()["paths"])
        self.assertIn("/info", spec.json()["paths"])
        self.assertIn("/v1/provenance-impact-dossiers", spec.json()["paths"])
        self.assertIn("/v1/lineage-evolution-dossiers", spec.json()["paths"])

    def test_complete_verified_http_workflow_and_audit(self) -> None:
        headers = {"X-Actor": "api-test"}
        source_response = self.client.post(
            "/v1/sources",
            headers=headers,
            json={"name": "input.json", "kind": "file", "metadata": {"owner": "qa"}},
        )
        self.assertEqual(201, source_response.status_code)
        source = source_response.json()
        dataset_response = self.client.post(
            "/v1/datasets",
            headers=headers,
            json={
                "name": "api-customers",
                "schema": {
                    "fields": {
                        "id": {"type": "integer", "required": True},
                        "name": {"type": "string", "required": True},
                    },
                    "allow_extra": False,
                },
            },
        )
        self.assertEqual(201, dataset_response.status_code)
        dataset = dataset_response.json()
        version_response = self.client.post(
            f"/v1/datasets/{dataset['id']}/versions",
            headers=headers,
            json={"source_id": source["id"], "records": [{"id": 1, "name": "Ada"}]},
        )
        self.assertEqual(201, version_response.status_code)
        version = version_response.json()
        self.assertEqual("VERIFIED", version["quality"]["verdict"])

        integrity = self.client.get(
            f"/v1/versions/{version['id']}/provenance/verify"
        )
        self.assertEqual(200, integrity.status_code)
        self.assertTrue(integrity.json()["valid"])

        audit = self.client.get(
            "/v1/audit", params={"resource_id": version["id"]}
        ).json()["items"]
        self.assertEqual(1, len(audit))
        self.assertEqual("api-test", audit[0]["actor"])

    def test_http_errors_are_explicit(self) -> None:
        missing = self.client.get("/v1/versions/00000000-0000-0000-0000-000000000000")
        self.assertEqual(404, missing.status_code)
        self.assertEqual("NOT_FOUND", missing.json()["error"])
        invalid = self.client.post("/v1/sources", json={"name": "", "kind": "file"})
        self.assertEqual(422, invalid.status_code)

    def test_drift_request_forbids_client_verdict_and_unknown_fields(self) -> None:
        response = self.client.post(
            "/v1/versions/baseline/drift-reports",
            json={
                "candidate_version_id": "candidate",
                "verdict": "STABLE",
                "threshold": 99,
            },
        )
        self.assertEqual(422, response.status_code)

    def test_http_drift_workflow_returns_computed_report(self) -> None:
        source = self.client.post(
            "/v1/sources", json={"name": "drift.json", "kind": "file"}
        ).json()
        dataset = self.client.post(
            "/v1/datasets",
            json={
                "name": "http-drift",
                "schema": {
                    "fields": {"id": {"type": "integer", "required": True}},
                    "allow_extra": False,
                },
            },
        ).json()
        versions = []
        for records in ([{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 1}]):
            response = self.client.post(
                f"/v1/datasets/{dataset['id']}/versions",
                json={"source_id": source["id"], "records": records},
            )
            self.assertEqual(201, response.status_code)
            versions.append(response.json())

        response = self.client.post(
            f"/v1/versions/{versions[0]['id']}/drift-reports",
            json={"candidate_version_id": versions[1]["id"]},
        )
        self.assertEqual(201, response.status_code)
        report = response.json()
        self.assertEqual("STABLE", report["verdict"])
        self.assertEqual(64, len(report["report_hash"]))
        self.assertEqual(
            report,
            self.client.get(f"/v1/drift-reports/{report['id']}").json(),
        )
        listed = self.client.get(
            f"/v1/datasets/{dataset['id']}/drift-reports"
        ).json()
        self.assertEqual([report], listed["items"])

    def test_http_contract_workflow_and_client_verdict_rejection(self) -> None:
        source = self.client.post(
            "/v1/sources", json={"name": "contract.json", "kind": "file"}
        ).json()
        dataset = self.client.post("/v1/datasets", json={"name": "http-contract"}).json()
        contract_response = self.client.post(
            f"/v1/datasets/{dataset['id']}/contracts",
            json={
                "name": "strict-users",
                "fields": {
                    "id": {
                        "types": ["integer"],
                        "required": True,
                        "nullable": False,
                        "max_missing_rate": 0,
                        "unique": True,
                    }
                },
                "max_duplicate_rate": 0,
            },
        )
        self.assertEqual(201, contract_response.status_code)
        contract = contract_response.json()
        version = self.client.post(
            f"/v1/datasets/{dataset['id']}/versions",
            json={"source_id": source["id"], "records": [{"id": 1}, {"id": 2}]},
        ).json()
        report_response = self.client.post(
            f"/v1/contracts/{contract['id']}/reports",
            json={"version_id": version["id"]},
        )
        self.assertEqual(201, report_response.status_code)
        self.assertEqual("COMPATIBLE", report_response.json()["verdict"])
        forbidden = self.client.post(
            f"/v1/contracts/{contract['id']}/reports",
            json={"version_id": version["id"], "verdict": "COMPATIBLE"},
        )
        self.assertEqual(422, forbidden.status_code)

    def test_http_lineage_and_impact_workflow_forbids_client_results(self) -> None:
        source = self.client.post(
            "/v1/sources", json={"name": "lineage.json", "kind": "file"}
        ).json()
        versions = []
        for index in range(2):
            dataset = self.client.post(
                "/v1/datasets", json={"name": f"http-lineage-{index}"}
            ).json()
            version = self.client.post(
                f"/v1/datasets/{dataset['id']}/versions",
                json={"source_id": source["id"], "records": [{"id": index}]},
            ).json()
            versions.append(version)
        link_response = self.client.post(
            "/v1/lineage-links",
            json={
                "upstream_version_id": versions[0]["id"],
                "downstream_version_id": versions[1]["id"],
                "relation_type": "DERIVED_FROM",
            },
        )
        self.assertEqual(201, link_response.status_code)
        report_response = self.client.post(
            "/v1/impact-reports",
            json={"changed_version_id": versions[0]["id"], "max_depth": 2},
        )
        self.assertEqual(201, report_response.status_code)
        self.assertEqual("CONTAINED", report_response.json()["qualification"])
        forbidden = self.client.post(
            "/v1/impact-reports",
            json={
                "changed_version_id": versions[0]["id"],
                "max_depth": 2,
                "qualification": "CONTAINED",
                "paths": [],
            },
        )
        self.assertEqual(422, forbidden.status_code)

    def test_http_contract_compatibility_workflow_forbids_client_diff(self) -> None:
        dataset = self.client.post(
            "/v1/datasets", json={"name": "http-compatibility"}
        ).json()
        contract_body = {
            "name": "http-contract",
            "fields": {
                "id": {
                    "types": ["integer"],
                    "required": True,
                    "nullable": False,
                    "max_missing_rate": 0,
                    "unique": True,
                }
            },
        }
        baseline = self.client.post(
            f"/v1/datasets/{dataset['id']}/contracts", json=contract_body
        ).json()
        contract_body["name"] = "http-contract-v2"
        candidate = self.client.post(
            f"/v1/datasets/{dataset['id']}/contracts", json=contract_body
        ).json()
        response = self.client.post(
            "/v1/contract-compatibility-reports",
            json={
                "baseline_contract_id": baseline["id"],
                "candidate_contract_id": candidate["id"],
            },
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual("FULLY_COMPATIBLE", response.json()["qualification"])
        forbidden = self.client.post(
            "/v1/contract-compatibility-reports",
            json={
                "baseline_contract_id": baseline["id"],
                "candidate_contract_id": candidate["id"],
                "qualification": "FULLY_COMPATIBLE",
                "changes": {},
            },
        )
        self.assertEqual(422, forbidden.status_code)

    def test_http_provenance_closure_workflow_forbids_client_verdict(self) -> None:
        source = self.client.post(
            "/v1/sources", json={"name": "closure.json", "kind": "file"}
        ).json()
        dataset = self.client.post(
            "/v1/datasets", json={"name": "http-closure"}
        ).json()
        versions = []
        for value in (1, 2):
            versions.append(
                self.client.post(
                    f"/v1/datasets/{dataset['id']}/versions",
                    json={"source_id": source["id"], "records": [{"value": value}]},
                ).json()
            )
        response = self.client.post(
            "/v1/provenance-closure-reports",
            json={"version_ids": [versions[1]["id"]]},
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual("COMPLETE", response.json()["qualification"])
        forbidden = self.client.post(
            "/v1/provenance-closure-reports",
            json={
                "version_ids": [versions[1]["id"]],
                "qualification": "COMPLETE",
                "chains": [],
            },
        )
        self.assertEqual(422, forbidden.status_code)

    def test_http_provenance_impact_workflow_forbids_client_metrics(self) -> None:
        source = self.client.post(
            "/v1/sources", json={"name": "downstream-impact.json", "kind": "file"}
        ).json()
        dataset = self.client.post(
            "/v1/datasets", json={"name": "http-provenance-impact"}
        ).json()
        versions = [
            self.client.post(
                f"/v1/datasets/{dataset['id']}/versions",
                json={"source_id": source["id"], "records": [{"value": value}]},
            ).json()
            for value in (1, 2)
        ]
        response = self.client.post(
            "/v1/provenance-impact-dossiers",
            json={"version_ids": [versions[0]["id"]]},
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual("CONTAINED", response.json()["qualification"])
        self.assertEqual(versions[1]["id"], response.json()["affected"][0]["version_id"])
        dossier_id = response.json()["id"]
        self.assertEqual(
            dossier_id,
            self.client.get(f"/v1/provenance-impact-dossiers/{dossier_id}").json()["id"],
        )
        self.assertEqual(1, self.client.get("/v1/provenance-impact-dossiers").json()["count"])
        forbidden = self.client.post(
            "/v1/provenance-impact-dossiers",
            json={
                "version_ids": [versions[0]["id"]],
                "qualification": "WIDESPREAD",
                "summary": {"affected_version_count": 99},
            },
        )
        self.assertEqual(422, forbidden.status_code)

    def test_http_lineage_evolution_workflow_forbids_client_attribution(self) -> None:
        source = self.client.post("/v1/sources", json={"name": "evolution.json", "kind": "file"}).json()
        root_dataset = self.client.post("/v1/datasets", json={"name": "http-evolution-root"}).json()
        roots = [
            self.client.post(
                f"/v1/datasets/{root_dataset['id']}/versions",
                json={"source_id": source["id"], "records": [{"value": value}]},
            ).json()
            for value in (1, 2)
        ]
        target_dataset = self.client.post("/v1/datasets", json={"name": "http-evolution-target"}).json()
        target = self.client.post(
            f"/v1/datasets/{target_dataset['id']}/versions",
            json={"source_id": source["id"], "records": [{"value": 3}]},
        ).json()
        for root in roots:
            self.assertEqual(201, self.client.post(
                "/v1/lineage-links",
                json={"upstream_version_id": root["id"], "downstream_version_id": target["id"], "relation_type": "DERIVED_FROM"},
            ).status_code)
        response = self.client.post(
            "/v1/lineage-evolution-dossiers",
            json={"version_ids": [roots[1]["id"], roots[0]["id"]]},
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual("EXPLAINED", response.json()["qualification"])
        dossier_id = response.json()["id"]
        self.assertEqual(dossier_id, self.client.get(f"/v1/lineage-evolution-dossiers/{dossier_id}").json()["id"])
        self.assertEqual(1, self.client.get("/v1/lineage-evolution-dossiers").json()["count"])
        forbidden = self.client.post(
            "/v1/lineage-evolution-dossiers",
            json={"version_ids": [roots[0]["id"], roots[1]["id"]], "qualification": "EXPLAINED", "transitions": []},
        )
        self.assertEqual(422, forbidden.status_code)


if __name__ == "__main__":
    unittest.main()
