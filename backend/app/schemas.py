from datetime import date, datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=15, max_length=128)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=15, max_length=128)
    role: Literal["viewer", "processor", "reviewer", "steward", "field", "citizen", "admin"] = "viewer"


class UserSecurityRequest(BaseModel):
    role: Literal["viewer", "processor", "reviewer", "steward", "field", "citizen", "admin"]
    is_active: bool
    expected_auth_version: int = Field(ge=0)
    rationale: str = Field(min_length=1, max_length=500)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str | None
    owner_id: str
    workflow_revision: int = 0
    validated_revision: int | None = None
    created_at: datetime


class MemberCreate(BaseModel):
    username: str
    project_role: Literal["viewer", "member", "processor", "reviewer", "steward", "field", "citizen"] = "viewer"


class DatasetRegister(BaseModel):
    name: str
    source_organization: str | None = None
    capture_date: date | None = None
    declared_crs: str | None = None
    access_classification: Literal["public", "internal", "restricted"] = "internal"
    accuracy_metadata: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    administrative_namespace: dict[str, Any] = Field(default_factory=dict)
    license_classification: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    source_version: str | None = None
    version_label: str | None = None


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


class DatasetMetadataRequest(BaseModel):
    expected_content_hash: str | None = None
    source_organization: str | None = None
    capture_date: date | None = None
    administrative_namespace: dict[str, Any] = Field(default_factory=dict)
    license_classification: str | None = None
    accuracy_metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    source_version: str | None = None
    version_label: str | None = None
    access_classification: Literal["public", "internal", "restricted"] | None = None


class MappingDictionaryRequest(BaseModel):
    canonical_field: str = Field(min_length=1, max_length=80)
    source_term: str = Field(min_length=1, max_length=255)
    language: Literal["en", "hi"] = "en"
    normalized_term: str = Field(min_length=1, max_length=255)
    value_type: str | None = None
    units: str | None = None
    cardinality: Literal["single", "multi", "composite"] = "single"
    rationale: str = Field(min_length=1, max_length=2000)
    expected_version: int | None = Field(default=None, ge=1)
    confirm: bool = False


class DepartmentTemplateRequest(BaseModel):
    department: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    administrative_namespace: dict[str, Any] = Field(default_factory=dict)
    mapping: dict[str, Any] = Field(default_factory=dict)
    field_descriptions: list[dict[str, Any]] = Field(default_factory=list)
    confirm: bool = False


class ReconciliationRequest(BaseModel):
    source_feature_ids: list[str] = Field(min_length=3, max_length=20)
    anchor_feature_id: str | None = None
    rationale: str | None = Field(default=None, max_length=2000)


class ReconciliationDecision(BaseModel):
    decision: Literal["accepted", "rejected", "deferred"]
    rationale: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)
    overrides: dict[str, Any] = Field(default_factory=dict)
    geometry_source_id: str | None = None
    attribute_source_id: str | None = None
    attribute_sources: dict[str, str] = Field(default_factory=dict)
    approve_selection: bool = False


class GeometryChangeSetRequest(BaseModel):
    operation: Literal["move", "split", "merge", "shared_edge"]
    parcel_entity_ids: list[str] = Field(min_length=1, max_length=100)
    draft_geometries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    successor_ids: list[str] = Field(default_factory=list)
    attribute_source_id: str | None = None
    attribute_sources: dict[str, str] = Field(default_factory=dict)
    attribute_overrides: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)
    authorization: dict[str, Any] = Field(default_factory=dict)
    expected_geometry_fingerprint: str | None = None


class GeometryDecision(BaseModel):
    decision: Literal["approved", "rejected", "deferred"]
    rationale: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(ge=1)


class GeometryDraftRequest(BaseModel):
    operation: Literal["move", "split", "merge", "shared_edge"]
    parcel_entity_ids: list[str] = Field(min_length=1, max_length=100)
    cut_line: dict[str, Any] | None = None
    draft_geometries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    before_geometries: dict[str, dict[str, Any]] = Field(default_factory=dict)


class MeasurementRequest(BaseModel):
    geometry: dict[str, Any]
    source_crs: str = "EPSG:4326"
    analysis_crs: str | None = None
    method: Literal["projected", "geodesic", "display"] = "projected"
    purpose: Literal["display", "analysis", "cadastral_review"] = "analysis"


class GroundControlRequest(BaseModel):
    dataset_id: str
    method: Literal["translation", "similarity", "affine"] = "affine"
    source_crs: str | None = None
    target_crs: str | None = None
    max_checkpoint_residual: float = Field(default=1.0, gt=0, allow_inf_nan=False,
        description="Maximum independent checkpoint error in metres, regardless of target CRS units")
    control_points: list[dict[str, Any]] = Field(min_length=1)


class GroundControlApprovalRequest(BaseModel):
    expected_revision: int = Field(default=1, ge=1)
    rationale: str = Field(min_length=1, max_length=2000)


class TrainingExampleRequest(BaseModel):
    proposal_id: str | None = None
    label: Literal[0, 1]
    features: dict[str, FiniteFloat] = Field(default_factory=dict)
    group_key: str = Field(min_length=1, max_length=255)
    split: Literal["train", "calibration", "validation", "test"] = "train"
    hard_negative: bool = False


class RankerTrainRequest(BaseModel):
    seed: int = 17
    version: str = "ranker-v1"


class RankerActivationRequest(BaseModel):
    active: bool


class AssignmentRequest(BaseModel):
    assignee_id: str
    parcel_entity_ids: list[str] = Field(min_length=1, max_length=500)
    expires_at: datetime | None = None
    reference_policy: dict[str, Any] = Field(default_factory=dict)


class AssignmentUpdateRequest(BaseModel):
    status: Literal["assigned", "active", "revoked"] | None = None
    expires_at: datetime | None = None
    expected_revision: int = Field(ge=1)


class FieldEvidenceRequest(BaseModel):
    parcel_entity_id: str
    client_event_id: str = Field(min_length=1, max_length=100)
    expected_project_revision: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=100000)
    ward: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    radius_m: float | None = Field(default=None, gt=0, le=10000)


class ComplianceRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    jurisdiction: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=80)
    effective_from: date | None = None
    effective_to: date | None = None
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    formula: dict[str, Any] = Field(default_factory=dict)
    threshold: dict[str, Any] = Field(default_factory=dict)
    operator: Literal["<", "<=", ">", ">=", "==", "between", "ratio_le", "ratio_ge"] | None = None
    units: str | None = None
    reference: str | None = None
    confirm: bool = False


class ComplianceEvaluateRequest(BaseModel):
    parcel_entity_id: str
    rule_id: str
    values: dict[str, Any] = Field(default_factory=dict)


class FieldEvidenceResolutionRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    decision: Literal["accept", "reject", "resubmit"]
    rationale: str = Field(min_length=1, max_length=2000)
    new_expected_project_revision: int | None = Field(default=None, ge=0)


class CitizenGrantRequest(BaseModel):
    citizen_username: str
    parcel_entity_id: str
    fields: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class CitizenGrantUpdateRequest(BaseModel):
    status: Literal["active", "revoked"] | None = None
    fields: list[str] | None = None
    expires_at: datetime | None = None


class CitizenCaseRequest(BaseModel):
    parcel_entity_id: str
    category: Literal["evidence", "discrepancy", "dispute", "status"]
    description: str = Field(min_length=1, max_length=4000)


class CitizenCaseResponseRequest(BaseModel):
    response: str = Field(min_length=1, max_length=4000)
    status: Literal["in_review", "resolved", "closed"] = "resolved"
