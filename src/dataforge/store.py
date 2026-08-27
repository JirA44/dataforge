"""Transactional application service for DataForge."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from .contract_compatibility import COMPATIBILITY_RULES_VERSION, compare_contracts
from .contracts import CONTRACT_RULES_VERSION, evaluate_contract
from .db import SQLiteDatabase
from .drift import DRIFT_RULES_VERSION, compare_drift
from .errors import ConflictError, IntegrityError, NotFoundError, ValidationError
from .hashing import canonical_json, hash_json, sha256_text
from .lineage import LINEAGE_RULES_VERSION, RELATION_TYPES, analyze_downstream
from .lineage_evolution import LINEAGE_EVOLUTION_RULES_VERSION, build_lineage_evolution
from .provenance_closure import (
    PROVENANCE_CLOSURE_RULES_VERSION,
    build_provenance_closure,
)
from .provenance_impact import (
    PROVENANCE_IMPACT_RULES_VERSION,
    build_provenance_impact,
)
from .quality import QUALITY_RULES_VERSION, evaluate_quality

SUPPORTED_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
CONTRACT_TYPES = SUPPORTED_TYPES - {"null"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def _nonempty(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValidationError(f"{field} must not be empty")
    return normalized


def validate_schema_spec(schema_spec: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if schema_spec is None:
        return None
    if not isinstance(schema_spec, Mapping):
        raise ValidationError("schema must be an object")
    fields = schema_spec.get("fields", {})
    if not isinstance(fields, Mapping):
        raise ValidationError("schema.fields must be an object")
    normalized_fields: Dict[str, Dict[str, Any]] = {}
    for raw_name, raw_definition in fields.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValidationError("schema field names must be non-empty strings")
        if not isinstance(raw_definition, Mapping):
            raise ValidationError(f"schema field {raw_name!r} must be an object")
        expected = raw_definition.get("type")
        if expected not in SUPPORTED_TYPES:
            raise ValidationError(
                f"schema field {raw_name!r} has unsupported type {expected!r}"
            )
        normalized_fields[raw_name.strip()] = {
            "type": expected,
            "required": bool(raw_definition.get("required", True)),
        }
    unknown = set(schema_spec) - {"fields", "allow_extra"}
    if unknown:
        raise ValidationError(f"Unknown schema keys: {', '.join(sorted(unknown))}")
    return {
        "fields": dict(sorted(normalized_fields.items())),
        "allow_extra": bool(schema_spec.get("allow_extra", False)),
    }


def validate_contract_definition(definition: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize the already strict HTTP model for direct store callers too."""

    if not isinstance(definition, Mapping):
        raise ValidationError("contract definition must be an object")
    allowed = {
        "name", "description", "fields", "allow_extra", "min_rows", "max_rows",
        "max_duplicate_rate",
    }
    unknown = set(definition) - allowed
    if unknown:
        raise ValidationError(f"Unknown contract keys: {', '.join(sorted(unknown))}")
    raw_contract_name = definition.get("name")
    if not isinstance(raw_contract_name, str) or len(raw_contract_name) > 200:
        raise ValidationError("name must be a string between 1 and 200 characters")
    name = _nonempty(raw_contract_name, "name")
    description = definition.get("description")
    if description is not None and (
        not isinstance(description, str) or len(description) > 4000
    ):
        raise ValidationError("description must be null or a string up to 4000 characters")
    fields = definition.get("fields")
    if not isinstance(fields, Mapping) or not fields or len(fields) > 200:
        raise ValidationError("fields must contain between 1 and 200 field rules")
    normalized_fields: Dict[str, Dict[str, Any]] = {}
    for raw_name, raw_rule in fields.items():
        if (
            not isinstance(raw_name, str)
            or not raw_name.strip()
            or len(raw_name.strip()) > 200
        ):
            raise ValidationError("contract field names must be strings from 1 to 200 characters")
        field_name = raw_name.strip()
        if field_name in normalized_fields:
            raise ValidationError(f"Duplicate normalized contract field {field_name!r}")
        if not isinstance(raw_rule, Mapping):
            raise ValidationError(f"contract field {field_name!r} must be an object")
        rule_unknown = set(raw_rule) - {
            "types", "required", "nullable", "max_missing_rate", "unique"
        }
        if rule_unknown:
            raise ValidationError(
                f"Unknown keys for contract field {field_name!r}: "
                + ", ".join(sorted(rule_unknown))
            )
        types = raw_rule.get("types")
        if (
            not isinstance(types, Sequence)
            or isinstance(types, (str, bytes))
            or not types
        ):
            raise ValidationError(f"contract field {field_name!r}.types must not be empty")
        if len(types) > 6 or any(not isinstance(value, str) for value in types):
            raise ValidationError(f"contract field {field_name!r}.types is invalid")
        normalized_types = sorted(set(types))
        if any(value not in CONTRACT_TYPES for value in normalized_types):
            raise ValidationError(f"contract field {field_name!r} has unsupported types")
        missing_rate = raw_rule.get("max_missing_rate", 0.0)
        if isinstance(missing_rate, bool) or not isinstance(missing_rate, (int, float)):
            raise ValidationError("max_missing_rate must be a number")
        if not 0.0 <= float(missing_rate) <= 1.0:
            raise ValidationError("max_missing_rate must be between 0 and 1")
        for boolean_key, default in (
            ("required", True), ("nullable", False), ("unique", False)
        ):
            if not isinstance(raw_rule.get(boolean_key, default), bool):
                raise ValidationError(f"contract field {field_name!r}.{boolean_key} must be boolean")
        normalized_fields[field_name] = {
            "types": normalized_types,
            "required": raw_rule.get("required", True),
            "nullable": raw_rule.get("nullable", False),
            "max_missing_rate": float(missing_rate),
            "unique": raw_rule.get("unique", False),
        }
    min_rows = definition.get("min_rows", 1)
    max_rows = definition.get("max_rows")
    maximum_duplicate_rate = definition.get("max_duplicate_rate", 0.0)
    if isinstance(min_rows, bool) or not isinstance(min_rows, int) or not 0 <= min_rows <= 10_000_000:
        raise ValidationError("min_rows must be an integer between 0 and 10000000")
    if max_rows is not None and (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= 10_000_000
    ):
        raise ValidationError("max_rows must be null or an integer between 1 and 10000000")
    if max_rows is not None and max_rows < min_rows:
        raise ValidationError("max_rows must be greater than or equal to min_rows")
    if (
        isinstance(maximum_duplicate_rate, bool)
        or not isinstance(maximum_duplicate_rate, (int, float))
        or not 0.0 <= float(maximum_duplicate_rate) <= 1.0
    ):
        raise ValidationError("max_duplicate_rate must be between 0 and 1")
    if not isinstance(definition.get("allow_extra", False), bool):
        raise ValidationError("allow_extra must be boolean")
    return {
        "name": name,
        "description": description,
        "fields": dict(sorted(normalized_fields.items())),
        "allow_extra": definition.get("allow_extra", False),
        "min_rows": min_rows,
        "max_rows": max_rows,
        "max_duplicate_rate": float(maximum_duplicate_rate),
    }


