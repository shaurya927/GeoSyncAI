from datetime import date, datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class LoginRequest(BaseModel):
    username: str
    password: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str | None
    owner_id: str
    created_at: datetime


class MemberCreate(BaseModel):
    username: str
    project_role: Literal["viewer", "member", "processor", "reviewer"] = "viewer"


class DatasetRegister(BaseModel):
    name: str
    source_organization: str | None = None
    capture_date: date | None = None
    declared_crs: str | None = None
    access_classification: Literal["public", "internal", "restricted"] = "internal"
    accuracy_metadata: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CRSConfirmation(BaseModel):
    crs: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=500)


class SchemaMappingRequest(BaseModel):
    mapping: dict[str, str] = Field(default_factory=dict)
    confirm: bool = False


class PolicyRequest(BaseModel):
    expected_version: int = Field(ge=0)
    max_distance: float = Field(gt=0, le=10000)
    ambiguity_margin: float = Field(ge=0, le=1)
    geometry_tolerance: float = Field(ge=0, le=100)
    overlap_tolerance_m2: float = Field(ge=0, le=100)
    coverage_boundary: dict[str, Any] | None = None


class ReviewRequest(BaseModel):
    decision: Literal["under_review", "accepted", "rejected", "needs_field_verification", "deferred"]
    rationale: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)


class SelectionRequest(BaseModel):
    geometry_source_id: str | None = None
    attribute_source_id: str
    attribute_sources: dict[str, str] = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)
    rationale: str = Field(min_length=1, max_length=2000)


class MatchRequest(BaseModel):
    left_dataset_id: str
    right_dataset_id: str
    id_fields: list[str] = Field(default_factory=lambda: ["parcel_id", "plot_id", "khasra_no", "survey_number"])
    max_distance: float = Field(default=75.0, gt=0, le=10000)
    ambiguity_margin: float = Field(default=0.08, ge=0, le=1)
    namespace_fields: list[str] = Field(default_factory=list)


class ChangeRequest(BaseModel):
    before_dataset_id: str
    after_dataset_id: str
    id_fields: list[str] = Field(default_factory=lambda: ["parcel_id", "plot_id", "khasra_no", "survey_number"])
    geometry_tolerance: float = Field(default=0.5, ge=0)
    analysis_crs: str | None = None
