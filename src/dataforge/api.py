"""FastAPI HTTP adapter. All decisions remain in :mod:`dataforge.store`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, Query, Request, status
from fastapi.responses import JSONResponse

from . import __version__
from .errors import ConflictError, DataForgeError, NotFoundError, ValidationError
from .models import (
    ContractCheckCreate,
    ContractCompatibilityCreate,
    DataContractCreate,
    DatasetCreate,
    DriftComparisonCreate,
    ImpactReportCreate,
    LineageLinkCreate,
    LineageEvolutionDossierCreate,
    ProvenanceClosureCreate,
    ProvenanceImpactDossierCreate,
    SourceCreate,
    VersionCreate,
)
from .store import DataForgeStore


def create_app(database_path: Optional[str] = None) -> FastAPI:
    db_path = database_path or os.getenv("DATAFORGE_DB_PATH", "data/dataforge.sqlite3")
    app = FastAPI(
        title="DataForge API",
        version=__version__,
        servers=[{"url": "http://127.0.0.1:8010"}],
        description=(
            "Immutable dataset versions, hashed provenance, conservative computed "
            "quality verdicts, deterministic drift, provenance closure, contract "
            "compatibility and downstream impact reports, with append-only audit."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.store = DataForgeStore(Path(db_path))

    @app.exception_handler(DataForgeError)
    async def handle_domain_error(_: Request, exc: DataForgeError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ConflictError):
            code = status.HTTP_409_CONFLICT
        elif isinstance(exc, ValidationError):
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return JSONResponse(status_code=code, content={"error": exc.code, "message": exc.message})

    def store(request: Request) -> DataForgeStore:
        return request.app.state.store

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "service": "dataforge", "version": __version__}

    @app.get("/info", tags=["system"])
    def info() -> dict:
        return {
            "name": "DataForge",
            "version": __version__,
            "release": "V1.07",
            "capabilities": [
                "immutable-dataset-registry",
                "versioned-contracts",
                "quality-and-drift-reports",
                "lineage-impact-analysis",
                "bidirectional-contract-compatibility",
                "provenance-closure-dossiers",
                "downstream-provenance-impact-dossiers",
                "chronological-lineage-evolution-attribution",
            ],
        }

    @app.post("/v1/sources", status_code=status.HTTP_201_CREATED, tags=["sources"])
    def create_source(
        payload: SourceCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_source(**payload.model_dump(), actor=x_actor)

    @app.get("/v1/sources", tags=["sources"])
    def list_sources(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_sources(limit=limit, offset=offset)
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/sources/{source_id}", tags=["sources"])
    def get_source(source_id: str, request: Request) -> dict:
        return store(request).get_source(source_id)

    @app.post("/v1/datasets", status_code=status.HTTP_201_CREATED, tags=["datasets"])
    def create_dataset(
        payload: DatasetCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        values = payload.model_dump(by_alias=False)
        schema_model = values.pop("schema_spec")
        values["schema_spec"] = schema_model
        return store(request).create_dataset(**values, actor=x_actor)

    @app.get("/v1/datasets", tags=["datasets"])
    def list_datasets(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_datasets(limit=limit, offset=offset)
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/datasets/{dataset_id}", tags=["datasets"])
    def get_dataset(dataset_id: str, request: Request) -> dict:
        return store(request).get_dataset(dataset_id)

    @app.post(
        "/v1/datasets/{dataset_id}/versions",
        status_code=status.HTTP_201_CREATED,
        tags=["versions"],
    )
    def create_version(
        dataset_id: str,
        payload: VersionCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_version(
            dataset_id=dataset_id,
            source_id=payload.source_id,
            records=payload.records,
            actor=x_actor,
        )

    @app.get("/v1/datasets/{dataset_id}/versions", tags=["versions"])
    def list_versions(
        dataset_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        include_records: bool = Query(default=False),
    ) -> dict:
        items = store(request).list_versions(
            dataset_id, limit=limit, offset=offset, include_records=include_records
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/versions/{version_id}", tags=["versions"])
    def get_version(
        version_id: str,
        request: Request,
        include_records: bool = Query(default=True),
    ) -> dict:
        return store(request).get_version(version_id, include_records=include_records)

    @app.get("/v1/versions/{version_id}/provenance/verify", tags=["provenance"])
    def verify_provenance(version_id: str, request: Request) -> dict:
        return store(request).verify_provenance(version_id)

    @app.get("/v1/versions/{version_id}/quality", tags=["quality"])
    def get_quality(version_id: str, request: Request) -> dict:
        return store(request).get_latest_quality(version_id)

    @app.post(
        "/v1/versions/{version_id}/quality-checks",
        status_code=status.HTTP_201_CREATED,
        tags=["quality"],
    )
    def recheck_quality(
        version_id: str,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).run_quality_checks(version_id, actor=x_actor)

    @app.post(
        "/v1/versions/{baseline_version_id}/drift-reports",
        status_code=status.HTTP_201_CREATED,
        tags=["drift"],
    )
    def create_drift_report(
        baseline_version_id: str,
        payload: DriftComparisonCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_drift_report(
            baseline_version_id=baseline_version_id,
            candidate_version_id=payload.candidate_version_id,
            actor=x_actor,
        )

    @app.get("/v1/drift-reports/{report_id}", tags=["drift"])
    def get_drift_report(report_id: str, request: Request) -> dict:
        return store(request).get_drift_report(report_id)

    @app.get("/v1/datasets/{dataset_id}/drift-reports", tags=["drift"])
    def list_drift_reports(
        dataset_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_drift_reports(dataset_id, limit=limit, offset=offset)
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.post(
        "/v1/datasets/{dataset_id}/contracts",
        status_code=status.HTTP_201_CREATED,
        tags=["contracts"],
    )
    def create_data_contract(
        dataset_id: str,
        payload: DataContractCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_data_contract(
            dataset_id=dataset_id, **payload.model_dump(), actor=x_actor
        )

    @app.get("/v1/datasets/{dataset_id}/contracts", tags=["contracts"])
    def list_data_contracts(
        dataset_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_data_contracts(
            dataset_id, limit=limit, offset=offset
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/contracts/{contract_id}", tags=["contracts"])
    def get_data_contract(contract_id: str, request: Request) -> dict:
        return store(request).get_data_contract(contract_id)

    @app.post(
        "/v1/contracts/{contract_id}/reports",
        status_code=status.HTTP_201_CREATED,
        tags=["contracts"],
    )
    def create_contract_report(
        contract_id: str,
        payload: ContractCheckCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_contract_report(
            contract_id=contract_id, version_id=payload.version_id, actor=x_actor
        )

    @app.get("/v1/contract-reports/{report_id}", tags=["contracts"])
    def get_contract_report(report_id: str, request: Request) -> dict:
        return store(request).get_contract_report(report_id)

    @app.get("/v1/datasets/{dataset_id}/contract-reports", tags=["contracts"])
    def list_contract_reports(
        dataset_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_contract_reports(
            dataset_id, limit=limit, offset=offset
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.post(
        "/v1/contract-compatibility-reports",
        status_code=status.HTTP_201_CREATED,
        tags=["contracts"],
    )
    def create_contract_compatibility_report(
        payload: ContractCompatibilityCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_contract_compatibility_report(
            **payload.model_dump(), actor=x_actor
        )

    @app.get(
        "/v1/contract-compatibility-reports/{report_id}", tags=["contracts"]
    )
    def get_contract_compatibility_report(report_id: str, request: Request) -> dict:
        return store(request).get_contract_compatibility_report(report_id)

    @app.get(
        "/v1/contracts/{baseline_contract_id}/compatibility-reports",
        tags=["contracts"],
    )
    def list_contract_compatibility_reports(
        baseline_contract_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_contract_compatibility_reports(
            baseline_contract_id, limit=limit, offset=offset
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.post(
        "/v1/provenance-closure-reports",
        status_code=status.HTTP_201_CREATED,
        tags=["provenance"],
    )
    def create_provenance_closure_report(
        payload: ProvenanceClosureCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_provenance_closure_report(
            version_ids=payload.version_ids, actor=x_actor
        )

    @app.get("/v1/provenance-closure-reports", tags=["provenance"])
    def list_provenance_closure_reports(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_provenance_closure_reports(
            limit=limit, offset=offset
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get(
        "/v1/provenance-closure-reports/{report_id}", tags=["provenance"]
    )
    def get_provenance_closure_report(report_id: str, request: Request) -> dict:
        return store(request).get_provenance_closure_report(report_id)

    @app.post(
        "/v1/provenance-impact-dossiers",
        status_code=status.HTTP_201_CREATED,
        tags=["provenance"],
    )
    def create_provenance_impact_dossier(
        payload: ProvenanceImpactDossierCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_provenance_impact_dossier(
            version_ids=payload.version_ids,
            dataset_ids=payload.dataset_ids,
            actor=x_actor,
        )

    @app.get("/v1/provenance-impact-dossiers", tags=["provenance"])
    def list_provenance_impact_dossiers(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_provenance_impact_dossiers(
            limit=limit, offset=offset
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/provenance-impact-dossiers/{dossier_id}", tags=["provenance"])
    def get_provenance_impact_dossier(dossier_id: str, request: Request) -> dict:
        return store(request).get_provenance_impact_dossier(dossier_id)

    @app.post(
        "/v1/lineage-evolution-dossiers",
        status_code=status.HTTP_201_CREATED,
        tags=["lineage"],
    )
    def create_lineage_evolution_dossier(
        payload: LineageEvolutionDossierCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_lineage_evolution_dossier(
            version_ids=payload.version_ids, actor=x_actor
        )

    @app.get("/v1/lineage-evolution-dossiers", tags=["lineage"])
    def list_lineage_evolution_dossiers(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_lineage_evolution_dossiers(limit=limit, offset=offset)
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/lineage-evolution-dossiers/{dossier_id}", tags=["lineage"])
    def get_lineage_evolution_dossier(dossier_id: str, request: Request) -> dict:
        return store(request).get_lineage_evolution_dossier(dossier_id)

    @app.post(
        "/v1/lineage-links",
        status_code=status.HTTP_201_CREATED,
        tags=["lineage"],
    )
    def create_lineage_link(
        payload: LineageLinkCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_lineage_link(
            **payload.model_dump(), actor=x_actor
        )

    @app.get("/v1/lineage-links", tags=["lineage"])
    def list_lineage_links(
        request: Request,
        upstream_version_id: Optional[str] = Query(default=None),
        downstream_version_id: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_lineage_links(
            upstream_version_id=upstream_version_id,
            downstream_version_id=downstream_version_id,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/lineage-links/{link_id}", tags=["lineage"])
    def get_lineage_link(link_id: str, request: Request) -> dict:
        return store(request).get_lineage_link(link_id)

    @app.post(
        "/v1/impact-reports",
        status_code=status.HTTP_201_CREATED,
        tags=["lineage"],
    )
    def create_impact_report(
        payload: ImpactReportCreate,
        request: Request,
        x_actor: str = Header(default="api", alias="X-Actor"),
    ) -> dict:
        return store(request).create_impact_report(
            **payload.model_dump(), actor=x_actor
        )

    @app.get("/v1/impact-reports/{report_id}", tags=["lineage"])
    def get_impact_report(report_id: str, request: Request) -> dict:
        return store(request).get_impact_report(report_id)

    @app.get(
        "/v1/versions/{changed_version_id}/impact-reports", tags=["lineage"]
    )
    def list_impact_reports(
        changed_version_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        items = store(request).list_impact_reports(
            changed_version_id, limit=limit, offset=offset
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @app.get("/v1/audit", tags=["audit"])
    def list_audit(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        action: Optional[str] = Query(default=None),
        resource_type: Optional[str] = Query(default=None),
        resource_id: Optional[str] = Query(default=None),
    ) -> dict:
        items = store(request).list_audit(
            limit=limit,
            offset=offset,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    return app


app = create_app()
