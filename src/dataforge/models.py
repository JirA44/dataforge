"""Pydantic input contracts used by the FastAPI adapter."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FieldDefinition(StrictModel):
    type: Literal["string", "integer", "number", "boolean", "object", "array", "null"]
    required: bool = True


class DatasetSchema(StrictModel):
    fields: Dict[str, FieldDefinition] = Field(default_factory=dict)
    allow_extra: bool = False


class SourceCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=100)
    uri: Optional[str] = Field(default=None, max_length=2048)
    description: Optional[str] = Field(default=None, max_length=4000)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DatasetCreate(StrictModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, populate_by_name=True
    )

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    schema_spec: Optional[DatasetSchema] = Field(default=None, alias="schema")


class VersionCreate(StrictModel):
    source_id: str = Field(min_length=1, max_length=100)
    records: List[Dict[str, Any]]


class DriftComparisonCreate(StrictModel):
    candidate_version_id: str = Field(min_length=1, max_length=100)


ObservableType = Literal["string", "integer", "number", "boolean", "object", "array"]


class ContractField(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    types: List[ObservableType] = Field(min_length=1, max_length=6)
    required: bool = True
    nullable: bool = False
    max_missing_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    unique: bool = False


class DataContractCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    fields: Dict[str, ContractField] = Field(min_length=1, max_length=200)
    allow_extra: bool = False
    min_rows: int = Field(default=1, ge=0, le=10_000_000)
    max_rows: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    max_duplicate_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class ContractCheckCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    version_id: str = Field(min_length=1, max_length=100)


LineageRelationType = Literal[
    "DERIVED_FROM",
    "TRANSFORMED_FROM",
    "FILTERED_FROM",
    "AGGREGATED_FROM",
    "JOINED_FROM",
    "COPIED_FROM",
]


class LineageLinkCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    upstream_version_id: str = Field(min_length=1, max_length=100)
    downstream_version_id: str = Field(min_length=1, max_length=100)
    relation_type: LineageRelationType


class ImpactReportCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    changed_version_id: str = Field(min_length=1, max_length=100)
    max_depth: int = Field(default=3, ge=1, le=10)


class ContractCompatibilityCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    baseline_contract_id: str = Field(min_length=1, max_length=100)
    candidate_contract_id: str = Field(min_length=1, max_length=100)


class ProvenanceClosureCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    version_ids: List[str] = Field(min_length=1, max_length=50)

    @field_validator("version_ids")
    @classmethod
    def validate_version_ids(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("each version_id must contain between 1 and 100 characters")
        if len(set(values)) != len(values):
            raise ValueError("version_ids must be unique")
        return values


class ProvenanceImpactDossierCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    version_ids: List[str] = Field(default_factory=list, max_length=50)
    dataset_ids: List[str] = Field(default_factory=list, max_length=50)

    @field_validator("version_ids", "dataset_ids")
    @classmethod
    def validate_identifier_list(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("identifiers must contain between 1 and 100 characters")
        if len(set(values)) != len(values):
            raise ValueError("identifiers must be unique within each list")
        return values

    @model_validator(mode="after")
    def validate_selection(self):
        total = len(self.version_ids) + len(self.dataset_ids)
        if not 1 <= total <= 50:
            raise ValueError("select between 1 and 50 version/dataset identifiers")
        return self


class LineageEvolutionDossierCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    version_ids: List[str] = Field(min_length=2, max_length=100)

    @field_validator("version_ids")
    @classmethod
    def validate_version_ids(cls, values: List[str]) -> List[str]:
        if len(values) != len(set(values)):
            raise ValueError("version_ids must be unique")
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("version_ids must contain 1 to 100 characters")
        return values
