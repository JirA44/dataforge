-- DataForge V1.07 PostgreSQL 14+ reference schema.
-- The application currently runs on SQLite; this DDL preserves its invariants.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE sources (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    kind text NOT NULL,
    uri text,
    description text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_hash char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL
);

CREATE TABLE datasets (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    description text,
    schema_json jsonb,
    dataset_hash char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL
);

CREATE TABLE dataset_versions (
    id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES datasets(id),
    source_id uuid NOT NULL REFERENCES sources(id),
    version_number integer NOT NULL CHECK (version_number > 0),
    previous_version_id uuid REFERENCES dataset_versions(id),
    records_json jsonb NOT NULL,
    record_count integer NOT NULL CHECK (record_count >= 0),
    content_hash char(64) NOT NULL,
    manifest_json jsonb NOT NULL,
    provenance_hash char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL,
    UNIQUE (dataset_id, version_number)
);

CREATE TABLE quality_evaluations (
    id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES dataset_versions(id),
    verdict text NOT NULL CHECK (verdict IN ('VERIFIED', 'REJECTED', 'INSUFFICIENT')),
    checks_json jsonb NOT NULL,
    rules_version text NOT NULL,
    evaluated_at timestamptz NOT NULL
);

CREATE TABLE drift_reports (
    id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES datasets(id),
    baseline_version_id uuid NOT NULL REFERENCES dataset_versions(id),
    candidate_version_id uuid NOT NULL REFERENCES dataset_versions(id),
    verdict text NOT NULL CHECK (verdict IN ('STABLE', 'DRIFTED', 'INSUFFICIENT')),
    metrics_json jsonb NOT NULL,
    report_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (baseline_version_id, candidate_version_id, rules_version)
);

CREATE TABLE data_contracts (
    id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES datasets(id),
    version_number integer NOT NULL CHECK (version_number > 0),
    previous_contract_id uuid REFERENCES data_contracts(id),
    name text NOT NULL,
    description text,
    definition_json jsonb NOT NULL,
    contract_hash char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL,
    UNIQUE (dataset_id, version_number)
);

CREATE TABLE contract_reports (
    id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES datasets(id),
    contract_id uuid NOT NULL REFERENCES data_contracts(id),
    version_id uuid NOT NULL REFERENCES dataset_versions(id),
    verdict text NOT NULL CHECK (verdict IN ('COMPATIBLE', 'VIOLATION', 'INSUFFICIENT')),
    violations_json jsonb NOT NULL,
    insufficient_reasons_json jsonb NOT NULL,
    metrics_json jsonb NOT NULL,
    report_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (contract_id, version_id, rules_version)
);

CREATE TABLE lineage_links (
    id uuid PRIMARY KEY,
    upstream_version_id uuid NOT NULL REFERENCES dataset_versions(id),
    downstream_version_id uuid NOT NULL REFERENCES dataset_versions(id),
    relation_type text NOT NULL CHECK (relation_type IN (
        'DERIVED_FROM', 'TRANSFORMED_FROM', 'FILTERED_FROM',
        'AGGREGATED_FROM', 'JOINED_FROM', 'COPIED_FROM'
    )),
    link_hash char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL,
    CHECK (upstream_version_id <> downstream_version_id),
    UNIQUE (upstream_version_id, downstream_version_id, relation_type)
);

CREATE TABLE impact_reports (
    id uuid PRIMARY KEY,
    changed_version_id uuid NOT NULL REFERENCES dataset_versions(id),
    max_depth integer NOT NULL CHECK (max_depth BETWEEN 1 AND 10),
    qualification text NOT NULL CHECK (qualification IN (
        'ISOLATED', 'CONTAINED', 'PROPAGATED', 'CYCLE_DETECTED'
    )),
    paths_json jsonb NOT NULL,
    affected_versions_json jsonb NOT NULL,
    affected_datasets_json jsonb NOT NULL,
    cycle_paths_json jsonb NOT NULL,
    summary_json jsonb NOT NULL,
    evidence_hash char(64) NOT NULL,
    report_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (changed_version_id, max_depth, evidence_hash, rules_version)
);

CREATE TABLE contract_compatibility_reports (
    id uuid PRIMARY KEY,
    baseline_contract_id uuid NOT NULL REFERENCES data_contracts(id),
    candidate_contract_id uuid NOT NULL REFERENCES data_contracts(id),
    qualification text NOT NULL CHECK (qualification IN (
        'FULLY_COMPATIBLE', 'BACKWARD_COMPATIBLE', 'FORWARD_COMPATIBLE',
        'BREAKING', 'INSUFFICIENT'
    )),
    backward_json jsonb NOT NULL,
    forward_json jsonb NOT NULL,
    changes_json jsonb NOT NULL,
    insufficient_reasons_json jsonb NOT NULL,
    baseline_snapshot_hash char(64) NOT NULL,
    candidate_snapshot_hash char(64) NOT NULL,
    evidence_hash char(64) NOT NULL,
    report_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (baseline_contract_id, candidate_contract_id, evidence_hash, rules_version)
);

