from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .auth import CurrentUser, ensure_project_access, hash_password, verify_password, create_access_token
from .config import get_settings
from .db import get_db, init_db
from .models import (Dataset, Job, MatchProposal, Project, ProjectMember, ReviewDecision, SourceFeature,
                     TopologyConflict, User, ChangeProposal, PublishedVersion, PublicationFeature, SchemaMapping,
                     ParcelEntity, ParcelSourceLink, ParcelSelection, MappingDictionaryEntry, DepartmentTemplate,
                     ReconciliationCase, GeometryChangeSet, GroundControlSession, TrainingExample, ModelArtifact,
                     FieldAssignment, FieldEvidence, ComplianceRule, CitizenGrant, CitizenCase, AuditEvent, RasterAsset, uid)
from .schemas import (CRSConfirmation, ChangeRequest, DatasetRegister, LoginRequest, MatchRequest, MemberCreate,
                       ProjectCreate, ProjectOut, ReviewRequest, SchemaMappingRequest, SelectionRequest, PolicyRequest, Token,
                       DatasetMetadataRequest, MappingDictionaryRequest, DepartmentTemplateRequest, ReconciliationRequest,
                       ReconciliationDecision, GeometryChangeSetRequest, GeometryDecision, MeasurementRequest,
                       GroundControlRequest, TrainingExampleRequest, RankerTrainRequest, AssignmentRequest,
                       FieldEvidenceRequest, QueryRequest, ComplianceRuleRequest, ComplianceEvaluateRequest,
                       CitizenGrantRequest, CitizenCaseRequest)
from .services import (audit, bootstrap_synthetic, confirm_dataset_crs, ingest_dataset, materialize_identity_link,
                       run_change_detection, run_matching, run_topology, administrative_context, touch_project, next_version,
                       invalidate_dataset_evidence)
from .publication import publish, validate_project
from .policy import processing_policy
from .tasks import dispatch_job, recover_jobs, configuration_hash as job_configuration_hash
from .advanced import (build_reconciliation, compliance_result, geometry_measurements,
                       fit_control_points, measure_geometry, parse_structured_query, train_ranker)


DEMO_USERS = {"viewer": "viewer", "processor": "processor", "reviewer": "reviewer", "admin": "admin",
              "field": "field", "citizen": "citizen"}


def bootstrap(db: Session) -> None:
    users: dict[str, User] = {}
    for username, password in DEMO_USERS.items():
        user = db.scalar(select(User).where(User.username == username))
        if not user:
            user = User(username=username, password_hash=hash_password(password), role=username)
            db.add(user)
            db.flush()
        users[username] = user
    project = db.scalar(select(Project).where(Project.name == "Synthetic demonstration"))
    if not project:
        project = Project(name="Synthetic demonstration", description="Safe synthetic parcel workspace", owner_id=users["admin"].id)
        db.add(project)
        db.flush()
    for username in DEMO_USERS:
        if not db.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id,
                                                      ProjectMember.user_id == users[username].id)):
            db.add(ProjectMember(project_id=project.id, user_id=users[username].id,
                                  project_role="owner" if username == "admin" else username))
    db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if not settings.demo_mode and (len(settings.jwt_secret) < 32 or settings.jwt_secret == "change-this-in-production"):
        raise RuntimeError("Set JWT_SECRET to at least 32 random characters or explicitly enable DEMO_MODE")
    if settings.demo_mode and not settings.jwt_secret:
        settings.jwt_secret = "explicit-local-demo-only-secret-do-not-deploy"
    init_db()
    if get_settings().auto_bootstrap:
        db = next(get_db())
        try:
            bootstrap(db)
        finally:
            db.close()
    recover_jobs()
    yield