class DataForgeStore:
    """Owns all state transitions and never exposes mutation of stored artifacts."""

    def __init__(self, database_path: Union[str, Path] = "data/dataforge.sqlite3"):
        self.db = SQLiteDatabase(database_path)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        action: str,
        resource_type: str,
        resource_id: str,
        actor: str,
        details: Mapping[str, Any],
        occurred_at: Optional[str] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log
              (id, action, resource_type, resource_id, actor, details_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                action,
                resource_type,
                resource_id,
                actor or "anonymous",
                canonical_json(details),
                occurred_at or utc_now(),
            ),
        )

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "uri": row["uri"],
            "description": row["description"],
            "metadata": json.loads(row["metadata_json"]),
            "source_hash": row["source_hash"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _dataset_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "schema": json.loads(row["schema_json"]) if row["schema_json"] else None,
            "dataset_hash": row["dataset_hash"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _quality_from_row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "id": row["id"],
            "version_id": row["version_id"],
            "verdict": row["verdict"],
            "checks": json.loads(row["checks_json"]),
            "rules_version": row["rules_version"],
            "evaluated_at": row["evaluated_at"],
        }

    @staticmethod
    def _drift_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "dataset_id": row["dataset_id"],
            "baseline_version_id": row["baseline_version_id"],
            "candidate_version_id": row["candidate_version_id"],
            "verdict": row["verdict"],
            "metrics": json.loads(row["metrics_json"]),
            "report_hash": row["report_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _contract_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "dataset_id": row["dataset_id"],
            "version_number": row["version_number"],
            "previous_contract_id": row["previous_contract_id"],
            "definition": json.loads(row["definition_json"]),
            "contract_hash": row["contract_hash"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _contract_report_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "dataset_id": row["dataset_id"],
            "contract_id": row["contract_id"],
            "version_id": row["version_id"],
            "verdict": row["verdict"],
            "violations": json.loads(row["violations_json"]),
            "insufficient_reasons": json.loads(row["insufficient_reasons_json"]),
            "metrics": json.loads(row["metrics_json"]),
            "report_hash": row["report_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _lineage_link_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "upstream_version_id": row["upstream_version_id"],
            "downstream_version_id": row["downstream_version_id"],
            "relation_type": row["relation_type"],
            "link_hash": row["link_hash"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _impact_report_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "changed_version_id": row["changed_version_id"],
            "max_depth": row["max_depth"],
            "qualification": row["qualification"],
            "paths": json.loads(row["paths_json"]),
            "affected_versions": json.loads(row["affected_versions_json"]),
            "affected_datasets": json.loads(row["affected_datasets_json"]),
            "cycle_paths": json.loads(row["cycle_paths_json"]),
            "summary": json.loads(row["summary_json"]),
            "evidence_hash": row["evidence_hash"],
            "report_hash": row["report_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _compatibility_report_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "baseline_contract_id": row["baseline_contract_id"],
            "candidate_contract_id": row["candidate_contract_id"],
            "qualification": row["qualification"],
            "backward": json.loads(row["backward_json"]),
            "forward": json.loads(row["forward_json"]),
            "changes": json.loads(row["changes_json"]),
            "insufficient_reasons": json.loads(row["insufficient_reasons_json"]),
            "baseline_snapshot_hash": row["baseline_snapshot_hash"],
            "candidate_snapshot_hash": row["candidate_snapshot_hash"],
            "evidence_hash": row["evidence_hash"],
            "report_hash": row["report_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _provenance_closure_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "requested_version_ids": json.loads(row["requested_version_ids_json"]),
            "qualification": row["qualification"],
            "closure_version_ids": json.loads(row["closure_version_ids_json"]),
            "dataset_ids": json.loads(row["dataset_ids_json"]),
            "source_ids": json.loads(row["source_ids_json"]),
            "lineage_link_ids": json.loads(row["lineage_link_ids_json"]),
            "chains": json.loads(row["chains_json"]),
            "missing_references": json.loads(row["missing_references_json"]),
            "orphan_versions": json.loads(row["orphan_versions_json"]),
            "cycles": json.loads(row["cycles_json"]),
            "breaks": json.loads(row["breaks_json"]),
            "unused_versions": json.loads(row["unused_versions_json"]),
            "integrity": json.loads(row["integrity_json"]),
            "summary": json.loads(row["summary_json"]),
            "snapshot_hash": row["snapshot_hash"],
            "report_hash": row["report_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _provenance_impact_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "selected_version_ids": json.loads(row["selected_version_ids_json"]),
            "selected_dataset_ids": json.loads(row["selected_dataset_ids_json"]),
            "seed_version_ids": json.loads(row["seed_version_ids_json"]),
            "qualification": row["qualification"],
            "affected": json.loads(row["affected_json"]),
            "orphan_references": json.loads(row["orphan_references_json"]),
            "cycles": json.loads(row["cycles_json"]),
            "breaks": json.loads(row["breaks_json"]),
            "integrity": json.loads(row["integrity_json"]),
            "worst_branch": json.loads(row["worst_branch_json"])
            if row["worst_branch_json"]
            else None,
            "insufficient_reasons": json.loads(row["insufficient_reasons_json"]),
            "summary": json.loads(row["summary_json"]),
            "snapshot_hash": row["snapshot_hash"],
            "dossier_hash": row["dossier_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _lineage_evolution_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "selected_version_ids": json.loads(row["selected_version_ids_json"]),
            "chronological_version_ids": json.loads(row["chronological_version_ids_json"]),
            "dataset_id": row["dataset_id"],
            "qualification": row["qualification"],
            "states": json.loads(row["states_json"]),
            "transitions": json.loads(row["transitions_json"]),
            "worst_transition": json.loads(row["worst_transition_json"]),
            "compatibility_issues": json.loads(row["compatibility_issues_json"]),
            "insufficient_reasons": json.loads(row["insufficient_reasons_json"]),
            "summary": json.loads(row["summary_json"]),
            "snapshot_hash": row["snapshot_hash"],
            "dossier_hash": row["dossier_hash"],
            "rules_version": row["rules_version"],
            "created_at": row["created_at"],
        }

    def _version_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        include_records: bool,
    ) -> Dict[str, Any]:
        quality_row = connection.execute(
            """
            SELECT * FROM quality_evaluations
            WHERE version_id = ?
            ORDER BY evaluated_at DESC, rowid DESC LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        value: Dict[str, Any] = {
            "id": row["id"],
            "dataset_id": row["dataset_id"],
            "source_id": row["source_id"],
            "version_number": row["version_number"],
            "previous_version_id": row["previous_version_id"],
            "record_count": row["record_count"],
            "content_hash": row["content_hash"],
            "provenance": json.loads(row["manifest_json"]),
            "provenance_hash": row["provenance_hash"],
            "created_at": row["created_at"],
            "quality": self._quality_from_row(quality_row),
        }
        if include_records:
            value["records"] = json.loads(row["records_json"])
        return value

    def create_source(
        self,
        *,
        name: str,
        kind: str,
        uri: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        source_id = new_id()
        created_at = utc_now()
        descriptor = {
            "name": _nonempty(name, "name"),
            "kind": _nonempty(kind, "kind"),
            "uri": uri,
            "description": description,
            "metadata": dict(metadata or {}),
        }
        metadata_json = canonical_json(descriptor["metadata"])
        source_hash = hash_json(descriptor)
        try:
            with self.db.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sources
                      (id, name, kind, uri, description, metadata_json, source_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        descriptor["name"],
                        descriptor["kind"],
                        uri,
                        description,
                        metadata_json,
                        source_hash,
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "SOURCE_CREATED",
                    "source",
                    source_id,
                    actor,
                    {"source_hash": source_hash},
                    created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("An identical source already exists") from exc
        return self.get_source(source_id)

    def list_sources(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources ORDER BY created_at, id LIMIT ? OFFSET ?",
                (min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._source_from_row(row) for row in rows]

    def get_source(self, source_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Source {source_id} was not found")
            return self._source_from_row(row)

    def create_dataset(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        schema_spec: Optional[Mapping[str, Any]] = None,
        actor: str = "system",
    ) -> Dict[str, Any]:
        dataset_id = new_id()
        created_at = utc_now()
        normalized_schema = validate_schema_spec(schema_spec)
        descriptor = {
            "name": _nonempty(name, "name"),
            "description": description,
            "schema": normalized_schema,
        }
        dataset_hash = hash_json(descriptor)
        try:
            with self.db.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO datasets
                      (id, name, description, schema_json, dataset_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dataset_id,
                        descriptor["name"],
                        description,
                        canonical_json(normalized_schema) if normalized_schema is not None else None,
                        dataset_hash,
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "DATASET_CREATED",
                    "dataset",
                    dataset_id,
                    actor,
                    {"dataset_hash": dataset_hash, "has_declared_schema": normalized_schema is not None},
                    created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Dataset name {name!r} already exists") from exc
        return self.get_dataset(dataset_id)

    def list_datasets(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM datasets ORDER BY created_at, id LIMIT ? OFFSET ?",
                (min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._dataset_from_row(row) for row in rows]

    def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            result = self._dataset_from_row(row)
            count = connection.execute(
                "SELECT COUNT(*) FROM dataset_versions WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()[0]
            result["version_count"] = count
            return result

    @staticmethod
    def _validate_records(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise ValidationError("records must be an array of objects")
        normalized = []
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise ValidationError(f"records[{index}] must be an object")
            normalized.append(dict(record))
        canonical_json(normalized)
        return normalized

    @staticmethod
    def _insert_evaluation(
        connection: sqlite3.Connection,
        version_id: str,
        result: Mapping[str, Any],
        evaluated_at: str,
    ) -> str:
        evaluation_id = new_id()
        connection.execute(
            """
            INSERT INTO quality_evaluations
              (id, version_id, verdict, checks_json, rules_version, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_id,
                version_id,
                result["verdict"],
                canonical_json(result["checks"]),
                result["rules_version"],
                evaluated_at,
            ),
        )
        return evaluation_id

    def create_version(
        self,
        *,
        dataset_id: str,
        source_id: str,
        records: Sequence[Mapping[str, Any]],
        actor: str = "system",
    ) -> Dict[str, Any]:
        normalized_records = self._validate_records(records)
        records_json = canonical_json(normalized_records)
        content_hash = sha256_text(records_json)
        version_id = new_id()
        created_at = utc_now()
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            dataset = connection.execute(
                "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone()
            if dataset is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            source = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if source is None:
                raise NotFoundError(f"Source {source_id} was not found")
            previous = connection.execute(
                """
                SELECT * FROM dataset_versions
                WHERE dataset_id = ? ORDER BY version_number DESC LIMIT 1
                """,
                (dataset_id,),
            ).fetchone()
            version_number = 1 if previous is None else previous["version_number"] + 1
            manifest = {
                "format": "dataforge.provenance/1.0",
                "version_id": version_id,
                "dataset_id": dataset_id,
                "dataset_hash": dataset["dataset_hash"],
                "source_id": source_id,
                "source_hash": source["source_hash"],
                "version_number": version_number,
                "record_count": len(normalized_records),
                "content_hash": content_hash,
                "previous_version_id": previous["id"] if previous else None,
                "previous_provenance_hash": previous["provenance_hash"] if previous else None,
                "created_at": created_at,
            }
            manifest_json = canonical_json(manifest)
            provenance_hash = sha256_text(manifest_json)
            connection.execute(
                """
                INSERT INTO dataset_versions
                  (id, dataset_id, source_id, version_number, previous_version_id,
                   records_json, record_count, content_hash, manifest_json,
                   provenance_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    dataset_id,
                    source_id,
                    version_number,
                    manifest["previous_version_id"],
                    records_json,
                    len(normalized_records),
                    content_hash,
                    manifest_json,
                    provenance_hash,
                    created_at,
                ),
            )
            schema_spec = json.loads(dataset["schema_json"]) if dataset["schema_json"] else None
            quality = evaluate_quality(normalized_records, schema_spec, provenance_valid=True)
            evaluation_id = self._insert_evaluation(connection, version_id, quality, created_at)
            self._audit(
                connection,
                "VERSION_CREATED",
                "dataset_version",
                version_id,
                actor,
                {
                    "dataset_id": dataset_id,
                    "source_id": source_id,
                    "version_number": version_number,
                    "content_hash": content_hash,
                    "provenance_hash": provenance_hash,
                    "record_count": len(normalized_records),
                },
                created_at,
            )
            self._audit(
                connection,
                "QUALITY_EVALUATED",
                "quality_evaluation",
                evaluation_id,
                actor,
                {"version_id": version_id, "verdict": quality["verdict"]},
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Version could not be created due to a concurrent conflict") from exc
        finally:
            connection.close()
        return self.get_version(version_id)

    def list_versions(
        self,
        dataset_id: str,
        limit: int = 100,
        offset: int = 0,
        include_records: bool = False,
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            if connection.execute("SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)).fetchone() is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            rows = connection.execute(
                """
                SELECT * FROM dataset_versions WHERE dataset_id = ?
                ORDER BY version_number DESC LIMIT ? OFFSET ?
                """,
                (dataset_id, min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._version_from_row(connection, row, include_records) for row in rows]

    def get_version(self, version_id: str, include_records: bool = True) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Dataset version {version_id} was not found")
            return self._version_from_row(connection, row, include_records)

    def verify_provenance(self, version_id: str) -> Dict[str, Any]:
        issues: List[str] = []
        with self.db.connect() as connection:
            target = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if target is None:
                raise NotFoundError(f"Dataset version {version_id} was not found")
            rows = connection.execute(
                """
                SELECT * FROM dataset_versions
                WHERE dataset_id = ? AND version_number <= ?
                ORDER BY version_number
                """,
                (target["dataset_id"], target["version_number"]),
            ).fetchall()
            previous = None
            for row in rows:
                try:
                    manifest = json.loads(row["manifest_json"])
                    records = json.loads(row["records_json"])
                except json.JSONDecodeError:
                    issues.append(f"Version {row['id']} contains invalid stored JSON")
                    previous = row
                    continue
                if sha256_text(row["records_json"]) != row["content_hash"]:
                    issues.append(f"Version {row['id']} content hash mismatch")
                if len(records) != row["record_count"]:
                    issues.append(f"Version {row['id']} record count mismatch")
                if sha256_text(row["manifest_json"]) != row["provenance_hash"]:
                    issues.append(f"Version {row['id']} provenance hash mismatch")
                expected_fields = {
                    "version_id": row["id"],
                    "dataset_id": row["dataset_id"],
                    "source_id": row["source_id"],
                    "version_number": row["version_number"],
                    "record_count": row["record_count"],
                    "content_hash": row["content_hash"],
                    "previous_version_id": previous["id"] if previous else None,
                    "previous_provenance_hash": previous["provenance_hash"] if previous else None,
                    "created_at": row["created_at"],
                }
                for key, expected in expected_fields.items():
                    if manifest.get(key) != expected:
                        issues.append(f"Version {row['id']} manifest field {key} mismatch")
                source = connection.execute(
                    "SELECT source_hash FROM sources WHERE id = ?", (row["source_id"],)
                ).fetchone()
                dataset = connection.execute(
                    "SELECT dataset_hash FROM datasets WHERE id = ?", (row["dataset_id"],)
                ).fetchone()
                if source is None or manifest.get("source_hash") != source["source_hash"]:
                    issues.append(f"Version {row['id']} source provenance mismatch")
                if dataset is None or manifest.get("dataset_hash") != dataset["dataset_hash"]:
                    issues.append(f"Version {row['id']} dataset provenance mismatch")
                previous = row
        return {
            "version_id": version_id,
            "valid": not issues,
            "checked_versions": len(rows),
            "issues": issues,
        }

    def run_quality_checks(self, version_id: str, actor: str = "system") -> Dict[str, Any]:
        integrity = self.verify_provenance(version_id)
        evaluated_at = utc_now()
        with self.db.connect() as connection:
            version = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (version_id,)
            ).fetchone()
            dataset = connection.execute(
                "SELECT * FROM datasets WHERE id = ?", (version["dataset_id"],)
            ).fetchone()
            records = json.loads(version["records_json"])
            schema_spec = json.loads(dataset["schema_json"]) if dataset["schema_json"] else None
            result = evaluate_quality(
                records,
                schema_spec,
                provenance_valid=integrity["valid"],
                provenance_reason=(
                    "Content and provenance chain hashes are valid"
                    if integrity["valid"]
                    else "; ".join(integrity["issues"])
                ),
            )
            evaluation_id = self._insert_evaluation(connection, version_id, result, evaluated_at)
            self._audit(
                connection,
                "QUALITY_EVALUATED",
                "quality_evaluation",
                evaluation_id,
                actor,
                {"version_id": version_id, "verdict": result["verdict"], "recheck": True},
                evaluated_at,
            )
        return self.get_quality_evaluation(evaluation_id)

    def get_quality_evaluation(self, evaluation_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM quality_evaluations WHERE id = ?", (evaluation_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Quality evaluation {evaluation_id} was not found")
            result = self._quality_from_row(row)
            if result is None:  # pragma: no cover - defensive only
                raise IntegrityError("Stored quality evaluation could not be decoded")
            return result

    def get_latest_quality(self, version_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM dataset_versions WHERE id = ?", (version_id,)
            ).fetchone() is None:
                raise NotFoundError(f"Dataset version {version_id} was not found")
            row = connection.execute(
                """
                SELECT * FROM quality_evaluations WHERE version_id = ?
                ORDER BY evaluated_at DESC, rowid DESC LIMIT 1
                """,
                (version_id,),
            ).fetchone()
            result = self._quality_from_row(row)
            if result is None:
                raise IntegrityError(f"Dataset version {version_id} has no quality evaluation")
            return result

    def create_drift_report(
        self,
        *,
        baseline_version_id: str,
        candidate_version_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Create once, then return the same immutable report for identical inputs."""

        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            baseline = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (baseline_version_id,)
            ).fetchone()
            if baseline is None:
                raise NotFoundError(f"Dataset version {baseline_version_id} was not found")
            candidate = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (candidate_version_id,)
            ).fetchone()
            if candidate is None:
                raise NotFoundError(f"Dataset version {candidate_version_id} was not found")
            if baseline["dataset_id"] != candidate["dataset_id"]:
                raise ValidationError("Drift comparison requires versions of the same dataset")
            if baseline["version_number"] >= candidate["version_number"]:
                raise ValidationError(
                    "baseline_version_id must precede candidate_version_id"
                )

            existing = connection.execute(
                """
                SELECT * FROM drift_reports
                WHERE baseline_version_id = ? AND candidate_version_id = ?
                  AND rules_version = ?
                """,
                (baseline_version_id, candidate_version_id, DRIFT_RULES_VERSION),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._drift_from_row(existing)

            result = compare_drift(
                json.loads(baseline["records_json"]),
                json.loads(candidate["records_json"]),
            )
            report_payload = {
                "format": "dataforge.drift/1.0",
                "dataset_id": baseline["dataset_id"],
                "baseline_version_id": baseline_version_id,
                "baseline_content_hash": baseline["content_hash"],
                "candidate_version_id": candidate_version_id,
                "candidate_content_hash": candidate["content_hash"],
                "verdict": result["verdict"],
                "metrics": result["metrics"],
                "rules_version": result["rules_version"],
            }
            report_hash = hash_json(report_payload)
            report_id = new_id()
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO drift_reports
                  (id, dataset_id, baseline_version_id, candidate_version_id,
                   verdict, metrics_json, report_hash, rules_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    baseline["dataset_id"],
                    baseline_version_id,
                    candidate_version_id,
                    result["verdict"],
                    canonical_json(result["metrics"]),
                    report_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "DRIFT_REPORT_CREATED",
                "drift_report",
                report_id,
                actor,
                {
                    "dataset_id": baseline["dataset_id"],
                    "baseline_version_id": baseline_version_id,
                    "candidate_version_id": candidate_version_id,
                    "verdict": result["verdict"],
                    "report_hash": report_hash,
                    "rules_version": result["rules_version"],
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Drift report could not be created") from exc
        finally:
            connection.close()
        return self.get_drift_report(report_id)

    def get_drift_report(self, report_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM drift_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Drift report {report_id} was not found")
            return self._drift_from_row(row)

    def list_drift_reports(
        self, dataset_id: str, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone() is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            rows = connection.execute(
                """
                SELECT * FROM drift_reports WHERE dataset_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (dataset_id, min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._drift_from_row(row) for row in rows]

    def create_data_contract(
        self,
        *,
        dataset_id: str,
        name: str,
        fields: Mapping[str, Mapping[str, Any]],
        description: Optional[str] = None,
        allow_extra: bool = False,
        min_rows: int = 1,
        max_rows: Optional[int] = None,
        max_duplicate_rate: float = 0.0,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Append the next immutable contract version for a dataset."""

        definition = validate_contract_definition(
            {
                "name": name,
                "description": description,
                "fields": fields,
                "allow_extra": allow_extra,
                "min_rows": min_rows,
                "max_rows": max_rows,
                "max_duplicate_rate": max_duplicate_rate,
            }
        )
        contract_id = new_id()
        created_at = utc_now()
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone() is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            previous = connection.execute(
                """
                SELECT * FROM data_contracts WHERE dataset_id = ?
                ORDER BY version_number DESC LIMIT 1
                """,
                (dataset_id,),
            ).fetchone()
            version_number = 1 if previous is None else previous["version_number"] + 1
            hash_payload = {
                "format": "dataforge.contract/1.0",
                "dataset_id": dataset_id,
                "version_number": version_number,
                "previous_contract_hash": previous["contract_hash"] if previous else None,
                "definition": definition,
            }
            contract_hash = hash_json(hash_payload)
            connection.execute(
                """
                INSERT INTO data_contracts
                  (id, dataset_id, version_number, previous_contract_id, name,
                   description, definition_json, contract_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    dataset_id,
                    version_number,
                    previous["id"] if previous else None,
                    definition["name"],
                    definition["description"],
                    canonical_json(definition),
                    contract_hash,
                    created_at,
                ),
            )
            self._audit(
                connection,
                "DATA_CONTRACT_CREATED",
                "data_contract",
                contract_id,
                actor,
                {
                    "dataset_id": dataset_id,
                    "version_number": version_number,
                    "contract_hash": contract_hash,
                    "previous_contract_id": previous["id"] if previous else None,
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Data contract could not be created") from exc
        finally:
            connection.close()
        return self.get_data_contract(contract_id)

    def get_data_contract(self, contract_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_contracts WHERE id = ?", (contract_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Data contract {contract_id} was not found")
            return self._contract_from_row(row)

    def list_data_contracts(
        self, dataset_id: str, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone() is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            rows = connection.execute(
                """
                SELECT * FROM data_contracts WHERE dataset_id = ?
                ORDER BY version_number DESC LIMIT ? OFFSET ?
                """,
                (dataset_id, min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._contract_from_row(row) for row in rows]

    def create_contract_report(
        self,
        *,
        contract_id: str,
        version_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Compute once and return the same report for identical immutable inputs."""

        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            contract = connection.execute(
                "SELECT * FROM data_contracts WHERE id = ?", (contract_id,)
            ).fetchone()
            if contract is None:
                raise NotFoundError(f"Data contract {contract_id} was not found")
            version = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if version is None:
                raise NotFoundError(f"Dataset version {version_id} was not found")
            if contract["dataset_id"] != version["dataset_id"]:
                raise ValidationError(
                    "Contract and dataset version must belong to the same dataset"
                )
            existing = connection.execute(
                """
                SELECT * FROM contract_reports
                WHERE contract_id = ? AND version_id = ? AND rules_version = ?
                """,
                (contract_id, version_id, CONTRACT_RULES_VERSION),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._contract_report_from_row(existing)

            result = evaluate_contract(
                json.loads(version["records_json"]),
                json.loads(contract["definition_json"]),
            )
            report_payload = {
                "format": "dataforge.contract-report/1.0",
                "dataset_id": version["dataset_id"],
                "contract_id": contract_id,
                "contract_hash": contract["contract_hash"],
                "version_id": version_id,
                "content_hash": version["content_hash"],
                "verdict": result["verdict"],
                "violations": result["violations"],
                "insufficient_reasons": result["insufficient_reasons"],
                "metrics": result["metrics"],
                "rules_version": result["rules_version"],
            }
            report_hash = hash_json(report_payload)
            report_id = new_id()
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO contract_reports
                  (id, dataset_id, contract_id, version_id, verdict, violations_json,
                   insufficient_reasons_json, metrics_json, report_hash, rules_version,
                   created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    version["dataset_id"],
                    contract_id,
                    version_id,
                    result["verdict"],
                    canonical_json(result["violations"]),
                    canonical_json(result["insufficient_reasons"]),
                    canonical_json(result["metrics"]),
                    report_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "CONTRACT_REPORT_CREATED",
                "contract_report",
                report_id,
                actor,
                {
                    "dataset_id": version["dataset_id"],
                    "contract_id": contract_id,
                    "version_id": version_id,
                    "verdict": result["verdict"],
                    "report_hash": report_hash,
                    "rules_version": result["rules_version"],
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Contract report could not be created") from exc
        finally:
            connection.close()
        return self.get_contract_report(report_id)

    def get_contract_report(self, report_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM contract_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Contract report {report_id} was not found")
            return self._contract_report_from_row(row)

    def list_contract_reports(
        self, dataset_id: str, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
            ).fetchone() is None:
                raise NotFoundError(f"Dataset {dataset_id} was not found")
            rows = connection.execute(
                """
                SELECT * FROM contract_reports WHERE dataset_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (dataset_id, min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._contract_report_from_row(row) for row in rows]

    def create_contract_compatibility_report(
        self,
        *,
        baseline_contract_id: str,
        candidate_contract_id: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Compare immutable snapshots once and return the same stored report."""

        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            baseline = connection.execute(
                "SELECT * FROM data_contracts WHERE id = ?", (baseline_contract_id,)
            ).fetchone()
            if baseline is None:
                raise NotFoundError(f"Data contract {baseline_contract_id} was not found")
            candidate = connection.execute(
                "SELECT * FROM data_contracts WHERE id = ?", (candidate_contract_id,)
            ).fetchone()
            if candidate is None:
                raise NotFoundError(f"Data contract {candidate_contract_id} was not found")

            baseline_definition = json.loads(baseline["definition_json"])
            candidate_definition = json.loads(candidate["definition_json"])
            baseline_snapshot = {
                "contract_id": baseline["id"],
                "dataset_id": baseline["dataset_id"],
                "version_number": baseline["version_number"],
                "contract_hash": baseline["contract_hash"],
                "definition": baseline_definition,
            }
            candidate_snapshot = {
                "contract_id": candidate["id"],
                "dataset_id": candidate["dataset_id"],
                "version_number": candidate["version_number"],
                "contract_hash": candidate["contract_hash"],
                "definition": candidate_definition,
            }
            baseline_snapshot_hash = hash_json(baseline_snapshot)
            candidate_snapshot_hash = hash_json(candidate_snapshot)
            evidence_hash = hash_json(
                {
                    "baseline_snapshot_hash": baseline_snapshot_hash,
                    "candidate_snapshot_hash": candidate_snapshot_hash,
                }
            )
            existing = connection.execute(
                """
                SELECT * FROM contract_compatibility_reports
                WHERE baseline_contract_id = ? AND candidate_contract_id = ?
                  AND evidence_hash = ? AND rules_version = ?
                """,
                (
                    baseline_contract_id,
                    candidate_contract_id,
                    evidence_hash,
                    COMPATIBILITY_RULES_VERSION,
                ),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._compatibility_report_from_row(existing)

            result = compare_contracts(
                baseline_definition,
                candidate_definition,
                same_dataset=baseline["dataset_id"] == candidate["dataset_id"],
            )
            report_payload = {
                "format": "dataforge.contract-compatibility/1.0",
                "baseline_contract_id": baseline_contract_id,
                "candidate_contract_id": candidate_contract_id,
                "qualification": result["qualification"],
                "backward": result["backward"],
                "forward": result["forward"],
                "changes": result["changes"],
                "insufficient_reasons": result["insufficient_reasons"],
                "baseline_snapshot_hash": baseline_snapshot_hash,
                "candidate_snapshot_hash": candidate_snapshot_hash,
                "evidence_hash": evidence_hash,
                "rules_version": result["rules_version"],
            }
            report_hash = hash_json(report_payload)
            report_id = new_id()
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO contract_compatibility_reports
                  (id, baseline_contract_id, candidate_contract_id, qualification,
                   backward_json, forward_json, changes_json, insufficient_reasons_json,
                   baseline_snapshot_hash, candidate_snapshot_hash, evidence_hash,
                   report_hash, rules_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    baseline_contract_id,
                    candidate_contract_id,
                    result["qualification"],
                    canonical_json(result["backward"]),
                    canonical_json(result["forward"]),
                    canonical_json(result["changes"]),
                    canonical_json(result["insufficient_reasons"]),
                    baseline_snapshot_hash,
                    candidate_snapshot_hash,
                    evidence_hash,
                    report_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "CONTRACT_COMPATIBILITY_REPORT_CREATED",
                "contract_compatibility_report",
                report_id,
                actor,
                {
                    "baseline_contract_id": baseline_contract_id,
                    "candidate_contract_id": candidate_contract_id,
                    "qualification": result["qualification"],
                    "baseline_snapshot_hash": baseline_snapshot_hash,
                    "candidate_snapshot_hash": candidate_snapshot_hash,
                    "report_hash": report_hash,
                    "rules_version": result["rules_version"],
                },
                created_at,
            )
            connection.commit()
        except NotFoundError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Contract compatibility report could not be created") from exc
        finally:
            connection.close()
        return self.get_contract_compatibility_report(report_id)

    def get_contract_compatibility_report(self, report_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM contract_compatibility_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    f"Contract compatibility report {report_id} was not found"
                )
            return self._compatibility_report_from_row(row)

    def list_contract_compatibility_reports(
        self, baseline_contract_id: str, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM data_contracts WHERE id = ?", (baseline_contract_id,)
            ).fetchone() is None:
                raise NotFoundError(f"Data contract {baseline_contract_id} was not found")
            rows = connection.execute(
                """
                SELECT * FROM contract_compatibility_reports
                WHERE baseline_contract_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (
                    baseline_contract_id,
                    min(max(limit, 1), 500),
                    max(offset, 0),
                ),
            ).fetchall()
            return [self._compatibility_report_from_row(row) for row in rows]

    def create_provenance_closure_report(
        self,
        *,
        version_ids: Sequence[str],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Reconstruct an order-independent immutable provenance dossier."""

        if (
            isinstance(version_ids, (str, bytes))
            or not isinstance(version_ids, Sequence)
            or not 1 <= len(version_ids) <= 50
            or any(not isinstance(value, str) or not value or len(value) > 100 for value in version_ids)
            or len(set(version_ids)) != len(version_ids)
        ):
            raise ValidationError("version_ids must contain 1 to 50 unique identifiers")
        normalized_ids = sorted(version_ids)
        request_hash = hash_json({"version_ids": normalized_ids})
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for version_id in normalized_ids:
                if connection.execute(
                    "SELECT 1 FROM dataset_versions WHERE id = ?", (version_id,)
                ).fetchone() is None:
                    raise NotFoundError(f"Dataset version {version_id} was not found")

            versions = {
                row["id"]: dict(row)
                for row in connection.execute("SELECT * FROM dataset_versions").fetchall()
            }
            datasets = {
                row["id"]: dict(row)
                for row in connection.execute("SELECT * FROM datasets").fetchall()
            }
            sources = {
                row["id"]: dict(row)
                for row in connection.execute("SELECT * FROM sources").fetchall()
            }
            lineage_links = [
                self._lineage_link_from_row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM lineage_links
                    ORDER BY upstream_version_id, downstream_version_id, relation_type, id
                    """
                ).fetchall()
            ]
            result = build_provenance_closure(
                requested_version_ids=normalized_ids,
                versions=versions,
                datasets=datasets,
                sources=sources,
                lineage_links=lineage_links,
            )
            existing = connection.execute(
                """
                SELECT * FROM provenance_closure_reports
                WHERE request_hash = ? AND snapshot_hash = ? AND rules_version = ?
                """,
                (
                    request_hash,
                    result["snapshot_hash"],
                    PROVENANCE_CLOSURE_RULES_VERSION,
                ),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._provenance_closure_from_row(existing)

            report_payload = {
                "format": "dataforge.provenance-closure/1.0",
                **result,
            }
            report_hash = hash_json(report_payload)
            report_id = new_id()
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO provenance_closure_reports
                  (id, request_hash, requested_version_ids_json, qualification,
                   closure_version_ids_json, dataset_ids_json, source_ids_json,
                   lineage_link_ids_json, chains_json, missing_references_json,
                   orphan_versions_json, cycles_json, breaks_json, unused_versions_json,
                   integrity_json, summary_json, snapshot_hash, report_hash,
                   rules_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    request_hash,
                    canonical_json(result["requested_version_ids"]),
                    result["qualification"],
                    canonical_json(result["closure_version_ids"]),
                    canonical_json(result["dataset_ids"]),
                    canonical_json(result["source_ids"]),
                    canonical_json(result["lineage_link_ids"]),
                    canonical_json(result["chains"]),
                    canonical_json(result["missing_references"]),
                    canonical_json(result["orphan_versions"]),
                    canonical_json(result["cycles"]),
                    canonical_json(result["breaks"]),
                    canonical_json(result["unused_versions"]),
                    canonical_json(result["integrity"]),
                    canonical_json(result["summary"]),
                    result["snapshot_hash"],
                    report_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "PROVENANCE_CLOSURE_REPORT_CREATED",
                "provenance_closure_report",
                report_id,
                actor,
                {
                    "requested_version_ids": result["requested_version_ids"],
                    "qualification": result["qualification"],
                    "snapshot_hash": result["snapshot_hash"],
                    "report_hash": report_hash,
                    "rules_version": result["rules_version"],
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Provenance closure report could not be created") from exc
        finally:
            connection.close()
        return self.get_provenance_closure_report(report_id)

    def get_provenance_closure_report(self, report_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provenance_closure_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Provenance closure report {report_id} was not found")
            return self._provenance_closure_from_row(row)

    def list_provenance_closure_reports(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM provenance_closure_reports
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._provenance_closure_from_row(row) for row in rows]

    def create_provenance_impact_dossier(
        self,
        *,
        version_ids: Sequence[str],
        dataset_ids: Sequence[str],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Verify evidence and compute deterministic downstream provenance impact."""

        for name, values in (("version_ids", version_ids), ("dataset_ids", dataset_ids)):
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise ValidationError(f"{name} must be an array")
            if len(set(values)) != len(values) or any(
                not isinstance(value, str) or not value or len(value) > 100 for value in values
            ):
                raise ValidationError(f"{name} must contain unique identifiers")
        if not 1 <= len(version_ids) + len(dataset_ids) <= 50:
            raise ValidationError("select between 1 and 50 version/dataset identifiers")
        selected_versions = sorted(version_ids)
        selected_datasets = sorted(dataset_ids)
        request_hash = hash_json(
            {"version_ids": selected_versions, "dataset_ids": selected_datasets}
        )
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for version_id in selected_versions:
                if connection.execute(
                    "SELECT 1 FROM dataset_versions WHERE id=?", (version_id,)
                ).fetchone() is None:
                    raise NotFoundError(f"Dataset version {version_id} was not found")
            for dataset_id in selected_datasets:
                if connection.execute(
                    "SELECT 1 FROM datasets WHERE id=?", (dataset_id,)
                ).fetchone() is None:
                    raise NotFoundError(f"Dataset {dataset_id} was not found")
            versions = {
                row["id"]: dict(row)
                for row in connection.execute("SELECT * FROM dataset_versions").fetchall()
            }
            datasets = {
                row["id"]: dict(row)
                for row in connection.execute("SELECT * FROM datasets").fetchall()
            }
            sources = {
                row["id"]: dict(row)
                for row in connection.execute("SELECT * FROM sources").fetchall()
            }
            lineage_links = [
                self._lineage_link_from_row(row)
                for row in connection.execute(
                    """SELECT * FROM lineage_links
                       ORDER BY upstream_version_id,downstream_version_id,relation_type,id"""
                ).fetchall()
            ]
            result = build_provenance_impact(
                selected_version_ids=selected_versions,
                selected_dataset_ids=selected_datasets,
                versions=versions,
                datasets=datasets,
                sources=sources,
                lineage_links=lineage_links,
            )
            existing = connection.execute(
                """SELECT * FROM provenance_impact_dossiers
                   WHERE request_hash=? AND snapshot_hash=? AND rules_version=?""",
                (request_hash, result["snapshot_hash"], PROVENANCE_IMPACT_RULES_VERSION),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._provenance_impact_from_row(existing)
            dossier_hash = hash_json(
                {"format": "dataforge.provenance-impact/1.0", **result}
            )
            dossier_id = new_id()
            created_at = utc_now()
            connection.execute(
                """INSERT INTO provenance_impact_dossiers
                   (id,request_hash,selected_version_ids_json,selected_dataset_ids_json,
                    seed_version_ids_json,qualification,affected_json,
                    orphan_references_json,cycles_json,breaks_json,integrity_json,
                    worst_branch_json,insufficient_reasons_json,summary_json,
                    snapshot_hash,dossier_hash,rules_version,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dossier_id,
                    request_hash,
                    canonical_json(result["selected_version_ids"]),
                    canonical_json(result["selected_dataset_ids"]),
                    canonical_json(result["seed_version_ids"]),
                    result["qualification"],
                    canonical_json(result["affected"]),
                    canonical_json(result["orphan_references"]),
                    canonical_json(result["cycles"]),
                    canonical_json(result["breaks"]),
                    canonical_json(result["integrity"]),
                    canonical_json(result["worst_branch"])
                    if result["worst_branch"] is not None
                    else None,
                    canonical_json(result["insufficient_reasons"]),
                    canonical_json(result["summary"]),
                    result["snapshot_hash"],
                    dossier_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "PROVENANCE_IMPACT_DOSSIER_CREATED",
                "provenance_impact_dossier",
                dossier_id,
                actor,
                {
                    "qualification": result["qualification"],
                    "selected_version_ids": selected_versions,
                    "selected_dataset_ids": selected_datasets,
                    "snapshot_hash": result["snapshot_hash"],
                    "dossier_hash": dossier_hash,
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Provenance impact dossier could not be created") from exc
        finally:
            connection.close()
        return self.get_provenance_impact_dossier(dossier_id)

    def get_provenance_impact_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provenance_impact_dossiers WHERE id=?", (dossier_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Provenance impact dossier {dossier_id} was not found")
            return self._provenance_impact_from_row(row)

    def list_provenance_impact_dossiers(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM provenance_impact_dossiers
                   ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?""",
                (min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._provenance_impact_from_row(row) for row in rows]

    def create_lineage_evolution_dossier(
        self, *, version_ids: Sequence[str], actor: str = "system"
    ) -> Dict[str, Any]:
        if (
            isinstance(version_ids, (str, bytes))
            or not isinstance(version_ids, Sequence)
            or not 2 <= len(version_ids) <= 100
            or len(set(version_ids)) != len(version_ids)
            or any(not isinstance(item, str) or not item or len(item) > 100 for item in version_ids)
        ):
            raise ValidationError("version_ids must contain 2 to 100 unique identifiers")
        selected_ids = sorted(version_ids)
        request_hash = hash_json({"version_ids": selected_ids})
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for version_id in selected_ids:
                if connection.execute(
                    "SELECT 1 FROM dataset_versions WHERE id=?", (version_id,)
                ).fetchone() is None:
                    raise NotFoundError(f"Dataset version {version_id} was not found")
            versions = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM dataset_versions").fetchall()}
            datasets = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM datasets").fetchall()}
            sources = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM sources").fetchall()}
            lineage_links = [
                self._lineage_link_from_row(row)
                for row in connection.execute(
                    """SELECT * FROM lineage_links
                       ORDER BY upstream_version_id,downstream_version_id,relation_type,id"""
                ).fetchall()
            ]
            result = build_lineage_evolution(
                selected_version_ids=selected_ids,
                versions=versions,
                datasets=datasets,
                sources=sources,
                lineage_links=lineage_links,
            )
            existing = connection.execute(
                """SELECT * FROM lineage_evolution_dossiers
                   WHERE request_hash=? AND snapshot_hash=? AND rules_version=?""",
                (request_hash, result["snapshot_hash"], LINEAGE_EVOLUTION_RULES_VERSION),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._lineage_evolution_from_row(existing)
            dossier_hash = hash_json({"format": "dataforge.lineage-evolution/1.0", **result})
            dossier_id = new_id()
            created_at = utc_now()
            connection.execute(
                """INSERT INTO lineage_evolution_dossiers
                   (id,request_hash,selected_version_ids_json,chronological_version_ids_json,
                    dataset_id,qualification,states_json,transitions_json,worst_transition_json,
                    compatibility_issues_json,insufficient_reasons_json,summary_json,
                    snapshot_hash,dossier_hash,rules_version,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dossier_id,
                    request_hash,
                    canonical_json(result["selected_version_ids"]),
                    canonical_json(result["chronological_version_ids"]),
                    result["dataset_id"],
                    result["qualification"],
                    canonical_json(result["states"]),
                    canonical_json(result["transitions"]),
                    canonical_json(result["worst_transition"]),
                    canonical_json(result["compatibility_issues"]),
                    canonical_json(result["insufficient_reasons"]),
                    canonical_json(result["summary"]),
                    result["snapshot_hash"],
                    dossier_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "LINEAGE_EVOLUTION_DOSSIER_CREATED",
                "lineage_evolution_dossier",
                dossier_id,
                actor,
                {"qualification": result["qualification"], "snapshot_hash": result["snapshot_hash"], "dossier_hash": dossier_hash},
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Lineage evolution dossier could not be created") from exc
        finally:
            connection.close()
        return self.get_lineage_evolution_dossier(dossier_id)

    def get_lineage_evolution_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM lineage_evolution_dossiers WHERE id=?", (dossier_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Lineage evolution dossier {dossier_id} was not found")
            return self._lineage_evolution_from_row(row)

    def list_lineage_evolution_dossiers(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM lineage_evolution_dossiers
                   ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?""",
                (min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._lineage_evolution_from_row(row) for row in rows]

    def create_lineage_link(
        self,
        *,
        upstream_version_id: str,
        downstream_version_id: str,
        relation_type: str,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Create once, then return the same immutable directed lineage link."""

        if upstream_version_id == downstream_version_id:
            raise ValidationError("A dataset version cannot link to itself")
        if relation_type not in RELATION_TYPES:
            raise ValidationError(
                "relation_type must be one of: " + ", ".join(RELATION_TYPES)
            )
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            upstream = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (upstream_version_id,)
            ).fetchone()
            if upstream is None:
                raise NotFoundError(
                    f"Dataset version {upstream_version_id} was not found"
                )
            downstream = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (downstream_version_id,)
            ).fetchone()
            if downstream is None:
                raise NotFoundError(
                    f"Dataset version {downstream_version_id} was not found"
                )
            existing = connection.execute(
                """
                SELECT * FROM lineage_links
                WHERE upstream_version_id = ? AND downstream_version_id = ?
                  AND relation_type = ?
                """,
                (upstream_version_id, downstream_version_id, relation_type),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._lineage_link_from_row(existing)

            link_payload = {
                "format": "dataforge.lineage-link/1.0",
                "upstream_version_id": upstream_version_id,
                "upstream_content_hash": upstream["content_hash"],
                "downstream_version_id": downstream_version_id,
                "downstream_content_hash": downstream["content_hash"],
                "relation_type": relation_type,
                "rules_version": LINEAGE_RULES_VERSION,
            }
            link_hash = hash_json(link_payload)
            link_id = new_id()
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO lineage_links
                  (id, upstream_version_id, downstream_version_id, relation_type,
                   link_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    upstream_version_id,
                    downstream_version_id,
                    relation_type,
                    link_hash,
                    created_at,
                ),
            )
            self._audit(
                connection,
                "LINEAGE_LINK_CREATED",
                "lineage_link",
                link_id,
                actor,
                {
                    "upstream_version_id": upstream_version_id,
                    "downstream_version_id": downstream_version_id,
                    "relation_type": relation_type,
                    "link_hash": link_hash,
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Lineage link could not be created") from exc
        finally:
            connection.close()
        return self.get_lineage_link(link_id)

    def get_lineage_link(self, link_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM lineage_links WHERE id = ?", (link_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Lineage link {link_id} was not found")
            return self._lineage_link_from_row(row)

    def list_lineage_links(
        self,
        *,
        upstream_version_id: Optional[str] = None,
        downstream_version_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        parameters: List[Any] = []
        if upstream_version_id is not None:
            clauses.append("upstream_version_id = ?")
            parameters.append(upstream_version_id)
        if downstream_version_id is not None:
            clauses.append("downstream_version_id = ?")
            parameters.append(downstream_version_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.extend([min(max(limit, 1), 500), max(offset, 0)])
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM lineage_links{where} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return [self._lineage_link_from_row(row) for row in rows]

    @staticmethod
    def _available_contract_compliance(
        connection: sqlite3.Connection, version_id: str
    ) -> Dict[str, Any]:
        row = connection.execute(
            """
            SELECT cr.id AS report_id, cr.contract_id, cr.verdict, cr.report_hash,
                   cr.rules_version, dc.version_number AS contract_version
            FROM contract_reports cr
            JOIN data_contracts dc ON dc.id = cr.contract_id
            WHERE cr.version_id = ?
            ORDER BY dc.version_number DESC, cr.created_at DESC, cr.rowid DESC
            LIMIT 1
            """,
            (version_id,),
        ).fetchone()
        if row is None:
            return {"status": "NOT_AVAILABLE"}
        return {
            "status": "AVAILABLE",
            "verdict": row["verdict"],
            "contract_id": row["contract_id"],
            "contract_version": row["contract_version"],
            "contract_report_id": row["report_id"],
            "contract_report_hash": row["report_hash"],
            "contract_rules_version": row["rules_version"],
        }

    def create_impact_report(
        self,
        *,
        changed_version_id: str,
        max_depth: int = 3,
        actor: str = "system",
    ) -> Dict[str, Any]:
        """Create an immutable report for the current lineage/compliance evidence."""

        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= 10:
            raise ValidationError("max_depth must be an integer between 1 and 10")
        connection = self.db.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "SELECT * FROM dataset_versions WHERE id = ?", (changed_version_id,)
            ).fetchone()
            if changed is None:
                raise NotFoundError(
                    f"Dataset version {changed_version_id} was not found"
                )
            link_rows = connection.execute(
                """
                SELECT * FROM lineage_links
                ORDER BY upstream_version_id, downstream_version_id, relation_type, id
                """
            ).fetchall()
            links = [self._lineage_link_from_row(row) for row in link_rows]
            version_rows = connection.execute("SELECT * FROM dataset_versions").fetchall()
            versions = {
                row["id"]: {
                    "dataset_id": row["dataset_id"],
                    "version_number": row["version_number"],
                    "content_hash": row["content_hash"],
                }
                for row in version_rows
            }
            compliance = {
                version_id: self._available_contract_compliance(connection, version_id)
                for version_id in versions
            }
            result = analyze_downstream(
                changed_version_id=changed_version_id,
                max_depth=max_depth,
                links=links,
                versions=versions,
                compliance=compliance,
            )
            lineage_graph_hash = hash_json(
                {"link_hashes": sorted(link["link_hash"] for link in links)}
            )
            compliance_evidence = [
                {
                    "version_id": item["version_id"],
                    "contract_compliance": item["contract_compliance"],
                }
                for item in result["affected_versions"]
            ]
            evidence_hash = hash_json(
                {
                    "lineage_graph_hash": lineage_graph_hash,
                    "contract_compliance": compliance_evidence,
                }
            )
            existing = connection.execute(
                """
                SELECT * FROM impact_reports
                WHERE changed_version_id = ? AND max_depth = ?
                  AND evidence_hash = ? AND rules_version = ?
                """,
                (changed_version_id, max_depth, evidence_hash, LINEAGE_RULES_VERSION),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._impact_report_from_row(existing)

            report_payload = {
                "format": "dataforge.impact-report/1.0",
                "changed_version_id": changed_version_id,
                "changed_content_hash": changed["content_hash"],
                "max_depth": max_depth,
                "qualification": result["qualification"],
                "paths": result["paths"],
                "affected_versions": result["affected_versions"],
                "affected_datasets": result["affected_datasets"],
                "cycle_paths": result["cycle_paths"],
                "summary": result["summary"],
                "evidence_hash": evidence_hash,
                "rules_version": result["rules_version"],
            }
            report_hash = hash_json(report_payload)
            report_id = new_id()
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO impact_reports
                  (id, changed_version_id, max_depth, qualification, paths_json,
                   affected_versions_json, affected_datasets_json, cycle_paths_json,
                   summary_json, evidence_hash, report_hash, rules_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    changed_version_id,
                    max_depth,
                    result["qualification"],
                    canonical_json(result["paths"]),
                    canonical_json(result["affected_versions"]),
                    canonical_json(result["affected_datasets"]),
                    canonical_json(result["cycle_paths"]),
                    canonical_json(result["summary"]),
                    evidence_hash,
                    report_hash,
                    result["rules_version"],
                    created_at,
                ),
            )
            self._audit(
                connection,
                "IMPACT_REPORT_CREATED",
                "impact_report",
                report_id,
                actor,
                {
                    "changed_version_id": changed_version_id,
                    "max_depth": max_depth,
                    "qualification": result["qualification"],
                    "affected_version_count": result["summary"]["affected_version_count"],
                    "evidence_hash": evidence_hash,
                    "report_hash": report_hash,
                    "rules_version": result["rules_version"],
                },
                created_at,
            )
            connection.commit()
        except (NotFoundError, ValidationError):
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ConflictError("Impact report could not be created") from exc
        finally:
            connection.close()
        return self.get_impact_report(report_id)

    def get_impact_report(self, report_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM impact_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Impact report {report_id} was not found")
            return self._impact_report_from_row(row)

    def list_impact_reports(
        self, changed_version_id: str, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM dataset_versions WHERE id = ?", (changed_version_id,)
            ).fetchone() is None:
                raise NotFoundError(
                    f"Dataset version {changed_version_id} was not found"
                )
            rows = connection.execute(
                """
                SELECT * FROM impact_reports WHERE changed_version_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (
                    changed_version_id,
                    min(max(limit, 1), 500),
                    max(offset, 0),
                ),
            ).fetchall()
            return [self._impact_report_from_row(row) for row in rows]

    def list_audit(
        self,
        limit: int = 100,
        offset: int = 0,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        parameters: List[Any] = []
        for column, value in (
            ("action", action),
            ("resource_type", resource_type),
            ("resource_id", resource_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.extend([min(max(limit, 1), 500), max(offset, 0)])
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_log{where} ORDER BY sequence DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "id": row["id"],
                    "action": row["action"],
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "actor": row["actor"],
                    "details": json.loads(row["details_json"]),
                    "occurred_at": row["occurred_at"],
                }
                for row in rows
            ]
