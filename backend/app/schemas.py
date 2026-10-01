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
    project_role: Literal["member", "processor", "reviewer"] = "member"


class DatasetRegister(BaseModel):
    name: str
    source_organization: str | None = None
    capture_date: date | None = None
    declared_crs: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    decision: Literal["accepted", "rejected", "needs_field_verification", "deferred"]
    rationale: str = Field(min_length=1)


class MatchRequest(BaseModel):
    left_dataset_id: str
    right_dataset_id: str
    id_fields: list[str] = Field(default_factory=lambda: ["parcel_id", "plot_id", "khasra_no", "survey_number"])
    max_distance: float = 75.0
    ambiguity_margin: float = 0.08


class ChangeRequest(BaseModel):
    before_dataset_id: str
    after_dataset_id: str
    id_fields: list[str] = Field(default_factory=lambda: ["parcel_id", "plot_id", "khasra_no", "survey_number"])
    geometry_tolerance: float = 1e-8
