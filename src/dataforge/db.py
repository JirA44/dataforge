"""SQLite schema and connection management."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT,
    description TEXT,
    metadata_json TEXT NOT NULL,
    source_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    schema_json TEXT,
    dataset_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    previous_version_id TEXT REFERENCES dataset_versions(id),
    records_json TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK (record_count >= 0),
    content_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    provenance_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, version_number)
);

CREATE TABLE IF NOT EXISTS quality_evaluations (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('VERIFIED', 'REJECTED', 'INSUFFICIENT')),
    checks_json TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drift_reports (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    baseline_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    candidate_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('STABLE', 'DRIFTED', 'INSUFFICIENT')),
    metrics_json TEXT NOT NULL,
    report_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(baseline_version_id, candidate_version_id, rules_version)
);

CREATE TABLE IF NOT EXISTS data_contracts (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    previous_contract_id TEXT REFERENCES data_contracts(id),
    name TEXT NOT NULL,
    description TEXT,
    definition_json TEXT NOT NULL,
    contract_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, version_number)
);

CREATE TABLE IF NOT EXISTS contract_reports (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    contract_id TEXT NOT NULL REFERENCES data_contracts(id),
    version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('COMPATIBLE', 'VIOLATION', 'INSUFFICIENT')),
    violations_json TEXT NOT NULL,
    insufficient_reasons_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    report_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, version_id, rules_version)
);

CREATE TABLE IF NOT EXISTS lineage_links (
    id TEXT PRIMARY KEY,
    upstream_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    downstream_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'DERIVED_FROM', 'TRANSFORMED_FROM', 'FILTERED_FROM',
        'AGGREGATED_FROM', 'JOINED_FROM', 'COPIED_FROM'
    )),
    link_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (upstream_version_id <> downstream_version_id),
    UNIQUE(upstream_version_id, downstream_version_id, relation_type)
);

CREATE TABLE IF NOT EXISTS impact_reports (
    id TEXT PRIMARY KEY,
    changed_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    max_depth INTEGER NOT NULL CHECK (max_depth BETWEEN 1 AND 10),
    qualification TEXT NOT NULL CHECK (qualification IN (
        'ISOLATED', 'CONTAINED', 'PROPAGATED', 'CYCLE_DETECTED'
    )),
    paths_json TEXT NOT NULL,
    affected_versions_json TEXT NOT NULL,
    affected_datasets_json TEXT NOT NULL,
    cycle_paths_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    report_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(changed_version_id, max_depth, evidence_hash, rules_version)
);

CREATE TABLE IF NOT EXISTS contract_compatibility_reports (
    id TEXT PRIMARY KEY,
    baseline_contract_id TEXT NOT NULL REFERENCES data_contracts(id),
    candidate_contract_id TEXT NOT NULL REFERENCES data_contracts(id),
    qualification TEXT NOT NULL CHECK (qualification IN (
        'FULLY_COMPATIBLE', 'BACKWARD_COMPATIBLE', 'FORWARD_COMPATIBLE',
        'BREAKING', 'INSUFFICIENT'
    )),
    backward_json TEXT NOT NULL,
    forward_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    insufficient_reasons_json TEXT NOT NULL,
    baseline_snapshot_hash TEXT NOT NULL,
    candidate_snapshot_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    report_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(baseline_contract_id, candidate_contract_id, evidence_hash, rules_version)
);

CREATE TABLE IF NOT EXISTS provenance_closure_reports (
    id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    requested_version_ids_json TEXT NOT NULL,
    qualification TEXT NOT NULL CHECK (qualification IN (
        'COMPLETE', 'PARTIAL', 'BROKEN', 'INSUFFICIENT', 'INCOMPATIBLE'
    )),
    closure_version_ids_json TEXT NOT NULL,
    dataset_ids_json TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    lineage_link_ids_json TEXT NOT NULL,
    chains_json TEXT NOT NULL,
    missing_references_json TEXT NOT NULL,
    orphan_versions_json TEXT NOT NULL,
    cycles_json TEXT NOT NULL,
    breaks_json TEXT NOT NULL,
    unused_versions_json TEXT NOT NULL,
    integrity_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    report_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(request_hash, snapshot_hash, rules_version)
);

CREATE TABLE IF NOT EXISTS provenance_impact_dossiers (
    id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    selected_version_ids_json TEXT NOT NULL,
    selected_dataset_ids_json TEXT NOT NULL,
    seed_version_ids_json TEXT NOT NULL,
    qualification TEXT NOT NULL CHECK (qualification IN (
        'CONTAINED', 'WIDESPREAD', 'INSUFFICIENT', 'INCOMPATIBLE'
    )),
    affected_json TEXT NOT NULL,
    orphan_references_json TEXT NOT NULL,
    cycles_json TEXT NOT NULL,
    breaks_json TEXT NOT NULL,
    integrity_json TEXT NOT NULL,
    worst_branch_json TEXT,
    insufficient_reasons_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    dossier_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(request_hash, snapshot_hash, rules_version)
);

CREATE TABLE IF NOT EXISTS lineage_evolution_dossiers (
    id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    selected_version_ids_json TEXT NOT NULL,
    chronological_version_ids_json TEXT NOT NULL,
    dataset_id TEXT,
    qualification TEXT NOT NULL CHECK (qualification IN (
        'EXPLAINED', 'PARTIAL', 'INSUFFICIENT', 'INCOMPATIBLE'
    )),
    states_json TEXT NOT NULL,
    transitions_json TEXT NOT NULL,
    worst_transition_json TEXT NOT NULL,
    compatibility_issues_json TEXT NOT NULL,
    insufficient_reasons_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    dossier_hash TEXT NOT NULL UNIQUE,
    rules_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(request_hash, snapshot_hash, rules_version)
);

CREATE TABLE IF NOT EXISTS audit_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_dataset ON dataset_versions(dataset_id, version_number);
CREATE INDEX IF NOT EXISTS idx_quality_version ON quality_evaluations(version_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_drift_dataset ON drift_reports(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_contracts_dataset ON data_contracts(dataset_id, version_number);
CREATE INDEX IF NOT EXISTS idx_contract_reports_dataset ON contract_reports(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_lineage_upstream ON lineage_links(upstream_version_id, downstream_version_id);
CREATE INDEX IF NOT EXISTS idx_lineage_downstream ON lineage_links(downstream_version_id, upstream_version_id);
CREATE INDEX IF NOT EXISTS idx_impact_changed ON impact_reports(changed_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_compatibility_baseline ON contract_compatibility_reports(baseline_contract_id, created_at);
CREATE INDEX IF NOT EXISTS idx_compatibility_candidate ON contract_compatibility_reports(candidate_contract_id, created_at);
CREATE INDEX IF NOT EXISTS idx_provenance_closure_request ON provenance_closure_reports(request_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_provenance_impact_request ON provenance_impact_dossiers(request_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_lineage_evolution_request ON lineage_evolution_dossiers(request_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_log(resource_type, resource_id, sequence);

CREATE TRIGGER IF NOT EXISTS immutable_sources_update
BEFORE UPDATE ON sources BEGIN SELECT RAISE(ABORT, 'sources are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_sources_delete
BEFORE DELETE ON sources BEGIN SELECT RAISE(ABORT, 'sources are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_datasets_update
BEFORE UPDATE ON datasets BEGIN SELECT RAISE(ABORT, 'datasets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_datasets_delete
BEFORE DELETE ON datasets BEGIN SELECT RAISE(ABORT, 'datasets are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_versions_update
BEFORE UPDATE ON dataset_versions BEGIN SELECT RAISE(ABORT, 'dataset versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_versions_delete
BEFORE DELETE ON dataset_versions BEGIN SELECT RAISE(ABORT, 'dataset versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_quality_update
BEFORE UPDATE ON quality_evaluations BEGIN SELECT RAISE(ABORT, 'quality evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_quality_delete
BEFORE DELETE ON quality_evaluations BEGIN SELECT RAISE(ABORT, 'quality evaluations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_drift_update
BEFORE UPDATE ON drift_reports BEGIN SELECT RAISE(ABORT, 'drift reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_drift_delete
BEFORE DELETE ON drift_reports BEGIN SELECT RAISE(ABORT, 'drift reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_contracts_update
BEFORE UPDATE ON data_contracts BEGIN SELECT RAISE(ABORT, 'data contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_contracts_delete
BEFORE DELETE ON data_contracts BEGIN SELECT RAISE(ABORT, 'data contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_contract_reports_update
BEFORE UPDATE ON contract_reports BEGIN SELECT RAISE(ABORT, 'contract reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_contract_reports_delete
BEFORE DELETE ON contract_reports BEGIN SELECT RAISE(ABORT, 'contract reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_lineage_links_update
BEFORE UPDATE ON lineage_links BEGIN SELECT RAISE(ABORT, 'lineage links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_lineage_links_delete
BEFORE DELETE ON lineage_links BEGIN SELECT RAISE(ABORT, 'lineage links are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_impact_reports_update
BEFORE UPDATE ON impact_reports BEGIN SELECT RAISE(ABORT, 'impact reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_impact_reports_delete
BEFORE DELETE ON impact_reports BEGIN SELECT RAISE(ABORT, 'impact reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_contract_compatibility_update
BEFORE UPDATE ON contract_compatibility_reports BEGIN SELECT RAISE(ABORT, 'contract compatibility reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_contract_compatibility_delete
BEFORE DELETE ON contract_compatibility_reports BEGIN SELECT RAISE(ABORT, 'contract compatibility reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_provenance_closure_update
BEFORE UPDATE ON provenance_closure_reports BEGIN SELECT RAISE(ABORT, 'provenance closure reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_provenance_closure_delete
BEFORE DELETE ON provenance_closure_reports BEGIN SELECT RAISE(ABORT, 'provenance closure reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_provenance_impact_update
BEFORE UPDATE ON provenance_impact_dossiers BEGIN SELECT RAISE(ABORT, 'provenance impact dossiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_provenance_impact_delete
BEFORE DELETE ON provenance_impact_dossiers BEGIN SELECT RAISE(ABORT, 'provenance impact dossiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_lineage_evolution_update
BEFORE UPDATE ON lineage_evolution_dossiers BEGIN SELECT RAISE(ABORT, 'lineage evolution dossiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_lineage_evolution_delete
BEFORE DELETE ON lineage_evolution_dossiers BEGIN SELECT RAISE(ABORT, 'lineage evolution dossiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_update
BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_delete
BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


class SQLiteDatabase:
    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SQLITE_SCHEMA)