app = FastAPI(title="GeoSyncAI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


Db = Annotated[Session, Depends(get_db)]


@app.get("/health")
def health(db: Db):
    db.execute(select(User).limit(1))
    return {"status": "ok", "service": "geosyncai", "database": "connected"}


@app.get("/ready")
def readiness(db: Db):
    health(db)
    settings = get_settings()
    if settings.celery_broker_url:
        from redis import Redis
        try:
            Redis.from_url(settings.celery_broker_url, socket_connect_timeout=2).ping()
        except Exception as exc:
            raise HTTPException(503, "Job broker unavailable") from exc
    return {"status": "ready", "jobs": "celery" if settings.celery_broker_url else "local"}


@app.post("/api/auth/token", response_model=Token)
def login(payload: LoginRequest, db: Db):
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return {"access_token": create_access_token(user), "user": {"id": user.id, "username": user.username, "role": user.role}}


@app.get("/api/auth/me")
def me(user: CurrentUser):
    return {"id": user.id, "username": user.username, "role": user.role}


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects(db: Db, user: CurrentUser):
    if user.role == "admin":
        return list(db.scalars(select(Project).order_by(Project.created_at.desc())))
    return list(db.scalars(select(Project).join(ProjectMember, ProjectMember.project_id == Project.id)
                           .where(ProjectMember.user_id == user.id).order_by(Project.created_at.desc())))


@app.post("/api/projects", response_model=ProjectOut)
def create_project(payload: ProjectCreate, db: Db, user: CurrentUser):
    if user.role == "viewer":
        raise HTTPException(status_code=403, detail="Write access denied")
    project = Project(name=payload.name, description=payload.description, owner_id=user.id)
    db.add(project)
    db.flush()
    db.add(ProjectMember(project_id=project.id, user_id=user.id, project_role="owner"))
    audit(db, "project_created", user.id, project.id, "project", project.id)
    db.commit()
    return project


@app.get("/api/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Db, user: CurrentUser):
    return ensure_project_access(db, project_id, user)


@app.get("/api/projects/{project_id}/policy")
def get_policy(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return processing_policy(db, project_id)


@app.post("/api/projects/{project_id}/policy")
def save_policy(project_id: str, payload: PolicyRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    touch_project(db, project_id)
    current = processing_policy(db, project_id)
    if current['version'] != payload.expected_version:
        raise HTTPException(409, "Policy changed; refresh before retrying")
    if payload.coverage_boundary:
        from shapely.geometry import shape
        from .spatial import check_coordinates
        try:
            boundary = shape(payload.coverage_boundary)
            check_coordinates(boundary, True)
            if not boundary.is_valid or boundary.geom_type not in {'Polygon', 'MultiPolygon'}:
                raise ValueError('Invalid polygonal coverage boundary')
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
    policy = {**payload.model_dump(exclude={'expected_version'}), 'version': current['version'] + 1}
    audit(db, 'processing_policy_saved', user.id, project_id, details=policy)
    db.commit()
    return policy


@app.get("/api/projects/{project_id}/history")
def project_history(project_id: str, db: Db, user: CurrentUser):
    from .models import AuditEvent
    ensure_project_access(db, project_id, user)
    return [{'id':e.id, 'action':e.action, 'actor_id':e.actor_id, 'timestamp':e.created_at, 'details':e.details}
            for e in db.scalars(select(AuditEvent).where(AuditEvent.project_id == project_id).order_by(AuditEvent.created_at.desc()).limit(100))]


@app.get("/api/projects/{project_id}/dashboard")
def project_dashboard(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    datasets = list(db.scalars(select(Dataset).where(Dataset.project_id == project_id)))
    features = list(db.scalars(select(SourceFeature).join(Dataset, Dataset.id == SourceFeature.dataset_id)
                               .where(Dataset.project_id == project_id)))
    conflicts = list(db.scalars(select(TopologyConflict).where(TopologyConflict.project_id == project_id)))
    changes = list(db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id)))
    jobs = list(db.scalars(select(Job).where(Job.project_id == project_id)))
    assignments = list(db.scalars(select(FieldAssignment).where(FieldAssignment.project_id == project_id)))
    latest_model = db.scalar(select(ModelArtifact).where(ModelArtifact.project_id == project_id).order_by(ModelArtifact.created_at.desc()))
    return {"denominators": {"datasets": len(datasets), "source_records": len(features), "parcels": len(list(db.scalars(select(ParcelEntity.id).where(ParcelEntity.project_id == project_id)))),
                             "conflicts": len(conflicts), "changes": len(changes), "jobs": len(jobs), "field_assignments": len(assignments)},
            "source_accounting": {status: sum(feature.status == status for feature in features) for status in sorted({feature.status for feature in features})},
            "conflicts_by_type": {kind: sum(conflict.conflict_type == kind for conflict in conflicts) for kind in sorted({conflict.conflict_type for conflict in conflicts})},
            "changes_by_status": {status: sum(change.status == status for change in changes) for status in sorted({change.status for change in changes})},
            "jobs_by_status": {status: sum(job.status == status for job in jobs) for status in sorted({job.status for job in jobs})},
            "fieldwork": {"assigned": len(assignments), "submitted": len(list(db.scalars(select(FieldEvidence).join(FieldAssignment, FieldAssignment.id == FieldEvidence.assignment_id).where(FieldAssignment.project_id == project_id))))},
            "model_reliability": latest_model.metrics if latest_model else None}


@app.get("/api/projects/{project_id}/mapping-templates")
def mapping_templates(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{'id':m.id, 'dataset_id':m.dataset_id, 'version':m.version, 'mapping':m.mapping}
            for m in db.scalars(select(SchemaMapping).where(SchemaMapping.project_id == project_id, SchemaMapping.status == 'confirmed'))]


@app.post("/api/projects/{project_id}/members")
def add_member(project_id: str, payload: MemberCreate, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, review=True)
    if user.role != "admin":
        raise HTTPException(403, "Only administrators manage project membership")
    target = db.scalar(select(User).where(User.username == payload.username))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == target.id))
    if not member:
        member = ProjectMember(project_id=project_id, user_id=target.id, project_role=payload.project_role)
        db.add(member)
    else:
        member.project_role = payload.project_role
    db.commit()
    return {"project_id": project_id, "username": target.username, "project_role": member.project_role}


@app.post("/api/projects/{project_id}/datasets", status_code=201)
def register_dataset(project_id: str, payload: DatasetRegister, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = Dataset(project_id=project_id, name=payload.name, source_organization=payload.source_organization,
                      capture_date=payload.capture_date, declared_crs=payload.declared_crs,
                       access_classification=payload.access_classification,
                       accuracy_metadata=payload.accuracy_metadata, metadata_json=payload.metadata,
                       administrative_namespace=payload.administrative_namespace,
                       license_classification=payload.license_classification, provenance=payload.provenance,
                       source_version=payload.source_version, version_label=payload.version_label)
    db.add(dataset)
    project = db.get(Project, project_id)
    if project:
        project.workflow_revision += 1
        project.validated_revision = None
    audit(db, "dataset_registered", user.id, project_id, "dataset", dataset.id)
    db.commit()
    return dataset_response(dataset)


@app.post("/api/projects/{project_id}/datasets/upload", status_code=201)
async def upload_dataset(project_id: str, db: Db, user: CurrentUser, file: UploadFile = File(...),
                         name: str | None = Form(None), source_organization: str | None = Form(None),
                         capture_date: date | None = Form(None), declared_crs: str | None = Form(None),
                         parent_dataset_id: str | None = Form(None), license_classification: str | None = Form(None),
                         source_version: str | None = Form(None), version_label: str | None = Form(None),
                         administrative_namespace: str | None = Form(None)):
    ensure_project_access(db, project_id, user, write=True)
    data = await file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"Upload exceeds the {get_settings().max_upload_bytes} byte limit")
    namespace = {}
    if administrative_namespace:
        try:
            namespace = json.loads(administrative_namespace)
            if not isinstance(namespace, dict):
                raise ValueError("Administrative namespace must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "Administrative namespace must be valid JSON") from exc
    dataset = Dataset(project_id=project_id, name=name or file.filename or "uploaded dataset",
                      source_organization=source_organization, capture_date=capture_date, declared_crs=declared_crs,
                      parent_dataset_id=parent_dataset_id, license_classification=license_classification,
                      source_version=source_version, version_label=version_label,
                      administrative_namespace=namespace)
    if parent_dataset_id:
        validate_dataset(db, project_id, parent_dataset_id)
    db.add(dataset)
    db.flush()
    try:
        report = ingest_dataset(db, dataset, data, file.filename, file.content_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, "dataset_uploaded", user.id, project_id, "dataset", dataset.id,
          {"content_hash": dataset.content_hash, "record_count": dataset.record_count})
    db.commit()
    return {**dataset_response(dataset), "validation_report": report}


def dataset_response(dataset: Dataset) -> dict:
    return {"id": dataset.id, "project_id": dataset.project_id, "name": dataset.name,
            "source_organization": dataset.source_organization,
            "status": dataset.status, "content_hash": dataset.content_hash, "original_filename": dataset.original_filename,
            "capture_date": dataset.capture_date, "uploaded_at": dataset.uploaded_at, "record_count": dataset.record_count,
            "normalized_count": dataset.normalized_count, "declared_crs": dataset.declared_crs,
            "normalized_crs": dataset.normalized_crs, "analysis_crs": dataset.analysis_crs,
            "crs_transform": dataset.crs_transform, "crs_confirmed_by": dataset.crs_confirmed_by,
            "crs_confirmed_at": dataset.crs_confirmed_at, "access_classification": dataset.access_classification,
             "accuracy_metadata": dataset.accuracy_metadata, "schema_mapping_version": dataset.schema_mapping_version,
             "validation_report": dataset.validation_report, "administrative_namespace": dataset.administrative_namespace,
             "license_classification": dataset.license_classification, "provenance": dataset.provenance,
             "source_version": dataset.source_version, "version_label": dataset.version_label, "metadata": dataset.metadata_json,
             "parent_dataset_id": dataset.parent_dataset_id}


@app.get("/api/projects/{project_id}/datasets")
def list_datasets(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [dataset_response(d) for d in db.scalars(select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.uploaded_at.desc()))]


@app.patch("/api/projects/{project_id}/datasets/{dataset_id}/metadata")
def update_dataset_metadata(project_id: str, dataset_id: str, payload: DatasetMetadataRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = validate_dataset(db, project_id, dataset_id)
    if payload.expected_content_hash and payload.expected_content_hash != dataset.content_hash:
        raise HTTPException(409, "Source content changed; register a new version instead of overwriting metadata")
    linked = db.scalar(select(ParcelSourceLink.id).join(SourceFeature, SourceFeature.id == ParcelSourceLink.source_feature_id)
                       .where(SourceFeature.dataset_id == dataset.id))
    if linked and payload.model_dump(exclude_none=True, exclude={"expected_content_hash"}):
        raise HTTPException(409, "Reviewed source metadata is immutable; register a new dataset version")
    for field in ("source_organization", "capture_date", "administrative_namespace", "license_classification",
                  "accuracy_metadata", "provenance", "source_version", "version_label", "access_classification"):
        value = getattr(payload, field)
        if value is not None:
            setattr(dataset, field, value)
    audit(db, "dataset_metadata_updated", user.id, project_id, "dataset", dataset.id,
          {"content_hash": dataset.content_hash, "fields": list(payload.model_dump(exclude_none=True).keys())})
    db.commit()
    return dataset_response(dataset)


@app.post("/api/projects/{project_id}/raster-assets", status_code=201)
async def upload_raster_asset(project_id: str, db: Db, user: CurrentUser, file: UploadFile = File(...),
                              name: str | None = Form(None), source_crs: str | None = Form(None),
                              attribution: str | None = Form(None), access_classification: str = Form("internal"),
                              source_date: date | None = Form(None)):
    ensure_project_access(db, project_id, user, write=True)
    if access_classification not in {"public", "internal", "restricted"}:
        raise HTTPException(422, "Invalid raster access classification")
    data = await file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(413, "Raster exceeds the configured upload limit")
    if data[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise HTTPException(422, "Raster inspection accepts GeoTIFF/COG bytes only")
    asset = RasterAsset(project_id=project_id, name=name or file.filename or "raster.tif", content_hash=hashlib.sha256(data).hexdigest(),
                        source_crs=source_crs, attribution=attribution, access_classification=access_classification,
                        source_date=source_date, metadata_json={"bytes": len(data), "inspection": "header-only; CRS/bounds must be supplied or inspected by configured GDAL service"},
                        created_by=user.id)
    asset.id = uid()
    raw_path = get_settings().storage_path / f"{asset.id}-{Path(file.filename or 'raster.tif').name.replace('..', '_')}"
    raw_path.write_bytes(data)
    asset.raw_path = str(raw_path)
    db.add(asset)
    audit(db, "raster_registered", user.id, project_id, "raster_asset", asset.id,
          {"content_hash": asset.content_hash, "bytes": len(data), "source_crs": source_crs})
    db.commit()
    return raster_response(asset)


@app.get("/api/projects/{project_id}/raster-assets")
def list_raster_assets(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [raster_response(asset) for asset in db.scalars(select(RasterAsset).where(
        RasterAsset.project_id == project_id).order_by(RasterAsset.created_at.desc()))]


@app.get("/api/projects/{project_id}/raster-assets/{asset_id}/bytes")
def download_raster_asset(project_id: str, asset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    asset = db.get(RasterAsset, asset_id)
    if not asset or asset.project_id != project_id:
        raise HTTPException(404, "Raster asset not found")
    if asset.access_classification == "restricted" and user.role not in {"admin", "reviewer", "steward"}:
        raise HTTPException(403, "Restricted raster requires steward or reviewer permission")
    path = Path(asset.raw_path)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != asset.content_hash:
        raise HTTPException(409, "Raster integrity verification failed")
    return StreamingResponse(open(path, "rb"), media_type=asset.mime_type,
                             headers={"Content-Disposition": f'attachment; filename="{asset.name}"',
                                      "X-GeoSyncAI-Attribution": asset.attribution or ""})


def raster_response(asset: RasterAsset) -> dict:
    return {"id": asset.id, "name": asset.name, "content_hash": asset.content_hash, "mime_type": asset.mime_type,
            "source_crs": asset.source_crs, "bounds": asset.bounds, "metadata": asset.metadata_json,
            "attribution": asset.attribution, "access_classification": asset.access_classification,
            "source_date": asset.source_date, "status": asset.status}


@app.get("/api/projects/{project_id}/mapping-dictionary")
def list_mapping_dictionary(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": entry.id, "canonical_field": entry.canonical_field, "source_term": entry.source_term,
             "language": entry.language, "normalized_term": entry.normalized_term, "value_type": entry.value_type,
             "units": entry.units, "cardinality": entry.cardinality, "rationale": entry.rationale,
             "version": entry.version, "status": entry.status, "created_at": entry.created_at}
            for entry in db.scalars(select(MappingDictionaryEntry).where(MappingDictionaryEntry.project_id == project_id)
                                    .order_by(MappingDictionaryEntry.canonical_field, MappingDictionaryEntry.language, MappingDictionaryEntry.version))]


@app.post("/api/projects/{project_id}/mapping-dictionary", status_code=201)
def add_mapping_dictionary(project_id: str, payload: MappingDictionaryRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    version = (db.scalar(select(MappingDictionaryEntry.version).where(
        MappingDictionaryEntry.project_id == project_id,
        MappingDictionaryEntry.canonical_field == payload.canonical_field,
        MappingDictionaryEntry.source_term == payload.source_term,
        MappingDictionaryEntry.language == payload.language).order_by(MappingDictionaryEntry.version.desc())) or 0) + 1
    if payload.expected_version is not None and payload.expected_version != version:
        raise HTTPException(409, "Mapping dictionary term changed; refresh before retrying")
    entry = MappingDictionaryEntry(project_id=project_id, canonical_field=payload.canonical_field,
                                   source_term=payload.source_term, language=payload.language,
                                   normalized_term=payload.normalized_term, value_type=payload.value_type,
                                   units=payload.units, cardinality=payload.cardinality, rationale=payload.rationale,
                                   version=version, status="confirmed" if payload.confirm else "draft", created_by=user.id)
    db.add(entry)
    audit(db, "mapping_dictionary_saved", user.id, project_id, "mapping_dictionary", entry.id,
          {"canonical_field": entry.canonical_field, "language": entry.language, "version": version})
    db.commit()
    return {"id": entry.id, "version": version, "status": entry.status}


@app.get("/api/projects/{project_id}/department-templates")
def list_department_templates(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": template.id, "department": template.department, "name": template.name,
             "administrative_namespace": template.administrative_namespace, "version": template.version,
             "mapping": template.mapping, "field_descriptions": template.field_descriptions, "status": template.status}
            for template in db.scalars(select(DepartmentTemplate).where(DepartmentTemplate.project_id == project_id)
                                       .order_by(DepartmentTemplate.name, DepartmentTemplate.version.desc()))]


@app.post("/api/projects/{project_id}/department-templates", status_code=201)
def save_department_template(project_id: str, payload: DepartmentTemplateRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    version = (db.scalar(select(DepartmentTemplate.version).where(
        DepartmentTemplate.project_id == project_id, DepartmentTemplate.name == payload.name)
        .order_by(DepartmentTemplate.version.desc())) or 0) + 1
    template = DepartmentTemplate(project_id=project_id, department=payload.department, name=payload.name,
                                  administrative_namespace=payload.administrative_namespace, version=version,
                                  mapping=payload.mapping, field_descriptions=payload.field_descriptions,
                                  status="confirmed" if payload.confirm else "draft", created_by=user.id)
    db.add(template)
    audit(db, "department_template_saved", user.id, project_id, "department_template", template.id,
          {"name": template.name, "version": version, "confirmed": payload.confirm})
    db.commit()
    return {"id": template.id, "version": version, "status": template.status, "mapping": template.mapping}


@app.post("/api/projects/{project_id}/datasets/{dataset_id}/confirm-crs")
def confirm_crs(project_id: str, dataset_id: str, payload: CRSConfirmation, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = validate_dataset(db, project_id, dataset_id)
    try:
        report = confirm_dataset_crs(db, dataset, payload.crs, user.id, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**dataset_response(dataset), "validation_report": report}


@app.get("/api/projects/{project_id}/datasets/{dataset_id}/mapping")
def get_schema_mapping(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    dataset = validate_dataset(db, project_id, dataset_id)
    mapping = db.scalar(select(SchemaMapping).where(SchemaMapping.dataset_id == dataset.id)
                        .order_by(SchemaMapping.version.desc()))
    if not mapping:
        fields = (dataset.validation_report or {}).get("schema_fields", [])
        aliases = {"parcel_id": ["parcel_id", "plot_id", "khasra_no"], "survey_number": ["survey_number", "survey_no"],
                   "property_account": ["property_id", "account_no"], "village": ["village", "village_name"],
                   "village_code": ["village_code", "admin_code"], "recorded_area": ["area", "recorded_area", "area_m2", "area_sq_m", "recorded_area_m2"]}
        suggested = {key: field for key, names in aliases.items() for field in fields if field.casefold() in names}
        return {"version": 0, "status": "draft", "mapping": suggested,
                "source_fields": (dataset.validation_report or {}).get('schema_summary', [{"name": field} for field in fields])}
    return {"id": mapping.id, "version": mapping.version, "status": mapping.status,
            "mapping": mapping.mapping, "source_fields": mapping.source_fields,
            "created_at": mapping.created_at}


@app.post("/api/projects/{project_id}/datasets/{dataset_id}/mapping")
def save_schema_mapping(project_id: str, dataset_id: str, payload: SchemaMappingRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = validate_dataset(db, project_id, dataset_id)
    version = dataset.schema_mapping_version + 1
    fields = (dataset.validation_report or {}).get("schema_fields", [])
    allowed = {"parcel_id", "survey_number", "property_account", "village", "village_code", "district", "ward", "recorded_area", "area_units"}
    if not set(payload.mapping).issubset(allowed) or not set(payload.mapping.values()).issubset(fields):
        raise HTTPException(422, "Mapping must use supported canonical fields and existing source fields")
    if payload.confirm and not set(payload.mapping).intersection({"parcel_id", "survey_number", "property_account"}):
        raise HTTPException(422, "Confirm an identifier semantic: parcel ID, survey number, or property account")
    try:
        invalidate_dataset_evidence(db, dataset_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    record = SchemaMapping(project_id=project_id, dataset_id=dataset.id, version=version,
                           status="confirmed" if payload.confirm else "draft", mapping=payload.mapping,
                           source_fields=(dataset.validation_report or {}).get('schema_summary', [{"name": field} for field in fields]), created_by=user.id)
    db.add(record)
    db.flush()
    dataset.schema_mapping_version = version
    if payload.confirm:
        for feature in db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id)):
            feature.canonical_attributes = {key: feature.raw_attributes.get(field) for key, field in payload.mapping.items()}
            feature.administrative_context = administrative_context(feature.canonical_attributes)
    project = db.get(Project, project_id)
    if project:
        project.workflow_revision += 1
        project.validated_revision = None
    audit(db, "schema_mapping_saved", user.id, project_id, "schema_mapping", record.id,
          {"dataset_id": dataset.id, "version": version, "confirmed": payload.confirm})
    db.commit()
    return {"id": record.id, "version": record.version, "status": record.status,
            "mapping": record.mapping, "source_fields": record.source_fields}


@app.post("/api/projects/{project_id}/bootstrap-synthetic")
def synthetic(project_id: str, db: Db, user: CurrentUser, count: int = 25):
    ensure_project_access(db, project_id, user, write=True)
    return bootstrap_synthetic(db, project_id, user.id, count)


@app.get("/api/projects/{project_id}/datasets/{dataset_id}/features")
def list_features(project_id: str, dataset_id: str, db: Db, user: CurrentUser, limit: int = 500, offset: int = 0,
                  bbox: str | None = None):
    ensure_project_access(db, project_id, user)
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    limit = max(1, min(limit, 1000)); offset = max(0, offset)
    features = list(db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id)
                               .order_by(SourceFeature.id)))
    bounds = None
    if bbox:
        try:
            values = [float(value) for value in bbox.split(",")]
            if len(values) != 4: raise ValueError
            bounds = values
        except ValueError as exc:
            raise HTTPException(422, "bbox must contain four numeric CRS84 coordinates") from exc
    if bounds:
        from shapely.geometry import shape
        features = [feature for feature in features if feature.normalized_geometry and not (
            shape(feature.normalized_geometry).bounds[2] < bounds[0] or shape(feature.normalized_geometry).bounds[0] > bounds[2] or
            shape(feature.normalized_geometry).bounds[3] < bounds[1] or shape(feature.normalized_geometry).bounds[1] > bounds[3])]
    return [{"id": f.id, "original_id": f.original_id, "attributes": f.raw_attributes,
             "original_geometry": f.original_geometry, "geometry": f.normalized_geometry,
             "administrative_context": f.administrative_context, "canonical_attributes": f.canonical_attributes, "status": f.status,
             "processing_reason": f.processing_reason}
             for f in features[offset:offset + limit]]


@app.get("/api/projects/{project_id}/datasets/{dataset_id}/raw")
def download_raw(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id or not dataset.raw_path:
        raise HTTPException(status_code=404, detail="Raw file not found")
    if dataset.access_classification == "restricted" and user.role not in {"admin", "reviewer", "steward"}:
        raise HTTPException(status_code=403, detail="Restricted source download requires steward or reviewer permission")
    path = get_settings().storage_path / (dataset.id + "-" + (dataset.original_filename or "upload.bin").replace("..", ""))
    # Raw path is generated by the service and never taken from a URL parameter.
    path = __import__("pathlib").Path(dataset.raw_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Raw file not found")
    return StreamingResponse(open(path, "rb"), media_type=dataset.mime_type or "application/octet-stream",
                             headers={"Content-Disposition": f'attachment; filename="{dataset.original_filename or "dataset.bin"}"'})


@app.post("/api/projects/{project_id}/match")
def match(project_id: str, payload: MatchRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    validate_dataset_pair(db, project_id, payload.left_dataset_id, payload.right_dataset_id)
    require_spatial_ready(validate_dataset(db, project_id, payload.left_dataset_id))
    require_spatial_ready(validate_dataset(db, project_id, payload.right_dataset_id))
    result = run_matching(db, project_id, payload.left_dataset_id, payload.right_dataset_id,
                          payload.id_fields, payload.max_distance, payload.ambiguity_margin,
                          payload.namespace_fields)
    audit(db, "matching_completed", user.id, project_id, details={"result": result})
    db.commit()
    return result


@app.post("/api/projects/{project_id}/topology/{dataset_id}")
def topology(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    validate_dataset(db, project_id, dataset_id)
    require_spatial_ready(validate_dataset(db, project_id, dataset_id))
    return run_topology(db, project_id, dataset_id)


@app.get("/api/projects/{project_id}/matches")
def matches(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": p.id, "left_feature_id": p.left_feature_id, "right_feature_id": p.right_feature_id,
             "score": p.score, "score_type": p.score_type, "status": p.status, "revision": p.revision,
             "evidence": p.evidence}
            for p in db.scalars(select(MatchProposal).where(MatchProposal.project_id == project_id))]


@app.post("/api/projects/{project_id}/reconciliations")
def create_reconciliation(project_id: str, payload: ReconciliationRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    try:
        case = build_reconciliation(db, project_id, payload.source_feature_ids, payload.anchor_feature_id, user.id, payload.rationale)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return reconciliation_response(case)


@app.get("/api/projects/{project_id}/reconciliations")
def list_reconciliations(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [reconciliation_response(case) for case in db.scalars(select(ReconciliationCase).where(
        ReconciliationCase.project_id == project_id).order_by(ReconciliationCase.created_at.desc()))]


@app.post("/api/projects/{project_id}/reconciliations/{case_id}/decision")
def decide_reconciliation(project_id: str, case_id: str, payload: ReconciliationDecision, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    case = db.get(ReconciliationCase, case_id)
    if not case or case.project_id != project_id:
        raise HTTPException(404, "Reconciliation case not found")
    if case.revision != payload.expected_revision or case.status in {"accepted", "rejected"}:
        raise HTTPException(409, "Reconciliation case is stale or final")
    case.status = payload.decision
    case.rationale = payload.rationale
    case.recommendation = {**case.recommendation, "authorized_overrides": payload.overrides}
    case.revision += 1
    audit(db, "reconciliation_decision", user.id, project_id, "reconciliation", case.id,
          {"decision": payload.decision, "overrides": payload.overrides})
    db.commit()
    return reconciliation_response(case)


def reconciliation_response(case: ReconciliationCase) -> dict:
    return {"id": case.id, "anchor_feature_id": case.anchor_feature_id, "source_feature_ids": case.source_feature_ids,
            "status": case.status, "revision": case.revision, "recommendation": case.recommendation,
            "competing_values": case.competing_values, "evidence": case.evidence, "rationale": case.rationale}


@app.post("/api/projects/{project_id}/geometry-changes")
def create_geometry_changeset(project_id: str, payload: GeometryChangeSetRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    entities = list(db.scalars(select(ParcelEntity).where(ParcelEntity.project_id == project_id,
                                                          ParcelEntity.id.in_(payload.parcel_entity_ids),
                                                          ParcelEntity.status == "active")))
    if len(entities) != len(set(payload.parcel_entity_ids)):
        raise HTTPException(404, "Every parcel in a geometry changeset must belong to this project")
    if payload.operation in {"split", "merge", "shared_edge"} and len(payload.parcel_entity_ids) < 2:
        raise HTTPException(422, "This operation requires at least two affected parcel identities")
    try:
        measurements = geometry_measurements(db, payload.parcel_entity_ids, payload.draft_geometries)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if payload.operation == "split" and measurements["area_conservation_delta_m2"] > 0.01:
        raise HTTPException(422, "Split draft does not conserve source area within 0.01 m²")
    change = GeometryChangeSet(project_id=project_id, operation=payload.operation,
                               parcel_entity_ids=payload.parcel_entity_ids,
                               predecessor_ids=payload.parcel_entity_ids if payload.operation in {"split", "merge"} else [],
                               successor_ids=payload.successor_ids, before_geometries=measurements.get("before_geometries", {}),
                               draft_geometries=payload.draft_geometries,
                               measurements=measurements, authorization=payload.authorization,
                               rationale=payload.rationale, created_by=user.id)
    db.add(change)
    touch_project(db, project_id)
    audit(db, "geometry_changeset_created", user.id, project_id, "geometry_changeset", change.id,
          {"operation": payload.operation, "measurements": measurements})
    db.commit()
    return geometry_changeset_response(change)


@app.get("/api/projects/{project_id}/geometry-changes")
def list_geometry_changes(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [geometry_changeset_response(change) for change in db.scalars(select(GeometryChangeSet).where(
        GeometryChangeSet.project_id == project_id).order_by(GeometryChangeSet.created_at.desc()))]


@app.post("/api/projects/{project_id}/geometry-changes/{change_id}/decision")
def decide_geometry_changeset(project_id: str, change_id: str, payload: GeometryDecision, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    change = db.get(GeometryChangeSet, change_id)
    if not change or change.project_id != project_id:
        raise HTTPException(404, "Geometry changeset not found")
    if change.revision != payload.expected_revision or change.status in {"approved", "rejected"}:
        raise HTTPException(409, "Geometry changeset is stale or final")
    change.status = payload.decision
    change.rationale = f"{change.rationale}\nDecision: {payload.rationale}"
    change.revision += 1
    if payload.decision == "approved":
        change.approved_geometries = change.draft_geometries
        # Publication still requires an explicit validated baseline/change. The
        # approved artifact is recorded here and never mutates original uploads.
        audit(db, "geometry_changeset_approved", user.id, project_id, "geometry_changeset", change.id,
              {"operation": change.operation, "predecessors": change.predecessor_ids, "successors": change.successor_ids,
               "measurements": change.measurements})
    else:
        audit(db, "geometry_changeset_decision", user.id, project_id, "geometry_changeset", change.id,
              {"decision": payload.decision})
    touch_project(db, project_id)
    db.commit()
    return geometry_changeset_response(change)


def geometry_changeset_response(change: GeometryChangeSet) -> dict:
    return {"id": change.id, "operation": change.operation, "status": change.status, "revision": change.revision,
            "parcel_entity_ids": change.parcel_entity_ids, "predecessor_ids": change.predecessor_ids,
            "successor_ids": change.successor_ids, "before_geometries": change.before_geometries,
            "draft_geometries": change.draft_geometries, "approved_geometries": change.approved_geometries,
            "measurements": change.measurements, "authorization": change.authorization, "rationale": change.rationale}


@app.post("/api/projects/{project_id}/measurements")
def measure(project_id: str, payload: MeasurementRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    try:
        return measure_geometry(payload.geometry, payload.source_crs, payload.analysis_crs, payload.purpose)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/ground-control")
def create_ground_control(project_id: str, payload: GroundControlRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    validate_dataset(db, project_id, payload.dataset_id)
    if len(payload.control_points) < {"translation": 1, "similarity": 2, "affine": 3}[payload.method]:
        raise HTTPException(422, "Insufficient paired control points for the selected fitting method")
    # Coordinates are supplied as {source:[x,y], target:[x,y]}; residuals are
    # explicit and independent checkpoints can be tagged in the point payload.
    for point in payload.control_points:
        source = point.get("source"); target = point.get("target")
        if not isinstance(source, list) or not isinstance(target, list) or len(source) < 2 or len(target) < 2:
            raise HTTPException(422, "Each control point needs source and target coordinate pairs")
    try:
        fit = fit_control_points(payload.method, payload.control_points)
    except (ValueError, TypeError, FloatingPointError) as exc:
        raise HTTPException(422, str(exc)) from exc
    session = GroundControlSession(project_id=project_id, dataset_id=payload.dataset_id, method=payload.method,
                                   control_points=payload.control_points,
                                   residuals={"count": len(payload.control_points), **fit}, created_by=user.id)
    db.add(session)
    touch_project(db, project_id)
    audit(db, "ground_control_created", user.id, project_id, "ground_control", session.id,
          {"dataset_id": payload.dataset_id, "method": payload.method, "residuals": session.residuals})
    db.commit()
    return ground_control_response(session)


@app.post("/api/projects/{project_id}/ground-control/{session_id}/approve")
def approve_ground_control(project_id: str, session_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    session = db.get(GroundControlSession, session_id)
    if not session or session.project_id != project_id:
        raise HTTPException(404, "Ground control session not found")
    if not session.residuals.get("independent_checkpoints"):
        raise HTTPException(409, "An independent checkpoint is required before approval")
    session.status = "approved"; session.approved_by = user.id; session.revision += 1
    audit(db, "ground_control_approved", user.id, project_id, "ground_control", session.id, session.residuals)
    db.commit()
    return ground_control_response(session)


def ground_control_response(session: GroundControlSession) -> dict:
    return {"id": session.id, "dataset_id": session.dataset_id, "method": session.method,
            "control_points": session.control_points, "residuals": session.residuals,
            "status": session.status, "revision": session.revision}


@app.post("/api/projects/{project_id}/training-examples", status_code=201)
def add_training_example(project_id: str, payload: TrainingExampleRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    features = dict(payload.features)
    if payload.proposal_id:
        proposal = db.get(MatchProposal, payload.proposal_id)
        if not proposal or proposal.project_id != project_id:
            raise HTTPException(404, "Match proposal not found")
        features = {**features, **{key: float(value) for key, value in {
            "score": proposal.score, "identifier_agreement": bool((proposal.evidence or {}).get("identifier_agreement")),
            "iou": (proposal.evidence or {}).get("intersection_over_union", 0.0) or 0.0,
            "distance_inverse": 1.0 / (1.0 + float((proposal.evidence or {}).get("centroid_distance_m") or 100000.0)),
            "namespace_compatible": bool((proposal.evidence or {}).get("namespace_compatible", True)),
        }.items()}}
    if not features:
        raise HTTPException(422, "Reviewed examples need proposal evidence or numeric feature values")
    example = TrainingExample(project_id=project_id, proposal_id=payload.proposal_id, label=payload.label,
                              features=features, group_key=payload.group_key, split=payload.split,
                              hard_negative=payload.hard_negative, created_by=user.id)
    db.add(example)
    audit(db, "training_example_reviewed", user.id, project_id, "training_example", example.id,
          {"label": payload.label, "split": payload.split, "group_key": payload.group_key, "hard_negative": payload.hard_negative})
    db.commit()
    return {"id": example.id, "label": example.label, "split": example.split, "group_key": example.group_key}


@app.post("/api/projects/{project_id}/ranker/train")
def train_project_ranker(project_id: str, payload: RankerTrainRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    try:
        artifact = train_ranker(db, project_id, user.id, payload.seed, payload.version)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return model_artifact_response(artifact)


@app.get("/api/projects/{project_id}/ranker")
def list_rankers(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [model_artifact_response(artifact) for artifact in db.scalars(select(ModelArtifact).where(
        ModelArtifact.project_id == project_id).order_by(ModelArtifact.created_at.desc()))]


def model_artifact_response(artifact: ModelArtifact) -> dict:
    return {"id": artifact.id, "model_type": artifact.model_type, "version": artifact.version,
            "artifact": artifact.artifact, "metrics": artifact.metrics, "dataset_fingerprint": artifact.dataset_fingerprint,
            "seed": artifact.seed, "created_at": artifact.created_at}


@app.post("/api/projects/{project_id}/field-assignments", status_code=201)
def create_field_assignment(project_id: str, payload: AssignmentRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    assignee = db.get(User, payload.assignee_id)
    if not assignee or assignee.role not in {"field", "processor", "reviewer", "admin"}:
        raise HTTPException(422, "Assignments require an authorized fieldwork account")
    entities = list(db.scalars(select(ParcelEntity).where(ParcelEntity.project_id == project_id,
                                                          ParcelEntity.id.in_(payload.parcel_entity_ids),
                                                          ParcelEntity.status == "active")))
    if len(entities) != len(set(payload.parcel_entity_ids)):
        raise HTTPException(404, "Assignment contains a parcel outside this project")
    assignment = FieldAssignment(project_id=project_id, assignee_id=payload.assignee_id,
                                 parcel_entity_ids=payload.parcel_entity_ids, expires_at=payload.expires_at,
                                 reference_policy=payload.reference_policy, created_by=user.id)
    db.add(assignment)
    audit(db, "field_assignment_created", user.id, project_id, "field_assignment", assignment.id,
          {"assignee_id": payload.assignee_id, "parcel_entity_ids": payload.parcel_entity_ids})
    db.commit()
    return assignment_response(assignment)


@app.get("/api/projects/{project_id}/field-assignments")
def list_field_assignments(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    query = select(FieldAssignment).where(FieldAssignment.project_id == project_id)
    if user.role == "field":
        query = query.where(FieldAssignment.assignee_id == user.id)
    return [assignment_response(assignment) for assignment in db.scalars(query.order_by(FieldAssignment.created_at.desc()))]


@app.post("/api/projects/{project_id}/field-assignments/{assignment_id}/evidence", status_code=201)
def submit_field_evidence(project_id: str, assignment_id: str, payload: FieldEvidenceRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    assignment = db.get(FieldAssignment, assignment_id)
    if not assignment or assignment.project_id != project_id or (user.role == "field" and assignment.assignee_id != user.id):
        raise HTTPException(404, "Assignment not found")
    if payload.parcel_entity_id not in (assignment.parcel_entity_ids or []):
        raise HTTPException(422, "Evidence parcel is outside the bounded assignment")
    existing = db.scalar(select(FieldEvidence).where(FieldEvidence.client_event_id == payload.client_event_id))
    if existing:
        return field_evidence_response(existing)
    project = db.get(Project, project_id)
    status = "submitted" if project and payload.expected_project_revision == project.workflow_revision else "revision_conflict"
    evidence = FieldEvidence(assignment_id=assignment_id, parcel_entity_id=payload.parcel_entity_id,
                             client_event_id=payload.client_event_id, payload=payload.payload, status=status,
                             expected_project_revision=payload.expected_project_revision, created_by=user.id)
    db.add(evidence)
    audit(db, "field_evidence_submitted", user.id, project_id, "field_evidence", evidence.id,
          {"client_event_id": payload.client_event_id, "status": status})
    db.commit()
    return field_evidence_response(evidence)


def assignment_response(assignment: FieldAssignment) -> dict:
    return {"id": assignment.id, "assignee_id": assignment.assignee_id, "parcel_entity_ids": assignment.parcel_entity_ids,
            "expires_at": assignment.expires_at, "status": assignment.status, "revision": assignment.revision,
            "reference_policy": assignment.reference_policy, "created_at": assignment.created_at}


def field_evidence_response(evidence: FieldEvidence) -> dict:
    return {"id": evidence.id, "assignment_id": evidence.assignment_id, "parcel_entity_id": evidence.parcel_entity_id,
            "client_event_id": evidence.client_event_id, "payload": evidence.payload, "status": evidence.status,
            "expected_project_revision": evidence.expected_project_revision, "created_at": evidence.created_at}


@app.post("/api/projects/{project_id}/query")
def structured_query(project_id: str, payload: QueryRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    plan = parse_structured_query(payload.query)
    plan["limit"] = payload.limit
    if plan["kind"] == "help":
        return {"plan": plan, "clarification": "Ask for conflicts by ward, missing links, dated changes, or nearby tasks."}
    rows: list[dict] = []
    if plan["kind"] == "conflicts":
        conflicts_rows = list(db.scalars(select(TopologyConflict).where(TopologyConflict.project_id == project_id)
                                        .order_by(TopologyConflict.created_at.desc()).limit(payload.limit)))
        rows = [{"id": row.id, "type": row.conflict_type, "severity": row.severity, "description": row.description,
                 "details": row.details} for row in conflicts_rows]
        if plan["filters"].get("ward"):
            ward = plan["filters"]["ward"]
            rows = [row for row in rows if ward.casefold() in json.dumps(row.get("details", {})).casefold()]
    elif plan["kind"] == "missing_links":
        linked = {link.source_feature_id for link in db.scalars(select(ParcelSourceLink).where(ParcelSourceLink.project_id == project_id))}
        rows = [{"feature_id": feature.id, "dataset_id": feature.dataset_id, "original_id": feature.original_id}
                for feature in db.scalars(select(SourceFeature).join(Dataset, Dataset.id == SourceFeature.dataset_id)
                                          .where(Dataset.project_id == project_id, SourceFeature.status == "processed"))
                if feature.id not in linked][:payload.limit]
    elif plan["kind"] == "area_threshold":
        threshold = plan["filters"].get("area_threshold")
        if threshold is None:
            return {"plan": plan, "filters": plan["filters"], "rows": [], "clarification": plan.get("clarification"), "read_only": True}
        source_rows = db.scalars(select(SourceFeature).join(Dataset, Dataset.id == SourceFeature.dataset_id)
                                 .where(Dataset.project_id == project_id, SourceFeature.status == "processed"))
        for feature in source_rows:
            values = feature.canonical_attributes or feature.raw_attributes
            raw_area = values.get("recorded_area", values.get("area", values.get("area_m2")))
            try:
                if float(raw_area) >= float(threshold):
                    rows.append({"feature_id": feature.id, "dataset_id": feature.dataset_id, "original_id": feature.original_id,
                                 "area": raw_area, "units": values.get("area_units")})
            except (TypeError, ValueError):
                continue
            if len(rows) >= payload.limit:
                break
    elif plan["kind"] == "dated_changes":
        rows = [{"id": row.id, "type": row.change_type, "status": row.status, "evidence": row.evidence}
                for row in db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id)
                                      .order_by(ChangeProposal.created_at.desc()).limit(payload.limit))]
    elif plan["kind"] == "nearby_tasks":
        rows = [assignment_response(row) for row in db.scalars(select(FieldAssignment).where(
            FieldAssignment.project_id == project_id).order_by(FieldAssignment.created_at.desc()).limit(payload.limit))]
    return {"plan": plan, "filters": plan["filters"], "rows": rows, "read_only": True, "truncated": len(rows) >= payload.limit}


@app.post("/api/projects/{project_id}/compliance-rules", status_code=201)
def create_compliance_rule(project_id: str, payload: ComplianceRuleRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=payload.confirm)
    rule = ComplianceRule(project_id=project_id, name=payload.name, jurisdiction=payload.jurisdiction,
                          category=payload.category, effective_from=payload.effective_from, effective_to=payload.effective_to,
                          inputs=payload.inputs, formula=payload.formula, threshold=payload.threshold,
                          status="confirmed" if payload.confirm else "draft", created_by=user.id)
    db.add(rule)
    audit(db, "compliance_rule_saved", user.id, project_id, "compliance_rule", rule.id,
          {"name": rule.name, "category": rule.category, "status": rule.status})
    db.commit()
    return compliance_rule_response(rule)


@app.get("/api/projects/{project_id}/compliance-rules")
def list_compliance_rules(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [compliance_rule_response(rule) for rule in db.scalars(select(ComplianceRule).where(
        ComplianceRule.project_id == project_id).order_by(ComplianceRule.name, ComplianceRule.version.desc()))]


@app.post("/api/projects/{project_id}/compliance/evaluate")
def evaluate_compliance(project_id: str, payload: ComplianceEvaluateRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    rule = db.get(ComplianceRule, payload.rule_id)
    entity = db.get(ParcelEntity, payload.parcel_entity_id)
    if not rule or rule.project_id != project_id or not entity or entity.project_id != project_id:
        raise HTTPException(404, "Rule or parcel not found")
    if rule.status != "confirmed":
        raise HTTPException(409, "Only an approved, versioned compliance rule can be evaluated")
    result = compliance_result(rule, payload.values)
    audit(db, "compliance_evaluated", user.id, project_id, "compliance_rule", rule.id,
          {"parcel_entity_id": entity.id, "result": result})
    db.commit()
    return result


def compliance_rule_response(rule: ComplianceRule) -> dict:
    return {"id": rule.id, "name": rule.name, "jurisdiction": rule.jurisdiction, "category": rule.category,
            "version": rule.version, "effective_from": rule.effective_from, "effective_to": rule.effective_to,
            "inputs": rule.inputs, "formula": rule.formula, "threshold": rule.threshold, "status": rule.status}


@app.post("/api/projects/{project_id}/citizen-grants", status_code=201)
def create_citizen_grant(project_id: str, payload: CitizenGrantRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    citizen = db.scalar(select(User).where(User.username == payload.citizen_username))
    entity = db.get(ParcelEntity, payload.parcel_entity_id)
    allowed_fields = {"parcel_id", "survey_number", "property_account", "village", "village_code", "district", "ward",
                      "recorded_area", "area_units", "geometry", "status", "capture_date"}
    if not set(payload.fields).issubset(allowed_fields):
        raise HTTPException(422, "Citizen grants can contain only approved public fields")
    if not citizen or citizen.role != "citizen" or not entity or entity.project_id != project_id:
        raise HTTPException(422, "An explicit citizen account and project parcel are required")
    grant = CitizenGrant(project_id=project_id, citizen_id=citizen.id, parcel_entity_id=entity.id,
                         fields=sorted(set(payload.fields)), expires_at=payload.expires_at, granted_by=user.id)
    db.add(grant)
    audit(db, "citizen_grant_created", user.id, project_id, "citizen_grant", grant.id,
          {"citizen_id": citizen.id, "parcel_entity_id": entity.id, "fields": grant.fields})
    db.commit()
    return citizen_grant_response(grant)


@app.get("/api/projects/{project_id}/citizen-grants")
def list_citizen_grants(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    query = select(CitizenGrant).where(CitizenGrant.project_id == project_id)
    if user.role == "citizen":
        query = query.where(CitizenGrant.citizen_id == user.id)
    return [citizen_grant_response(grant) for grant in db.scalars(query.order_by(CitizenGrant.created_at.desc()))]


@app.get("/api/projects/{project_id}/citizen-records")
def citizen_records(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    grants = list(db.scalars(select(CitizenGrant).where(CitizenGrant.project_id == project_id,
                                                        CitizenGrant.citizen_id == user.id,
                                                        CitizenGrant.status == "active")))
    output = []
    for grant in grants:
        entity = db.get(ParcelEntity, grant.parcel_entity_id)
        selection = db.scalar(select(ParcelSelection).where(ParcelSelection.parcel_entity_id == grant.parcel_entity_id))
        if not entity or not selection:
            continue
        source = db.get(SourceFeature, selection.attribute_source_id)
        if not source:
            continue
        values = {field: (source.canonical_attributes or source.raw_attributes).get(field) for field in grant.fields}
        geometry_source = db.get(SourceFeature, selection.geometry_source_id) if selection.geometry_source_id else None
        output.append({"parcel_entity_id": entity.id, "fields": values, "grant_id": grant.id,
                       "geometry": (geometry_source or source).normalized_geometry if "geometry" in grant.fields else None})
    return output


@app.post("/api/projects/{project_id}/citizen-cases", status_code=201)
def create_citizen_case(project_id: str, payload: CitizenCaseRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    grant = db.scalar(select(CitizenGrant).where(CitizenGrant.project_id == project_id,
                                                CitizenGrant.citizen_id == user.id,
                                                CitizenGrant.parcel_entity_id == payload.parcel_entity_id,
                                                CitizenGrant.status == "active"))
    if not grant:
        raise HTTPException(403, "An explicit active record grant is required")
    case = CitizenCase(project_id=project_id, grant_id=grant.id, parcel_entity_id=payload.parcel_entity_id,
                       category=payload.category, description=payload.description)
    db.add(case)
    audit(db, "citizen_case_submitted", user.id, project_id, "citizen_case", case.id,
          {"grant_id": grant.id, "category": case.category})
    db.commit()
    return citizen_case_response(case)


@app.get("/api/projects/{project_id}/citizen-cases")
def list_citizen_cases(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    query = select(CitizenCase).where(CitizenCase.project_id == project_id)
    if user.role == "citizen":
        query = query.join(CitizenGrant, CitizenGrant.id == CitizenCase.grant_id).where(CitizenGrant.citizen_id == user.id)
    return [citizen_case_response(case) for case in db.scalars(query.order_by(CitizenCase.created_at.desc()))]


def citizen_grant_response(grant: CitizenGrant) -> dict:
    return {"id": grant.id, "citizen_id": grant.citizen_id, "parcel_entity_id": grant.parcel_entity_id,
            "fields": grant.fields, "expires_at": grant.expires_at, "status": grant.status, "created_at": grant.created_at}


def citizen_case_response(case: CitizenCase) -> dict:
    return {"id": case.id, "grant_id": case.grant_id, "parcel_entity_id": case.parcel_entity_id,
            "category": case.category, "description": case.description, "status": case.status,
            "response": case.response, "created_at": case.created_at}


@app.get("/api/projects/{project_id}/conflicts")
def conflicts(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": c.id, "type": c.conflict_type, "severity": c.severity, "description": c.description,
             "status": c.status, "revision": c.revision, "feature_id": c.feature_id, "dataset_id": c.dataset_id, "details": c.details}
             for c in db.scalars(select(TopologyConflict).where(TopologyConflict.project_id == project_id))]


@app.post("/api/projects/{project_id}/changes/detect")
def changes(project_id: str, payload: ChangeRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    validate_dataset_pair(db, project_id, payload.before_dataset_id, payload.after_dataset_id)
    require_spatial_ready(validate_dataset(db, project_id, payload.before_dataset_id))
    require_spatial_ready(validate_dataset(db, project_id, payload.after_dataset_id))
    try:
        return run_change_detection(db, project_id, payload.before_dataset_id, payload.after_dataset_id, payload.id_fields, payload.geometry_tolerance)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/projects/{project_id}/changes")
def list_changes(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": c.id, "change_type": c.change_type, "boundary_change": c.boundary_change, "status": c.status,
             "source_feature_id": c.source_feature_id, "comparison_feature_id": c.comparison_feature_id,
             "revision": c.revision, "before_geometry": c.before_geometry, "after_geometry": c.after_geometry,
             "before_attributes": c.before_attributes, "after_attributes": c.after_attributes,
             "affected_neighbors": c.affected_neighbors, "evidence": c.evidence}
             for c in db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id))]


@app.post("/api/projects/{project_id}/reviews/{target_type}/{target_id}")
def review(project_id: str, target_type: str, target_id: str, payload: ReviewRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    # Serialize canonical membership mutations for a project, not merely one
    # proposal row: two different proposals may reference the same source.
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    target = {"match": MatchProposal, "change": ChangeProposal, "conflict": TopologyConflict}.get(target_type)
    if not target:
        raise HTTPException(status_code=400, detail="target_type must be match, change, or conflict")
    record = db.get(target, target_id)
    if not record or record.project_id != project_id:
        raise HTTPException(status_code=404, detail="Review target not found")
    current_revision = record.revision or 1
    if payload.expected_revision is not None and payload.expected_revision != current_revision:
        raise HTTPException(status_code=409, detail={"message": "Review target has changed; refresh evidence before retrying",
                                                       "expected_revision": payload.expected_revision,
                                                       "current_revision": current_revision})
    if target_type == "match" and payload.decision == "accepted" and not record.right_feature_id:
        raise HTTPException(status_code=409, detail="An unmatched proposal cannot be accepted as an identity link")
    if record.status in {"accepted", "validated", "rejected", "resolved", "superseded"}:
        raise HTTPException(409, "Decision is final for this revision; create new evidence for a new review")
    if target_type == "conflict" and payload.decision == "accepted":
        record.status = "resolved"
    else:
        record.status = payload.decision
    if not db.execute(update(target).where(target.id == target_id, target.revision == payload.expected_revision)
                      .values(revision=current_revision + 1, status=record.status)
                      .execution_options(synchronize_session=False)).rowcount:
        db.rollback()
        raise HTTPException(409, "Stale review; reload evidence")
    record.revision = current_revision + 1
    db.add(ReviewDecision(project_id=project_id, target_type=target_type, target_id=target_id, actor_id=user.id,
                           decision=payload.decision, rationale=payload.rationale,
                           expected_revision=payload.expected_revision, target_revision=record.revision))
    if target_type == "match" and payload.decision == "accepted":
        try:
            materialize_identity_link(db, record)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    project = db.get(Project, project_id)
    if project:
        project.workflow_revision += 1
        project.validated_revision = None
    audit(db, "review_decision", user.id, project_id, target_type, target_id, {"decision": payload.decision})
    db.commit()
    return {"target_id": target_id, "decision": payload.decision, "status": record.status,
            "revision": record.revision}


@app.get("/api/projects/{project_id}/parcels")
def list_parcels(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    output = []
    for entity in db.scalars(select(ParcelEntity).where(ParcelEntity.project_id == project_id, ParcelEntity.status == "active")):
        members = list(db.scalars(select(ParcelSourceLink.source_feature_id).where(ParcelSourceLink.parcel_entity_id == entity.id)))
        selected = db.scalar(select(ParcelSelection).where(ParcelSelection.parcel_entity_id == entity.id))
        output.append({"id": entity.id, "source_feature_ids": members,
                       "selection": {"geometry_source_id": selected.geometry_source_id, "attribute_source_id": selected.attribute_source_id,
                                     "revision": selected.revision, "rationale": selected.rationale,
                                     "attribute_sources": selected.attribute_sources} if selected else None})
    return output


@app.post("/api/projects/{project_id}/parcels/{parcel_id}/selection")
def select_parcel_baseline(project_id: str, parcel_id: str, payload: SelectionRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    entity = db.get(ParcelEntity, parcel_id)
    if not entity or entity.project_id != project_id or entity.status != "active":
        raise HTTPException(404, "Canonical parcel not found")
    members = set(db.scalars(select(ParcelSourceLink.source_feature_id).where(ParcelSourceLink.parcel_entity_id == parcel_id)))
    if payload.attribute_source_id not in members or (payload.geometry_source_id and payload.geometry_source_id not in members):
        raise HTTPException(422, "Selections must reference linked source features")
    for field, source_id in payload.attribute_sources.items():
        source = db.get(SourceFeature, source_id) if source_id in members else None
        if not source or (field not in source.raw_attributes and field not in (source.canonical_attributes or {})):
            raise HTTPException(422, "Per-field precedence must reference a field present on a linked source")
    if payload.geometry_source_id:
        feature = db.get(SourceFeature, payload.geometry_source_id)
        if feature.status != "processed" or not feature.normalized_geometry:
            raise HTTPException(409, "Geometry source is not spatially ready")
    touch_project(db, project_id)
    selection = db.scalar(select(ParcelSelection).where(ParcelSelection.parcel_entity_id == parcel_id))
    if (selection.revision if selection else 0) != payload.expected_revision:
        raise HTTPException(409, "Baseline selection changed; refresh before retrying")
    if not selection:
        selection = ParcelSelection(parcel_entity_id=parcel_id, revision=0)
        db.add(selection)
    selection.geometry_source_id = payload.geometry_source_id
    selection.attribute_source_id = payload.attribute_source_id
    selection.attribute_sources = payload.attribute_sources
    selection.actor_id, selection.rationale = user.id, payload.rationale
    selection.revision += 1
    audit(db, "baseline_selected", user.id, project_id, "parcel", parcel_id, payload.model_dump())
    db.commit()
    return {"id": parcel_id, "revision": selection.revision}


@app.post("/api/projects/{project_id}/features/{feature_id}/baseline")
def create_singleton_baseline(project_id: str, feature_id: str, payload: SelectionRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    feature = db.get(SourceFeature, feature_id)
    if not feature or db.get(Dataset, feature.dataset_id).project_id != project_id:
        raise HTTPException(404, "Source feature not found")
    link = db.scalar(select(ParcelSourceLink).where(ParcelSourceLink.source_feature_id == feature_id))
    if not link:
        entity = ParcelEntity(project_id=project_id)
        db.add(entity)
        db.flush()
        link = ParcelSourceLink(project_id=project_id, parcel_entity_id=entity.id, source_feature_id=feature_id, link_status="baseline")
        db.add(link)
        db.flush()
    return select_parcel_baseline(project_id, link.parcel_entity_id, payload, db, user)


@app.post("/api/projects/{project_id}/publish")
def publish_version(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    project = db.get(Project, project_id)
    try:
        version = publish(db, project, user)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": version.id, "project_id": version.project_id, "version": version.version_number,
            "lineage_manifest": version.lineage_manifest, "created_at": version.created_at}


@app.post("/api/projects/{project_id}/versions/{version_id}/rollback")
def rollback_version(project_id: str, version_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    source = db.get(PublishedVersion, version_id)
    if not source or source.project_id != project_id:
        raise HTTPException(status_code=404, detail="Published version not found")
    project = db.get(Project, project_id)
    project.workflow_revision += 1
    project.validated_revision = project.workflow_revision
    manifest = {**source.lineage_manifest, "rollback_of_version_id": source.id,
                "rollback_created_at": datetime.now(timezone.utc).isoformat()}
    version = PublishedVersion(project_id=project_id, version_number=next_version(db, project_id), created_by=user.id,
                               project_revision=project.workflow_revision, base_version_id=source.id,
                               validation_report={"valid": True, "type": "traceable_rollback", "source_version": source.id},
                               lineage_manifest=manifest, excluded_records=source.excluded_records)
    db.add(version)
    db.flush()
    for feature in db.scalars(select(PublicationFeature).where(PublicationFeature.version_id == source.id)):
        from shapely.geometry import shape
        from .services import spatial_column
        db.add(PublicationFeature(version_id=version.id, source_feature_id=feature.source_feature_id,
                                  parcel_entity_id=feature.parcel_entity_id, attributes=feature.attributes,
                                  geometry=feature.geometry, lineage={**feature.lineage, "rollback_of_version_id": source.id},
                                  **spatial_column(shape(feature.geometry) if feature.geometry else None, "EPSG:4326")))
    audit(db, "version_rollback", user.id, project_id, "published_version", version.id,
          {"source_version_id": source.id, "version": version.version_number})
    db.commit()
    return {"id": version.id, "project_id": version.project_id, "version": version.version_number,
            "base_version_id": source.id, "lineage_manifest": version.lineage_manifest,
            "created_at": version.created_at}


@app.post("/api/projects/{project_id}/validate")
def validate_project_endpoint(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    return validate_project(db, project_id, user.id)


@app.get("/api/projects/{project_id}/versions")
def versions(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": v.id, "version": v.version_number, "created_at": v.created_at, "lineage_manifest": v.lineage_manifest}
            for v in db.scalars(select(PublishedVersion).where(PublishedVersion.project_id == project_id).order_by(PublishedVersion.version_number.desc()))]


@app.get("/api/projects/{project_id}/versions/{version_id}/export")
def export_version(project_id: str, version_id: str, db: Db, user: CurrentUser, format: str = "geojson", output_crs: str = "EPSG:4326"):
    ensure_project_access(db, project_id, user)
    version = db.get(PublishedVersion, version_id)
    if not version or version.project_id != project_id:
        raise HTTPException(status_code=404, detail="Published version not found")
    features = list(db.scalars(select(PublicationFeature).where(PublicationFeature.version_id == version_id)))
    if any(feature.geometry and not feature.lineage.get("crs_transformations") for feature in features):
        raise HTTPException(status_code=409, detail="Published spatial features do not have CRS transformation lineage")
    if format == "geojson":
        data = {"type": "FeatureCollection", "features": [{"type": "Feature", "id": f.parcel_entity_id or f.id,
                "geometry": f.geometry, "properties": {**f.attributes, "_lineage": f.lineage}} for f in features]}
        return data
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["parcel_entity_id", "source_feature_id", "dataset_id", "sha256", "lineage"])
        writer.writeheader()
        for f in features:
            for source in f.lineage.get("sources", []):
                writer.writerow({"parcel_entity_id": f.parcel_entity_id, "source_feature_id": source["feature_id"],
                                 "dataset_id": source["dataset_id"], "sha256": source["sha256"], "lineage": json.dumps(f.lineage)})
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="geosyncai-v{version.version_number}.csv"'})
    if format == "lineage":
        return {"version": version.version_number, "manifest": version.lineage_manifest,
                "features": [{"id": f.id, "lineage": f.lineage} for f in features]}
    if format == "quality":
        return {"version": version.version_number, "project_revision": version.project_revision,
                "validation_report": version.validation_report, "excluded_records": version.excluded_records}
    if format == "gpkg":
        try:
            from fiona.io import MemoryFile
        except ImportError as exc:
            raise HTTPException(status_code=503, detail="GeoPackage export requires Fiona/GDAL") from exc
        from .spatial import reproject
        from shapely.geometry import mapping, shape
        from pyproj import CRS
        try:
            crs = CRS(output_crs)
        except Exception as exc:
            raise HTTPException(422, "Invalid output CRS") from exc
        with MemoryFile(ext=".gpkg") as memory:
            schema = {"geometry": "Unknown", "properties": {"parcel_entity_id": "str:80",
                                                                  "attributes": "str", "lineage": "str"}}
            with memory.open(driver="GPKG", layer="published_parcels", schema=schema, crs_wkt=crs.to_wkt()) as collection:
                for feature in features:
                    collection.write({"geometry": mapping(reproject(shape(feature.geometry), "EPSG:4326", output_crs)) if feature.geometry else None,
                                      "properties": {"parcel_entity_id": feature.parcel_entity_id or "",
                                                      "attributes": json.dumps(feature.attributes),
                                                      "lineage": json.dumps(feature.lineage)}})
            payload = memory.read()
        return StreamingResponse(iter([payload]), media_type="application/geopackage+sqlite3",
                                 headers={"Content-Disposition": f'attachment; filename="geosyncai-v{version.version_number}.gpkg"'})
    raise HTTPException(status_code=400, detail="format must be geojson, gpkg, csv, lineage, or quality")


@app.get("/api/projects/{project_id}/exports/{format}")
def export_latest(project_id: str, format: str, db: Db, user: CurrentUser, output_crs: str = 'EPSG:4326'):
    """Resolve the latest immutable publication for browser export convenience."""
    ensure_project_access(db, project_id, user)
    version = db.scalar(select(PublishedVersion).where(PublishedVersion.project_id == project_id)
                        .order_by(PublishedVersion.version_number.desc()))
    if not version:
        raise HTTPException(status_code=404, detail="No published version is available for export")
    return export_version(project_id, version.id, db, user, format, output_crs)


@app.get("/api/projects/{project_id}/versions/{version_id}/verify")
def verify_version(project_id: str, version_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    version = db.get(PublishedVersion, version_id)
    if not version or version.project_id != project_id:
        raise HTTPException(404, "Published version not found")
    manifest_bytes = json.dumps(version.lineage_manifest, sort_keys=True, separators=(",", ":"), default=str).encode()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    source_results = []
    for source in version.lineage_manifest.get("features", []):
        for item in source.get("sources", []):
            dataset = db.get(Dataset, item.get("dataset_id"))
            actual = hashlib.sha256(__import__("pathlib").Path(dataset.raw_path).read_bytes()).hexdigest() if dataset and dataset.raw_path and __import__("pathlib").Path(dataset.raw_path).exists() else None
            source_results.append({"dataset_id": item.get("dataset_id"), "expected_sha256": item.get("sha256"),
                                  "actual_sha256": actual, "valid": actual == item.get("sha256")})
    return {"version_id": version.id, "version": version.version_number, "manifest_sha256": manifest_hash,
            "sources": source_results, "valid": all(item["valid"] for item in source_results),
            "immutability_scope": "canonical manifest and original upload bytes; same-database administrators are not excluded"}


@app.get("/api/ogc")
def ogc_landing():
    return {"title": "GeoSyncAI reviewed features", "description": "Read-only reviewed-version feature access",
            "links": [{"rel": "conformance", "href": "/api/ogc/conformance"},
                      {"rel": "data", "href": "/api/ogc/collections"}]}


@app.get("/api/ogc/conformance")
def ogc_conformance():
    return {"conformsTo": ["http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
                            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
                            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/landing-page",
                            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/html"]}


@app.get("/api/ogc/collections")
def ogc_collections(db: Db, user: CurrentUser):
    projects = list_projects(db, user)
    return {"collections": [{"id": project.id, "title": project.name, "itemType": "feature",
                              "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
                              "links": [{"rel": "items", "href": f"/api/ogc/collections/{project.id}/items"}]}
                             for project in projects]}


@app.get("/api/ogc/collections/{project_id}/items")
def ogc_items(project_id: str, db: Db, user: CurrentUser, version_id: str | None = None,
              bbox: str | None = None, limit: int = 100, offset: int = 0):
    ensure_project_access(db, project_id, user)
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    version = db.get(PublishedVersion, version_id) if version_id else db.scalar(select(PublishedVersion).where(
        PublishedVersion.project_id == project_id).order_by(PublishedVersion.version_number.desc()))
    if not version or version.project_id != project_id:
        raise HTTPException(404, "Reviewed version not found")
    features = list(db.scalars(select(PublicationFeature).where(PublicationFeature.version_id == version.id)))
    bounds = None
    if bbox:
        try:
            values = [float(value) for value in bbox.split(",")]
            if len(values) != 4 or values[0] > values[2] or values[1] > values[3]:
                raise ValueError
            bounds = values
        except ValueError as exc:
            raise HTTPException(422, "bbox must be minx,miny,maxx,maxy in CRS84") from exc
    def inside(feature):
        if not bounds or not feature.geometry:
            return True
        from shapely.geometry import shape
        minx, miny, maxx, maxy = shape(feature.geometry).bounds
        return not (maxx < bounds[0] or minx > bounds[2] or maxy < bounds[1] or miny > bounds[3])
    filtered = [feature for feature in features if inside(feature)]
    page = filtered[offset:offset + limit]
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "id": feature.parcel_entity_id or feature.id,
             "geometry": feature.geometry, "properties": {**feature.attributes, "_lineage": feature.lineage}}
            for feature in page], "numberMatched": len(filtered), "numberReturned": len(page),
            "links": ([{"rel": "next", "href": f"/api/ogc/collections/{project_id}/items?version_id={version.id}&limit={limit}&offset={offset + limit}"}]
                      if offset + limit < len(filtered) else [])}


@app.post("/api/projects/{project_id}/jobs")
def create_job(project_id: str, job_type: str, payload: dict, background: BackgroundTasks, db: Db, user: CurrentUser,
               idempotency_key: str | None = None):
    ensure_project_access(db, project_id, user, write=True)
    if job_type not in {"match", "topology", "change_detection"}:
        raise HTTPException(status_code=400, detail="Unsupported job type")
    policy = processing_policy(db, project_id)
    payload = {**{key: policy[key] for key in ('max_distance','ambiguity_margin','geometry_tolerance')}, **payload,
               'policy_version': policy['version']}
    validate_job_payload(db, project_id, job_type, payload)
    configuration_hash = job_configuration_hash(db, job_type, payload)
    idempotency_key = idempotency_key or configuration_hash
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.project_id == project_id, Job.idempotency_key == idempotency_key))
        if existing:
            if existing.configuration_hash != configuration_hash:
                raise HTTPException(409, "Idempotency key refers to different inputs or configuration")
            return job_response(existing)
    job = Job(project_id=project_id, job_type=job_type, payload=payload, created_by=user.id, idempotency_key=idempotency_key)
    job.configuration_hash = configuration_hash
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Job).where(Job.project_id == project_id, Job.idempotency_key == idempotency_key))
        if not existing or existing.configuration_hash != configuration_hash:
            raise HTTPException(409, "Job creation conflicted with another request")
        return job_response(existing)
    dispatch_job(job.id, background)
    return job_response(job)


@app.post("/api/projects/{project_id}/jobs/{job_id}/retry")
def retry_job(project_id: str, job_id: str, background: BackgroundTasks, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    job = db.get(Job, job_id)
    if not job or job.project_id != project_id:
        raise HTTPException(404, "Job not found")
    if job.status != "failed":
        raise HTTPException(409, "Only failed jobs can be retried")
    validate_job_payload(db, project_id, job.job_type, job.payload)
    job.status = "queued"
    job.stage = "queued"
    job.cancellation_requested = False
    job.error = None
    db.commit()
    dispatch_job(job_id, background)
    return job_response(job)


@app.post("/api/projects/{project_id}/jobs/{job_id}/cancel")
def cancel_job(project_id: str, job_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    job = db.get(Job, job_id)
    if not job or job.project_id != project_id:
        raise HTTPException(404, "Job not found")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(409, "Job is already final")
    job.cancellation_requested = True
    job.warnings = [*(job.warnings or []), "Cancellation requested; transactional stage will not publish partial effects"]
    db.commit()
    return job_response(job)


@app.get("/api/projects/{project_id}/jobs/{job_id}")
def get_job(project_id: str, job_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    job = db.get(Job, job_id)
    if not job or job.project_id != project_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_response(job)


def job_response(job: Job) -> dict:
    return {"id": job.id, "job_type": job.job_type, "status": job.status, "result": job.result, "error": job.error,
            "configuration_hash": job.configuration_hash, "attempts": job.attempts,
            "stage": job.stage, "progress": job.progress, "heartbeat_at": job.heartbeat_at,
            "warnings": job.warnings, "cancellation_requested": job.cancellation_requested,
            "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at}


def validate_dataset(db: Session, project_id: str, dataset_id: str) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


def validate_dataset_pair(db: Session, project_id: str, left_id: str, right_id: str) -> None:
    if left_id == right_id:
        raise HTTPException(422, "Choose distinct source datasets")
    validate_dataset(db, project_id, left_id)
    validate_dataset(db, project_id, right_id)


def require_spatial_ready(dataset: Dataset) -> None:
    report = dataset.validation_report or {}
    has_spatial_input = bool(dataset.geometry_type or report.get("has_geometry") or report.get("spatial_records") or
                             report.get("format") in {"geojson", "shp_zip", "geopackage"})
    if has_spatial_input and dataset.normalized_crs != "EPSG:4326":
        raise HTTPException(status_code=409,
                            detail=f"Dataset '{dataset.name}' requires explicit CRS confirmation before spatial processing")


def validate_job_payload(db: Session, project_id: str, job_type: str, payload: dict) -> None:
    from pydantic import ValidationError
    try:
        if job_type == "topology":
            dataset = validate_dataset(db, project_id, payload["dataset_id"])
            require_spatial_ready(dataset)
        elif job_type == "match":
            MatchRequest.model_validate(payload)
            validate_dataset_pair(db, project_id, payload["left_dataset_id"], payload["right_dataset_id"])
            require_spatial_ready(validate_dataset(db, project_id, payload["left_dataset_id"]))
            require_spatial_ready(validate_dataset(db, project_id, payload["right_dataset_id"]))
        else:
            ChangeRequest.model_validate(payload)
            validate_dataset_pair(db, project_id, payload["before_dataset_id"], payload["after_dataset_id"])
            require_spatial_ready(validate_dataset(db, project_id, payload["before_dataset_id"]))
            require_spatial_ready(validate_dataset(db, project_id, payload["after_dataset_id"]))
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Missing job payload field: {exc.args[0]}") from exc
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