CREATE TABLE provenance_closure_reports (
    id uuid PRIMARY KEY,
    request_hash char(64) NOT NULL,
    requested_version_ids_json jsonb NOT NULL,
    qualification text NOT NULL CHECK (qualification IN (
        'COMPLETE', 'PARTIAL', 'BROKEN', 'INSUFFICIENT', 'INCOMPATIBLE'
    )),
    closure_version_ids_json jsonb NOT NULL,
    dataset_ids_json jsonb NOT NULL,
    source_ids_json jsonb NOT NULL,
    lineage_link_ids_json jsonb NOT NULL,
    chains_json jsonb NOT NULL,
    missing_references_json jsonb NOT NULL,
    orphan_versions_json jsonb NOT NULL,
    cycles_json jsonb NOT NULL,
    breaks_json jsonb NOT NULL,
    unused_versions_json jsonb NOT NULL,
    integrity_json jsonb NOT NULL,
    summary_json jsonb NOT NULL,
    snapshot_hash char(64) NOT NULL,
    report_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (request_hash, snapshot_hash, rules_version)
);

CREATE TABLE provenance_impact_dossiers (
    id uuid PRIMARY KEY,
    request_hash char(64) NOT NULL,
    selected_version_ids_json jsonb NOT NULL,
    selected_dataset_ids_json jsonb NOT NULL,
    seed_version_ids_json jsonb NOT NULL,
    qualification text NOT NULL CHECK (qualification IN (
        'CONTAINED', 'WIDESPREAD', 'INSUFFICIENT', 'INCOMPATIBLE'
    )),
    affected_json jsonb NOT NULL,
    orphan_references_json jsonb NOT NULL,
    cycles_json jsonb NOT NULL,
    breaks_json jsonb NOT NULL,
    integrity_json jsonb NOT NULL,
    worst_branch_json jsonb,
    insufficient_reasons_json jsonb NOT NULL,
    summary_json jsonb NOT NULL,
    snapshot_hash char(64) NOT NULL,
    dossier_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE(request_hash, snapshot_hash, rules_version)
);

CREATE TABLE lineage_evolution_dossiers (
    id uuid PRIMARY KEY,
    request_hash char(64) NOT NULL,
    selected_version_ids_json jsonb NOT NULL,
    chronological_version_ids_json jsonb NOT NULL,
    dataset_id uuid,
    qualification text NOT NULL CHECK (qualification IN (
        'EXPLAINED', 'PARTIAL', 'INSUFFICIENT', 'INCOMPATIBLE'
    )),
    states_json jsonb NOT NULL,
    transitions_json jsonb NOT NULL,
    worst_transition_json jsonb NOT NULL,
    compatibility_issues_json jsonb NOT NULL,
    insufficient_reasons_json jsonb NOT NULL,
    summary_json jsonb NOT NULL,
    snapshot_hash char(64) NOT NULL,
    dossier_hash char(64) NOT NULL UNIQUE,
    rules_version text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE(request_hash, snapshot_hash, rules_version)
);

CREATE TABLE audit_log (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id uuid NOT NULL UNIQUE,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid NOT NULL,
    actor text NOT NULL,
    details_json jsonb NOT NULL,
    occurred_at timestamptz NOT NULL
);

CREATE INDEX idx_versions_dataset ON dataset_versions(dataset_id, version_number);
CREATE INDEX idx_quality_version ON quality_evaluations(version_id, evaluated_at);
CREATE INDEX idx_drift_dataset ON drift_reports(dataset_id, created_at);
CREATE INDEX idx_contracts_dataset ON data_contracts(dataset_id, version_number);
CREATE INDEX idx_contract_reports_dataset ON contract_reports(dataset_id, created_at);
CREATE INDEX idx_lineage_upstream ON lineage_links(upstream_version_id, downstream_version_id);
CREATE INDEX idx_lineage_downstream ON lineage_links(downstream_version_id, upstream_version_id);
CREATE INDEX idx_impact_changed ON impact_reports(changed_version_id, created_at);
CREATE INDEX idx_compatibility_baseline ON contract_compatibility_reports(baseline_contract_id, created_at);
CREATE INDEX idx_compatibility_candidate ON contract_compatibility_reports(candidate_contract_id, created_at);
CREATE INDEX idx_provenance_closure_request ON provenance_closure_reports(request_hash, created_at);
CREATE INDEX idx_provenance_impact_request ON provenance_impact_dossiers(request_hash, created_at);
CREATE INDEX idx_lineage_evolution_request ON lineage_evolution_dossiers(request_hash, created_at);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id, sequence);

CREATE OR REPLACE FUNCTION dataforge_reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable/append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER sources_immutable
BEFORE UPDATE OR DELETE ON sources FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER datasets_immutable
BEFORE UPDATE OR DELETE ON datasets FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER versions_immutable
BEFORE UPDATE OR DELETE ON dataset_versions FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER quality_immutable
BEFORE UPDATE OR DELETE ON quality_evaluations FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER drift_reports_immutable
BEFORE UPDATE OR DELETE ON drift_reports FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER data_contracts_immutable
BEFORE UPDATE OR DELETE ON data_contracts FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER contract_reports_immutable
BEFORE UPDATE OR DELETE ON contract_reports FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER lineage_links_immutable
BEFORE UPDATE OR DELETE ON lineage_links FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER impact_reports_immutable
BEFORE UPDATE OR DELETE ON impact_reports FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER contract_compatibility_reports_immutable
BEFORE UPDATE OR DELETE ON contract_compatibility_reports FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER provenance_closure_reports_immutable
BEFORE UPDATE OR DELETE ON provenance_closure_reports FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER provenance_impact_dossiers_immutable
BEFORE UPDATE OR DELETE ON provenance_impact_dossiers FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER lineage_evolution_dossiers_immutable
BEFORE UPDATE OR DELETE ON lineage_evolution_dossiers FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
CREATE TRIGGER audit_append_only
BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION dataforge_reject_mutation();
