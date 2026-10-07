from __future__ import annotations

import csv
import hashlib
import io
import json
import struct
import zlib
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from .auth import (CurrentUser, ensure_project_access, ensure_project_membership, hash_password, verify_password,
                   create_access_token, project_member, utc_expired, capability_allowed, DUMMY_PASSWORD_HASH, password_needs_rehash)
from .config import get_settings
from .db import get_db, init_db
from .models import (Dataset, Job, MatchProposal, Project, ProjectMember, ReviewDecision, SourceFeature,
                     TopologyConflict, User, ChangeProposal, PublishedVersion, PublicationFeature, SchemaMapping,
                     ParcelEntity, ParcelSourceLink, ParcelSelection, MappingDictionaryEntry, DepartmentTemplate,
                     ReconciliationCase, GeometryChangeSet, GroundControlSession, TrainingExample, ModelArtifact,
                     FieldAssignment, FieldEvidence, ComplianceRule, CitizenGrant, CitizenCase, RasterAsset, uid, AuditEvent)
from .schemas import (CRSConfirmation, ChangeRequest, DatasetRegister, LoginRequest, MatchRequest, MemberCreate,
                       ProjectCreate, ProjectOut, ReviewRequest, SchemaMappingRequest, SelectionRequest, PolicyRequest, Token,
                       DatasetMetadataRequest, MappingDictionaryRequest, DepartmentTemplateRequest, ReconciliationRequest,
                       ReconciliationDecision, GeometryChangeSetRequest, GeometryDraftRequest, GeometryDecision, MeasurementRequest,
                        GroundControlRequest, GroundControlApprovalRequest, TrainingExampleRequest, RankerTrainRequest, RankerActivationRequest, AssignmentRequest, AssignmentUpdateRequest,
                       FieldEvidenceRequest, FieldEvidenceResolutionRequest, QueryRequest, ComplianceRuleRequest, ComplianceEvaluateRequest,
                        CitizenGrantRequest, CitizenGrantUpdateRequest, CitizenCaseRequest, CitizenCaseResponseRequest)
from .services import (audit, bootstrap_synthetic, confirm_dataset_crs, ingest_dataset, materialize_identity_link,
                       run_change_detection, run_matching, run_topology, administrative_context, touch_project, next_version,
                       invalidate_dataset_evidence)
from .publication import canonical_output_context, canonical_sha256, publish, validate_project
from .policy import processing_policy
from .tasks import dispatch_job, recover_jobs, configuration_hash as job_configuration_hash
from .advanced import (build_reconciliation, compliance_result, geometry_measurements, resolve_canonical_geometry, validate_compliance_rule,
                       measure_geometry, train_ranker)
from .querying import execute_query, readable_dataset_ids
from .ground_control import fit_session, control_coverage
from .geometry_editing import preview_geometry
from .advanced import geometry_submission_gates
from .security import SecurityMiddleware, TrafficGuard, TrafficUnavailable
from .account_security import router as account_security_router


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
    if "*" in settings.allowed_hosts or "*" in settings.cors_origins:
        raise RuntimeError("Configure explicit ALLOWED_HOSTS and CORS_ORIGINS; wildcards are forbidden")
    if settings.production_mode:
        if settings.demo_mode or settings.auto_bootstrap or not settings.rate_limit_enabled:
            raise RuntimeError("Production requires traffic protection and disabled demo/auto-bootstrap modes")
        if not (settings.security_redis_url or settings.celery_broker_url):
            raise RuntimeError("Production requires Redis-backed shared traffic limits")
        if any(not origin.startswith('https://') for origin in settings.cors_origins):
            raise RuntimeError("Production CORS origins must use HTTPS, or an empty list for same-origin only")
        if len(set(settings.jwt_secret)) < 12:
            raise RuntimeError("Generate a random production JWT secret; repetitive values are rejected")
        traffic_guard.store.client.ping()
    init_db()
    if get_settings().auto_bootstrap:
        db = next(get_db())
        try:
            bootstrap(db)
        finally:
            db.close()
    if settings.production_mode:
        with next(get_db()) as db:
            for username, password in DEMO_USERS.items():
                account = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
                if account and verify_password(password, account.password_hash):
                    raise RuntimeError("Disable or replace active demonstration passwords before production startup")
    recover_jobs()
    yield


app_settings = get_settings()
traffic_guard = TrafficGuard(app_settings)
app = FastAPI(title="GeoSyncAI API", version="0.1.0", lifespan=lifespan,
              docs_url="/docs" if app_settings.api_docs_enabled else None,
              redoc_url=None, openapi_url="/openapi.json" if app_settings.api_docs_enabled else None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=app_settings.allowed_hosts)
app.add_middleware(SecurityMiddleware, guard=traffic_guard)
app.add_middleware(CORSMiddleware, allow_origins=app_settings.cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type"], expose_headers=["Retry-After", "X-Request-ID"])
app.include_router(account_security_router)


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
def login(payload: LoginRequest, db: Db, request: Request):
    if get_settings().rate_limit_enabled:
        try:
            allowed, retry = request.state.traffic_guard.consume("login-account", payload.username.casefold(),
                get_settings().account_login_requests_per_window, get_settings().account_login_window_seconds)
        except TrafficUnavailable as exc:
            raise HTTPException(503, "Login protection temporarily unavailable", headers={"Retry-After": "5"}) from exc
        if not allowed:
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(retry)})
    user = db.scalar(select(User).where(User.username == payload.username))
    valid = verify_password(payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if not user or not user.is_active or not valid:
        audit(db, "authentication_failed", None, details={"request_id": request.state.request_id})
        db.commit()
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if password_needs_rehash(user.password_hash):
        changed = db.execute(update(User).where(User.id == user.id, User.auth_version == user.auth_version,
            User.password_hash == user.password_hash).values(password_hash=hash_password(payload.password)))
        if changed.rowcount != 1:
            raise HTTPException(409, "Account changed; sign in again")
    audit(db, "authentication_succeeded", user.id)
    db.commit()
    return {"access_token": create_access_token(user), "user": {"id": user.id, "username": user.username, "role": user.role}}


@app.get("/api/auth/me")
def me(user: CurrentUser):
    return {"id": user.id, "username": user.username, "role": user.role}


@app.get("/api/projects/{project_id}/capabilities")
def capabilities(project_id: str, db: Db, user: CurrentUser):
    project = ensure_project_membership(db, project_id, user)
    member = project_member(db, project_id, user.id)
    role = user.role
    departmental = capability_allowed(user, member, "departmental")
    process = departmental and (role == "admin" or (role in {"processor", "reviewer", "steward"}
                   and member and member.project_role in {"processor", "reviewer", "steward", "owner"}))
    review = departmental and (role == "admin" or (role in {"reviewer", "steward"}
                   and member and member.project_role in {"reviewer", "steward", "owner"}))
    return {"project_id": project.id, "role": role, "project_role": member.project_role if member else "admin",
            "read_project": True, "read_departmental": departmental,
            "process": bool(process), "review": bool(review), "publish": bool(review),
            "fieldwork": capability_allowed(user, member, "fieldwork"),
            "citizen_records": capability_allowed(user, member, "citizen"), "exports": departmental}


@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects(db: Db, user: CurrentUser):
    if user.role == "admin":
        return list(db.scalars(select(Project).order_by(Project.created_at.desc())))
    return list(db.scalars(select(Project).join(ProjectMember, ProjectMember.project_id == Project.id)
                           .where(ProjectMember.user_id == user.id).order_by(Project.created_at.desc())))


@app.post("/api/projects", response_model=ProjectOut)
def create_project(payload: ProjectCreate, db: Db, user: CurrentUser):
    if user.role not in {"admin", "processor", "reviewer", "steward"}:
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
    return ensure_project_membership(db, project_id, user)


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
            for m in db.scalars(select(SchemaMapping).where(SchemaMapping.project_id == project_id, SchemaMapping.status == 'confirmed', SchemaMapping.dataset_id.in_(readable_dataset_ids(db, project_id, user))))]


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


@app.get("/api/projects/{project_id}/members")
def list_members(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    rows = db.execute(select(User, ProjectMember.project_role).join(ProjectMember, ProjectMember.user_id == User.id)
                      .where(ProjectMember.project_id == project_id).order_by(User.username)).all()
    return [{"id": target.id, "username": target.username, "role": target.role, "project_role": project_role}
            for target, project_role in rows]


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
def upload_dataset(project_id: str, db: Db, user: CurrentUser, file: UploadFile = File(...),
                         name: str | None = Form(None), source_organization: str | None = Form(None),
                         capture_date: date | None = Form(None), declared_crs: str | None = Form(None),
                          parent_dataset_id: str | None = Form(None), license_classification: str | None = Form(None),
                          source_version: str | None = Form(None), version_label: str | None = Form(None),
                          administrative_namespace: str | None = Form(None), access_classification: str = Form("internal"),
                          accuracy_metadata: str | None = Form(None), provenance: str | None = Form(None)):
    ensure_project_access(db, project_id, user, write=True)
    # Native format/geometry work belongs in FastAPI's bounded thread pool,
    # leaving the event loop available to reject excess traffic and serve reads.
    data = file.file.read(get_settings().max_upload_bytes + 1)
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
    if access_classification not in {"public", "internal", "restricted"}:
        raise HTTPException(422, "Invalid source access classification")
    try:
        accuracy = json.loads(accuracy_metadata) if accuracy_metadata else {}
        source_provenance = json.loads(provenance) if provenance else {}
        if not isinstance(accuracy, dict) or not isinstance(source_provenance, dict):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "Accuracy metadata and provenance must be JSON objects") from exc
    dataset = Dataset(project_id=project_id, name=name or file.filename or "uploaded dataset",
                      source_organization=source_organization, capture_date=capture_date, declared_crs=declared_crs,
                      parent_dataset_id=parent_dataset_id, license_classification=license_classification,
                      source_version=source_version, version_label=version_label,
                      administrative_namespace=namespace, access_classification=access_classification,
                      accuracy_metadata=accuracy, provenance=source_provenance)
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
    allowed = readable_dataset_ids(db, project_id, user)
    return [dataset_response(d) for d in db.scalars(select(Dataset).where(Dataset.project_id == project_id, Dataset.id.in_(allowed)).order_by(Dataset.uploaded_at.desc()))]


@app.patch("/api/projects/{project_id}/datasets/{dataset_id}/metadata")
def update_dataset_metadata(project_id: str, dataset_id: str, payload: DatasetMetadataRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = validate_dataset(db, project_id, dataset_id)
    if dataset.id not in readable_dataset_ids(db, project_id, user):
        raise HTTPException(403, "Restricted source requires project reviewer permission")
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
def upload_raster_asset(project_id: str, db: Db, user: CurrentUser, file: UploadFile = File(...),
                              name: str | None = Form(None), source_crs: str | None = Form(None),
                              attribution: str | None = Form(None), access_classification: str = Form("internal"),
                              source_date: date | None = Form(None)):
    ensure_project_access(db, project_id, user, write=True)
    if access_classification not in {"public", "internal", "restricted"}:
        raise HTTPException(422, "Invalid raster access classification")
    data = file.file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(413, "Raster exceeds the configured upload limit")
    if data[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise HTTPException(422, "Raster inspection accepts GeoTIFF/COG bytes only")
    try:
        from rasterio.io import MemoryFile
        with MemoryFile(data) as memory:
            with memory.open() as source:
                if source.driver not in {"GTiff", "COG"}:
                    raise ValueError("Raster must be a GeoTIFF or Cloud Optimized GeoTIFF")
                actual_crs = source.crs.to_string() if source.crs else None
                if actual_crs and source_crs and actual_crs != source_crs:
                    raise ValueError("Supplied raster CRS does not match GeoTIFF CRS metadata")
                resolved_crs = actual_crs or source_crs
                metadata = {"driver": source.driver, "width": source.width, "height": source.height,
                            "count": source.count, "dtype": list(source.dtypes), "nodata": source.nodata,
                            "transform": list(source.transform), "bounds": [source.bounds.left, source.bounds.bottom,
                                                                             source.bounds.right, source.bounds.top],
                            "resolution": list(source.res), "crs": resolved_crs,
                            "crs_from_file": bool(actual_crs), "inspection": "rasterio_geotiff_metadata"}
                raster_bounds = list(metadata["bounds"])
    except ImportError as exc:
        raise HTTPException(503, "GeoTIFF inspection requires the pinned rasterio runtime") from exc
    except Exception as exc:
        raise HTTPException(422, f"Invalid GeoTIFF: {exc}") from exc
    asset = RasterAsset(project_id=project_id, name=name or file.filename or "raster.tif", content_hash=hashlib.sha256(data).hexdigest(),
                        source_crs=resolved_crs, bounds=raster_bounds, mime_type=file.content_type or "image/tiff",
                        attribution=attribution, access_classification=access_classification,
                        source_date=source_date, metadata_json={**metadata, "bytes": len(data)},
                        status="registered" if resolved_crs else "needs_crs_review",
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
        RasterAsset.project_id == project_id).order_by(RasterAsset.created_at.desc()))
            if asset.access_classification != "restricted" or capabilities(project_id, db, user)["review"]]


@app.get("/api/projects/{project_id}/raster-assets/{asset_id}/bytes")
def download_raster_asset(project_id: str, asset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    asset = db.get(RasterAsset, asset_id)
    if not asset or asset.project_id != project_id:
        raise HTTPException(404, "Raster asset not found")
    if asset.access_classification == "restricted" and not capabilities(project_id, db, user)["review"]:
        raise HTTPException(403, "Restricted raster requires steward or reviewer permission")
    path = Path(asset.raw_path)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != asset.content_hash:
        raise HTTPException(409, "Raster integrity verification failed")
    return StreamingResponse(open(path, "rb"), media_type=asset.mime_type,
                             headers={"Content-Disposition": f'attachment; filename="{asset.name}"',
                                       "X-GeoSyncAI-Attribution": asset.attribution or ""})


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def _raster_png(path: str) -> bytes:
    import numpy as np
    import rasterio
    with rasterio.open(path) as source:
        width, height = min(source.width, 512), min(source.height, 512)
        count = min(source.count, 3)
        values = source.read(indexes=list(range(1, count + 1)), out_shape=(count, height, width), masked=True).filled(0).astype("float64")
    if count == 1:
        low, high = float(np.nanmin(values)), float(np.nanmax(values))
        scale = 255.0 / (high - low) if high > low else 1.0
        pixels = np.clip((values[0] - low) * scale, 0, 255).astype("uint8")
        color_type, rows = 0, b"".join(b"\x00" + row.tobytes() for row in pixels)
    else:
        if count == 2:
            values = np.concatenate([values, values[:1]], axis=0)
        low, high = float(np.nanmin(values)), float(np.nanmax(values))
        scale = 255.0 / (high - low) if high > low else 1.0
        pixels = np.clip((values - low) * scale, 0, 255).astype("uint8").transpose(1, 2, 0)
        color_type, rows = 2, b"".join(b"\x00" + row.tobytes() for row in pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows)) + _png_chunk(b"IEND", b"")


@app.get("/api/projects/{project_id}/raster-assets/{asset_id}/preview")
def preview_raster_asset(project_id: str, asset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    asset = db.get(RasterAsset, asset_id)
    if not asset or asset.project_id != project_id:
        raise HTTPException(404, "Raster asset not found")
    if asset.access_classification == "restricted" and not capabilities(project_id, db, user)["review"]:
        raise HTTPException(403, "Restricted raster requires steward or reviewer permission")
    if not asset.source_crs:
        raise HTTPException(409, "Raster CRS is unresolved; preview is blocked until it is confirmed")
    path = Path(asset.raw_path)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != asset.content_hash:
        raise HTTPException(409, "Raster integrity verification failed")
    try:
        return StreamingResponse(iter([_raster_png(str(path))]), media_type="image/png",
                                 headers={"X-GeoSyncAI-Source-CRS": asset.source_crs,
                                          "X-GeoSyncAI-Bounds": json.dumps(asset.bounds or [])})
    except ImportError as exc:
        raise HTTPException(503, "Raster preview requires the pinned rasterio runtime") from exc
    except Exception as exc:
        raise HTTPException(422, f"Raster preview failed: {exc}") from exc


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
    if dataset.id not in readable_dataset_ids(db, project_id, user):
        raise HTTPException(403, "Restricted source requires project reviewer permission")
    try:
        report = confirm_dataset_crs(db, dataset, payload.crs, user.id, payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**dataset_response(dataset), "validation_report": report}


@app.get("/api/projects/{project_id}/datasets/{dataset_id}/mapping")
def get_schema_mapping(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    dataset = validate_dataset(db, project_id, dataset_id)
    if dataset.id not in readable_dataset_ids(db, project_id, user):
        raise HTTPException(403, "Restricted source requires project reviewer permission")
    mapping = db.scalar(select(SchemaMapping).where(SchemaMapping.dataset_id == dataset.id)
                        .order_by(SchemaMapping.version.desc()))
    if not mapping:
        fields = (dataset.validation_report or {}).get("schema_fields", [])
        aliases = {"parcel_id": ["parcel_id", "plot_id", "khasra_no"], "survey_number": ["survey_number", "survey_no"],
                   "property_account": ["property_id", "account_no"], "village": ["village", "village_name"],
                   "village_code": ["village_code", "admin_code"], "recorded_area": ["area", "recorded_area", "area_m2", "area_sq_m", "recorded_area_m2"]}
        suggested = {key: field for key, names in aliases.items() for field in fields if field.casefold() in names}
        dictionary = list(db.scalars(select(MappingDictionaryEntry).where(
            MappingDictionaryEntry.project_id == project_id, MappingDictionaryEntry.status == "confirmed")
            .order_by(MappingDictionaryEntry.version.desc())))
        for entry in dictionary:
            for field in fields:
                if field.casefold() in {entry.source_term.casefold(), entry.normalized_term.casefold()}:
                    suggested.setdefault(entry.canonical_field, field)
        templates = list(db.scalars(select(DepartmentTemplate).where(
            DepartmentTemplate.project_id == project_id, DepartmentTemplate.status == "confirmed")
            .order_by(DepartmentTemplate.version.desc())))
        for template in templates:
            for canonical, source_field in (template.mapping or {}).items():
                if isinstance(source_field, str) and source_field in fields:
                    suggested.setdefault(canonical, source_field)
        return {"version": 0, "status": "draft", "mapping": suggested,
                "source_fields": (dataset.validation_report or {}).get('schema_summary', [{"name": field} for field in fields]),
                "suggestion_evidence": {"dictionary_entry_ids": [entry.id for entry in dictionary],
                                        "department_template_ids": [template.id for template in templates]}}
    return {"id": mapping.id, "version": mapping.version, "status": mapping.status,
            "mapping": mapping.mapping, "source_fields": mapping.source_fields,
            "created_at": mapping.created_at}


@app.post("/api/projects/{project_id}/datasets/{dataset_id}/mapping")
def save_schema_mapping(project_id: str, dataset_id: str, payload: SchemaMappingRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = validate_dataset(db, project_id, dataset_id)
    if dataset.id not in readable_dataset_ids(db, project_id, user):
        raise HTTPException(403, "Restricted source requires project reviewer permission")
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
    if dataset.id not in readable_dataset_ids(db, project_id, user):
        raise HTTPException(403, "Restricted source features require steward or reviewer permission")
    limit = max(1, min(limit, 1000)); offset = max(0, offset)
    bounds = None
    if bbox:
        try:
            values = [float(value) for value in bbox.split(",")]
            if len(values) != 4 or values[0] > values[2] or values[1] > values[3]: raise ValueError
            bounds = values
        except ValueError as exc:
            raise HTTPException(422, "bbox must contain four numeric CRS84 coordinates") from exc
    feature_query = select(SourceFeature).where(SourceFeature.dataset_id == dataset_id).order_by(SourceFeature.id)
    database_bbox = bool(bounds and hasattr(SourceFeature, "spatial_geometry"))
    if database_bbox:
        feature_query = feature_query.where(
            func.ST_Intersects(SourceFeature.spatial_geometry, func.ST_MakeEnvelope(*bounds, 4326))
        )
    features = list(db.scalars(feature_query.offset(offset).limit(limit) if (not bounds or database_bbox)
                              else feature_query))
    if bounds and not database_bbox:
        from shapely.geometry import shape
        features = [feature for feature in features if feature.normalized_geometry and not (
            shape(feature.normalized_geometry).bounds[2] < bounds[0] or shape(feature.normalized_geometry).bounds[0] > bounds[2] or
            shape(feature.normalized_geometry).bounds[3] < bounds[1] or shape(feature.normalized_geometry).bounds[1] > bounds[3])]
    page = features if database_bbox or not bounds else features[offset:offset + limit]
    return [{"id": f.id, "original_id": f.original_id, "attributes": f.raw_attributes,
             "original_geometry": f.original_geometry, "geometry": f.normalized_geometry,
             "administrative_context": f.administrative_context, "canonical_attributes": f.canonical_attributes, "status": f.status,
             "processing_reason": f.processing_reason}
              for f in page]


@app.get("/api/projects/{project_id}/datasets/{dataset_id}/raw")
def download_raw(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id or not dataset.raw_path:
        raise HTTPException(status_code=404, detail="Raw file not found")
    if dataset.id not in readable_dataset_ids(db, project_id, user):
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
    ensure_readable_inputs(db, project_id, user, [payload.left_dataset_id, payload.right_dataset_id])
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
    ensure_readable_inputs(db, project_id, user, [dataset_id])
    validate_dataset(db, project_id, dataset_id)
    require_spatial_ready(validate_dataset(db, project_id, dataset_id))
    return run_topology(db, project_id, dataset_id)


@app.get("/api/projects/{project_id}/matches")
def matches(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": p.id, "left_feature_id": p.left_feature_id, "right_feature_id": p.right_feature_id,
             "score": p.score, "score_type": p.score_type, "status": p.status, "revision": p.revision,
             "evidence": p.evidence}
            for p in db.scalars(select(MatchProposal).where(MatchProposal.project_id == project_id))
            if source_ids_readable(db, project_id, user, [p.left_feature_id, p.right_feature_id])]


@app.post("/api/projects/{project_id}/reconciliations")
def create_reconciliation(project_id: str, payload: ReconciliationRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    if not source_ids_readable(db, project_id, user, payload.source_feature_ids):
        raise HTTPException(403, "Reconciliation contains restricted or unavailable source evidence")
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
        ReconciliationCase.project_id == project_id).order_by(ReconciliationCase.created_at.desc()))
            if source_ids_readable(db, project_id, user, case.source_feature_ids)]


@app.post("/api/projects/{project_id}/reconciliations/{case_id}/decision")
def decide_reconciliation(project_id: str, case_id: str, payload: ReconciliationDecision, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    case = db.get(ReconciliationCase, case_id)
    if not case or case.project_id != project_id:
        raise HTTPException(404, "Reconciliation case not found")
    if case.revision != payload.expected_revision or case.status in {"accepted", "rejected"}:
        raise HTTPException(409, "Reconciliation case is stale or final")
    if payload.overrides:
        raise HTTPException(422, "Choose evidenced per-field sources; arbitrary value invention is not supported")
    if payload.decision == "accepted":
        sources = [db.get(SourceFeature, source_id) for source_id in case.source_feature_ids]
        for source in sources:
            if not source or db.get(Dataset, source.dataset_id).project_id != project_id or source.status != "processed":
                raise HTTPException(409, "Reconciliation references an unavailable project source")
        active_links = list(db.scalars(select(ParcelSourceLink).join(ParcelEntity,
                    ParcelEntity.id == ParcelSourceLink.parcel_entity_id).where(
                    ParcelSourceLink.project_id == project_id,
                    ParcelSourceLink.source_feature_id.in_(case.source_feature_ids), ParcelEntity.status == "active")))
        existing_entities = {link.parcel_entity_id for link in active_links}
        if len(existing_entities) > 1:
            raise HTTPException(409, "Sources already belong to different active parcels; explicit grouped identity review is required")
        if existing_entities:
            entity = db.get(ParcelEntity, next(iter(existing_entities)))
            current_sources = set(db.scalars(select(ParcelSourceLink.source_feature_id).where(ParcelSourceLink.parcel_entity_id == entity.id)))
            if current_sources - set(case.source_feature_ids):
                raise HTTPException(409, "Case omits existing identity evidence; include it before extending membership")
        else:
            entity = ParcelEntity(project_id=project_id)
            db.add(entity); db.flush()
        linked = {link.source_feature_id for link in active_links}
        for source_id in case.source_feature_ids:
            if source_id not in linked:
                db.add(ParcelSourceLink(project_id=project_id, parcel_entity_id=entity.id,
                                       source_feature_id=source_id, link_status="reconciliation"))
        if payload.approve_selection:
            if payload.attribute_source_id not in case.source_feature_ids or (payload.geometry_source_id
                            and payload.geometry_source_id not in case.source_feature_ids):
                raise HTTPException(422, "Explicit selections must reference this case's evidenced sources")
            if not set(payload.attribute_sources.values()).issubset(set(case.source_feature_ids)):
                raise HTTPException(422, "Per-field choices must reference this case's sources")
            geometry_source = db.get(SourceFeature, payload.geometry_source_id) if payload.geometry_source_id else None
            if geometry_source and not geometry_source.normalized_geometry:
                raise HTTPException(409, "Selected boundary source has no reviewed CRS/geometry")
            selection = db.scalar(select(ParcelSelection).where(ParcelSelection.parcel_entity_id == entity.id))
            if selection:
                raise HTTPException(409, "Parcel already has a reviewed selection; change it through revisioned selection review")
            db.add(ParcelSelection(parcel_entity_id=entity.id, geometry_source_id=payload.geometry_source_id,
                                   attribute_source_id=payload.attribute_source_id, attribute_sources=payload.attribute_sources,
                                   actor_id=user.id, rationale=payload.rationale))
        case.recommendation = {**case.recommendation, "canonical_parcel_id": entity.id,
                                "selection_explicitly_approved": payload.approve_selection}
    case.status = payload.decision
    case.rationale = payload.rationale
    case.recommendation = {**case.recommendation, "authorized_overrides": payload.overrides}
    case.revision += 1
    audit(db, "reconciliation_decision", user.id, project_id, "reconciliation", case.id,
          {"decision": payload.decision, "overrides": payload.overrides})
    touch_project(db, project_id)
    db.commit()
    return reconciliation_response(case)


def reconciliation_response(case: ReconciliationCase) -> dict:
    return {"id": case.id, "anchor_feature_id": case.anchor_feature_id, "source_feature_ids": case.source_feature_ids,
            "status": case.status, "revision": case.revision, "recommendation": case.recommendation,
            "competing_values": case.competing_values, "evidence": case.evidence, "rationale": case.rationale}


@app.post("/api/projects/{project_id}/geometry-drafts")
def geometry_draft_preview(project_id: str, payload: GeometryDraftRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    try:
        return preview_geometry(db, project_id, user, payload)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/geometry-changes")
def create_geometry_changeset(project_id: str, payload: GeometryChangeSetRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    entities = list(db.scalars(select(ParcelEntity).where(ParcelEntity.project_id == project_id,
                                                          ParcelEntity.id.in_(payload.parcel_entity_ids),
                                                          ParcelEntity.status == "active")))
    if len(entities) != len(set(payload.parcel_entity_ids)):
        raise HTTPException(404, "Every parcel in a geometry changeset must belong to this project")
    if not parcel_ids_readable(db, project_id, user, payload.parcel_entity_ids):
        raise HTTPException(403, "Participating parcels contain restricted evidence")
    if payload.successor_ids:
        raise HTTPException(422, "Successor identities are server-generated and cannot be supplied by the client")
    if payload.operation == "split" and len(payload.parcel_entity_ids) != 1:
        raise HTTPException(422, "A split has exactly one approved parent parcel")
    if payload.operation in {"merge", "shared_edge"} and len(payload.parcel_entity_ids) < 2:
        raise HTTPException(422, "This operation requires at least two affected parcel identities")
    if payload.operation in {"move", "shared_edge"} and set(payload.draft_geometries) != set(payload.parcel_entity_ids):
        raise HTTPException(422, "Draft geometry keys must exactly match affected parcel identities")
    if payload.operation == "split" and len(payload.draft_geometries) < 2:
        raise HTTPException(422, "A split requires at least two proposed child geometries")
    if payload.operation == "merge" and len(payload.draft_geometries) != 1:
        raise HTTPException(422, "A merge requires exactly one proposed successor geometry")
    if payload.operation in {"split", "merge"} and not payload.attribute_source_id:
        raise HTTPException(422, "Split/merge requires an explicit reviewed attribute source")
    if payload.attribute_source_id:
        predecessor_members = set(db.scalars(select(ParcelSourceLink.source_feature_id).where(
            ParcelSourceLink.project_id == project_id,
            ParcelSourceLink.parcel_entity_id.in_(payload.parcel_entity_ids))))
        if payload.attribute_source_id not in predecessor_members:
            raise HTTPException(422, "Attribute source must be linked to an affected predecessor parcel")
        attribute_source = db.get(SourceFeature, payload.attribute_source_id)
        attribute_dataset = db.get(Dataset, attribute_source.dataset_id) if attribute_source else None
        if not attribute_source or not attribute_dataset or attribute_dataset.project_id != project_id:
            raise HTTPException(422, "Attribute source must belong to the affected project")
    member_source_ids = set(db.scalars(select(ParcelSourceLink.source_feature_id).where(
        ParcelSourceLink.project_id == project_id,
        ParcelSourceLink.parcel_entity_id.in_(payload.parcel_entity_ids))))
    if not set(payload.attribute_sources.values()).issubset(member_source_ids):
        raise HTTPException(422, "Every attribute precedence source must be linked to an affected parcel")
    for field, source_id in payload.attribute_sources.items():
        source = db.get(SourceFeature, source_id)
        if not source or field not in {**source.raw_attributes, **(source.canonical_attributes or {})}:
            raise HTTPException(422, "Attribute precedence field must exist on the selected source")
    if payload.attribute_overrides:
        raise HTTPException(422, "Geometry changes cannot silently override source attributes; select per-field evidence")
    try:
        measurements = geometry_measurements(db, payload.parcel_entity_ids, payload.draft_geometries)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if payload.operation in {"split", "merge"} and abs(measurements["area_conservation_delta_m2"]) > 0.01:
        raise HTTPException(422, f"{payload.operation.title()} draft does not conserve source area within 0.01 m²")
    if payload.operation in {"split", "merge"} and (
            measurements["partition_difference_m2"] > 0.01 or measurements["draft_overlap_m2"] > 0.01):
        raise HTTPException(422, "Split/merge must preserve predecessor coverage without overlapping children")
    if payload.expected_geometry_fingerprint and payload.expected_geometry_fingerprint != canonical_sha256(measurements["before_geometries"]):
        raise HTTPException(409, "Approved boundaries changed after preview; reload and preview again")
    gates = geometry_submission_gates(payload.operation, measurements, processing_policy(db, project_id))
    if gates:
        raise HTTPException(422, {"geometry_gates": gates, "affected_neighbors": measurements["affected_neighbors"]})
    change_id = uid()
    draft_geometries = dict(payload.draft_geometries)
    successor_ids = list(payload.successor_ids)
    measurements["before_fingerprint"] = canonical_sha256(measurements["before_geometries"])
    measurements["policy"] = processing_policy(db, project_id)
    if payload.operation in {"split", "merge"}:
        successor_ids = [uid() for _ in draft_geometries]
        draft_geometries = {successor_id: geometry for successor_id, geometry in zip(successor_ids, draft_geometries.values())}
        for successor_id in successor_ids:
            db.add(ParcelEntity(id=successor_id, project_id=project_id,
                                canonical_key=f"{payload.operation}:{change_id}:{successor_ids.index(successor_id)}", status="draft"))
        db.flush()
        for successor_id in successor_ids:
            for source_id in sorted(member_source_ids):
                db.add(ParcelSourceLink(project_id=project_id, parcel_entity_id=successor_id,
                                        source_feature_id=source_id, link_status="derived_evidence"))
            db.add(ParcelSelection(parcel_entity_id=successor_id, geometry_source_id=None,
                                   attribute_source_id=payload.attribute_source_id,
                                   attribute_sources=payload.attribute_sources, actor_id=user.id,
                                   rationale=f"Derived successor baseline for reviewed {payload.operation}", revision=1))
    change = GeometryChangeSet(id=change_id, project_id=project_id, operation=payload.operation,
                               parcel_entity_ids=payload.parcel_entity_ids,
                               predecessor_ids=payload.parcel_entity_ids if payload.operation in {"split", "merge"} else [],
                               successor_ids=successor_ids, before_geometries=measurements.get("before_geometries", {}),
                               draft_geometries=draft_geometries,
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
        GeometryChangeSet.project_id == project_id).order_by(GeometryChangeSet.created_at.desc()))
            if parcel_ids_readable(db, project_id, user, change.parcel_entity_ids)]


@app.post("/api/projects/{project_id}/geometry-changes/{change_id}/decision")
def decide_geometry_changeset(project_id: str, change_id: str, payload: GeometryDecision, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    change = db.get(GeometryChangeSet, change_id)
    if not change or change.project_id != project_id:
        raise HTTPException(404, "Geometry changeset not found")
    if change.revision != payload.expected_revision or change.status in {"approved", "rejected"}:
        raise HTTPException(409, "Geometry changeset is stale or final")
    predecessors = list(db.scalars(select(ParcelEntity).where(
        ParcelEntity.project_id == project_id,
        ParcelEntity.id.in_(change.predecessor_ids or []))))
    if len(predecessors) != len(set(change.predecessor_ids or [])):
        raise HTTPException(409, "Geometry changeset contains a predecessor outside this project")
    successors = list(db.scalars(select(ParcelEntity).where(
        ParcelEntity.project_id == project_id,
        ParcelEntity.id.in_(change.successor_ids or []))))
    if len(successors) != len(set(change.successor_ids or [])):
        raise HTTPException(409, "Geometry changeset contains a successor outside this project")
    affected = list(db.scalars(select(ParcelEntity).where(ParcelEntity.project_id == project_id,
                             ParcelEntity.id.in_(change.parcel_entity_ids or []), ParcelEntity.status == "active")))
    if len(affected) != len(set(change.parcel_entity_ids or [])):
        raise HTTPException(409, "Affected parcels are no longer active; refresh canonical geometry")
    if change.operation in {"move", "shared_edge"} and (change.successor_ids or change.predecessor_ids):
        raise HTTPException(409, "Non-creating operation contains unexpected identity mutations")
    if change.operation in {"split", "merge"} and any(
        item.status != "draft" or not (item.canonical_key or "").startswith(f"{change.operation}:{change.id}:")
        for item in successors):
        raise HTTPException(409, "Successor does not belong to this geometry changeset")
    if set(change.draft_geometries) != set(change.successor_ids or change.parcel_entity_ids):
        raise HTTPException(409, "Draft keys do not match this operation's identities")
    if payload.decision == "approved":
        measured = geometry_measurements(db, change.parcel_entity_ids, change.draft_geometries)
        if canonical_sha256(measured["before_geometries"]) != change.measurements.get("before_fingerprint"):
            raise HTTPException(409, "Canonical geometry changed since this draft; submit a refreshed draft")
        if change.measurements.get("policy") != processing_policy(db, project_id):
            raise HTTPException(409, "Processing policy changed since this draft")
        if geometry_submission_gates(change.operation, measured, processing_policy(db, project_id)):
            raise HTTPException(409, "Neighbor topology changed since the draft; reload and review a new preview")
    claimed = db.execute(update(GeometryChangeSet).where(GeometryChangeSet.id == change.id,
                         GeometryChangeSet.revision == payload.expected_revision,
                         GeometryChangeSet.status.in_(["draft", "deferred"]))
                         .values(status=payload.decision, revision=payload.expected_revision + 1)
                         .execution_options(synchronize_session=False))
    if not claimed.rowcount:
        db.rollback()
        raise HTTPException(409, "Concurrent geometry decision won; refresh before retrying")
    change.status = payload.decision
    change.rationale = f"{change.rationale}\nDecision: {payload.rationale}"
    change.decision_rationale = payload.rationale
    change.decision_at = datetime.now(timezone.utc)
    change.revision += 1
    if payload.decision == "approved":
        change.approved_geometries = change.draft_geometries
        change.approved_by = user.id
        for predecessor in predecessors:
            predecessor.status = "superseded"
        for successor in successors:
            successor.status = "active"
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
            "measurements": change.measurements, "authorization": change.authorization, "rationale": change.rationale,
            "approved_by": change.approved_by, "decision_at": change.decision_at,
            "decision_rationale": change.decision_rationale}


@app.post("/api/projects/{project_id}/measurements")
def measure(project_id: str, payload: MeasurementRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    try:
        return measure_geometry(payload.geometry, payload.source_crs, payload.analysis_crs, payload.purpose, payload.method)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/ground-control")
def create_ground_control(project_id: str, payload: GroundControlRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    dataset = validate_dataset(db, project_id, payload.dataset_id)
    if dataset.id not in readable_dataset_ids(db, project_id, user):
        raise HTTPException(403, "Restricted source requires project review permission")
    try:
        source_crs, target_crs, fit = fit_session(db, dataset, payload)
    except (ValueError, TypeError, FloatingPointError) as exc:
        raise HTTPException(422, str(exc)) from exc
    session = GroundControlSession(project_id=project_id, dataset_id=dataset.id, method=payload.method,
                                   source_crs=source_crs, target_crs=target_crs,
                                   control_points=payload.control_points,
                                   residuals={"count": len(payload.control_points), **fit}, created_by=user.id)
    db.add(session)
    touch_project(db, project_id)
    audit(db, "ground_control_created", user.id, project_id, "ground_control", session.id,
          {"dataset_id": dataset.id, "method": payload.method, "residuals": session.residuals})
    db.commit()
    return ground_control_response(session)


@app.get("/api/projects/{project_id}/ground-control")
def list_ground_control(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [ground_control_response(session) for session in db.scalars(
        select(GroundControlSession).where(GroundControlSession.project_id == project_id)
        .order_by(GroundControlSession.created_at.desc())) if session.dataset_id in readable_dataset_ids(db, project_id, user)]


@app.post("/api/projects/{project_id}/ground-control/{session_id}/approve")
def approve_ground_control(project_id: str, session_id: str, db: Db, user: CurrentUser,
                           payload: GroundControlApprovalRequest | None = None):
    ensure_project_access(db, project_id, user, write=True, review=True)
    session = db.get(GroundControlSession, session_id)
    if not session or session.project_id != project_id:
        raise HTTPException(404, "Ground control session not found")
    if payload is None:
        payload = GroundControlApprovalRequest(expected_revision=session.revision, rationale="Legacy approval request")
    if session.status != "draft":
        raise HTTPException(409, "Ground-control approval is final; create a new session")
    if session.revision != payload.expected_revision:
        raise HTTPException(409, "Ground-control session changed; refresh before approval")
    if session.residuals.get("residual_units") != "metres":
        raise HTTPException(409, "Refit legacy controls with explicit metre residuals before approval")
    if not session.residuals.get("checkpoint_count"):
        raise HTTPException(409, "An independent checkpoint is required before approval")
    if session.residuals.get("checkpoint_max") is None or session.residuals["checkpoint_max"] > session.residuals.get("max_checkpoint_residual_threshold", 1.0):
        raise HTTPException(409, "Independent checkpoint residual exceeds the configured threshold")
    source_dataset = validate_dataset(db, project_id, session.dataset_id)
    if source_dataset.content_hash != session.residuals.get("source_content_hash"):
        raise HTTPException(409, "Source changed since the controls were fitted")
    coverage = control_coverage(db, source_dataset.id, session.control_points)
    if not coverage["covers_source"]:
        raise HTTPException(409, "Source features fall outside validated control/checkpoint coverage; add independently verified controls")
    try:
        aligned_dataset = apply_ground_control_version(db, source_dataset, session, user.id, payload.rationale)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.status = "approved"; session.approved_by = user.id; session.approved_dataset_id = aligned_dataset.id; session.revision += 1
    audit(db, "ground_control_approved", user.id, project_id, "ground_control", session.id, session.residuals)
    db.commit()
    return ground_control_response(session)


def apply_ground_control_version(db: Session, source_dataset: Dataset, session: GroundControlSession,
                                 actor_id: str, rationale: str) -> Dataset:
    from shapely.affinity import affine_transform
    from shapely.geometry import mapping, shape
    from .services import analysis_crs_for, spatial_column
    from .spatial import reproject
    parameters = session.residuals.get("parameters") or {}
    if session.method == "translation":
        tx, ty = parameters.get("translation", [0, 0])
        affine_parameters = [1, 0, 0, 1, tx, ty]
    elif session.method == "similarity":
        affine_parameters = [parameters["a"], -parameters["b"], parameters["b"], parameters["a"], parameters["tx"], parameters["ty"]]
    elif session.method == "affine":
        x, y = parameters.get("x", []), parameters.get("y", [])
        if len(x) != 3 or len(y) != 3:
            raise ValueError("Stored affine parameters are incomplete")
        affine_parameters = [x[0], x[1], y[0], y[1], x[2], y[2]]
    else:
        raise ValueError("Unsupported ground-control method")
    aligned = Dataset(id=uid(), project_id=source_dataset.project_id, name=f"{source_dataset.name} · aligned",
                      source_organization=source_dataset.source_organization, capture_date=source_dataset.capture_date,
                      content_hash=source_dataset.content_hash, original_filename=source_dataset.original_filename,
                      mime_type=source_dataset.mime_type, raw_path=source_dataset.raw_path,
                      declared_crs=source_dataset.declared_crs, parent_dataset_id=source_dataset.id,
                      access_classification=source_dataset.access_classification,
                      accuracy_metadata=source_dataset.accuracy_metadata, metadata_json=source_dataset.metadata_json,
                      administrative_namespace=source_dataset.administrative_namespace,
                      license_classification=source_dataset.license_classification, provenance=source_dataset.provenance,
                      source_version=source_dataset.source_version, version_label=f"aligned-from-{source_dataset.id}",
                      status="processed")
    db.add(aligned); db.flush()
    output_count = 0
    for source in db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == source_dataset.id)):
        normalized_geometry = None
        geometry_type = source.geometry_type
        if source.original_geometry:
            try:
                transformed = affine_transform(shape(source.original_geometry), affine_parameters)
                normalized = transformed if session.target_crs == "EPSG:4326" else reproject(transformed, session.target_crs, "EPSG:4326")
                from .spatial import check_coordinates
                check_coordinates(normalized, True)
                if not normalized.is_valid:
                    raise ValueError(f"Aligned geometry {source.id} is invalid")
                normalized_geometry = mapping(normalized)
                geometry_type = normalized.geom_type
                output_count += 1
            except Exception as exc:
                raise ValueError(f"Could not apply alignment to source feature {source.id}: {exc}") from exc
        db.add(SourceFeature(dataset_id=aligned.id, original_id=source.original_id,
                              raw_attributes=source.raw_attributes, original_geometry=source.original_geometry,
                              normalized_geometry=normalized_geometry, administrative_context=source.administrative_context,
                              canonical_attributes=source.canonical_attributes, geometry_type=geometry_type,
                              status="processed" if normalized_geometry or not source.original_geometry else "quarantined",
                              processing_reason=None if normalized_geometry or not source.original_geometry else "Alignment produced no valid geometry",
                              **spatial_column(shape(normalized_geometry) if normalized_geometry else None, "EPSG:4326")))
    aligned.record_count = source_dataset.record_count
    aligned.validation_report = {"alignment": "approved", "source_dataset_id": source_dataset.id, "residual_units": "metres"}
    source_mapping = db.scalar(select(SchemaMapping).where(SchemaMapping.dataset_id == source_dataset.id)
                               .order_by(SchemaMapping.version.desc()))
    if source_mapping:
        aligned.schema_mapping_version = source_mapping.version
        db.add(SchemaMapping(dataset_id=aligned.id, project_id=source_dataset.project_id,
                            version=source_mapping.version, status=source_mapping.status,
                            mapping=source_mapping.mapping, source_fields=source_mapping.source_fields,
                            created_by=actor_id))
    aligned.normalized_crs = "EPSG:4326" if output_count else None
    aligned.normalized_count = output_count
    aligned.geometry_type = source_dataset.geometry_type
    first_geometry = next((feature.normalized_geometry for feature in db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == aligned.id)) if feature.normalized_geometry), None)
    aligned.analysis_crs = analysis_crs_for(shape(first_geometry)) if first_geometry else None
    aligned.crs_transform = {"method": "ground_control", "source_crs": session.source_crs, "target_crs": session.target_crs,
                             "parameters": parameters, "residuals": session.residuals, "approved_by": actor_id,
                             "rationale": rationale}
    aligned.crs_confirmed_by = actor_id
    aligned.crs_confirmed_at = datetime.now(timezone.utc)
    return aligned


def ground_control_response(session: GroundControlSession) -> dict:
    return {"id": session.id, "dataset_id": session.dataset_id, "method": session.method,
            "source_crs": session.source_crs, "target_crs": session.target_crs, "approved_dataset_id": session.approved_dataset_id,
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


@app.post("/api/projects/{project_id}/ranker/{artifact_id}/activation")
def activate_ranker(project_id: str, artifact_id: str, payload: RankerActivationRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    artifact = db.get(ModelArtifact, artifact_id)
    if not artifact or artifact.project_id != project_id:
        raise HTTPException(404, "Ranker artifact not found")
    if payload.active:
        validation = (artifact.metrics or {}).get("splits", {}).get("validation", {})
        if not validation.get("count"):
            raise HTTPException(409, "A held-out validation split is required before activation")
        for other in db.scalars(select(ModelArtifact).where(ModelArtifact.project_id == project_id,
                                                            ModelArtifact.model_type == artifact.model_type)):
            other.activation_status = "inactive"
        artifact.activation_status = "active"
        artifact.activated_by = user.id
        artifact.activated_at = datetime.now(timezone.utc)
    else:
        artifact.activation_status = "inactive"
        artifact.activated_by = user.id
        artifact.activated_at = datetime.now(timezone.utc)
    audit(db, "ranker_activation_changed", user.id, project_id, "model_artifact", artifact.id,
          {"active": payload.active, "version": artifact.version})
    db.commit()
    return model_artifact_response(artifact)


def model_artifact_response(artifact: ModelArtifact) -> dict:
    return {"id": artifact.id, "model_type": artifact.model_type, "version": artifact.version,
            "artifact": artifact.artifact, "metrics": artifact.metrics, "dataset_fingerprint": artifact.dataset_fingerprint,
             "seed": artifact.seed, "activation_status": artifact.activation_status,
             "activated_by": artifact.activated_by, "activated_at": artifact.activated_at, "created_at": artifact.created_at}


@app.post("/api/projects/{project_id}/field-assignments", status_code=201)
def create_field_assignment(project_id: str, payload: AssignmentRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    assignee = db.get(User, payload.assignee_id)
    if not assignee or assignee.role not in {"field", "processor", "reviewer", "admin"}:
        raise HTTPException(422, "Assignments require an authorized fieldwork account")
    if not project_member(db, project_id, assignee.id) and assignee.role != "admin":
        raise HTTPException(422, "The assignee must be an active member of this project")
    if utc_expired(payload.expires_at):
        raise HTTPException(422, "Assignment expiry must be in the future")
    if len(payload.parcel_entity_ids) > get_settings().field_max_assignments:
        raise HTTPException(422, "Assignment exceeds the configured parcel bound")
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
    ensure_project_access(db, project_id, user, capability="fieldwork")
    query = select(FieldAssignment).where(FieldAssignment.project_id == project_id)
    if user.role == "field":
        query = query.where(FieldAssignment.assignee_id == user.id)
    assignments = list(db.scalars(query.order_by(FieldAssignment.created_at.desc())))
    if user.role == "field":
        assignments = [assignment for assignment in assignments
                       if assignment.status in {"assigned", "active"} and not utc_expired(assignment.expires_at)]
    return [assignment_response(assignment) for assignment in assignments]


@app.patch("/api/projects/{project_id}/field-assignments/{assignment_id}")
def update_field_assignment(project_id: str, assignment_id: str, payload: AssignmentUpdateRequest,
                            db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    assignment = db.get(FieldAssignment, assignment_id)
    if not assignment or assignment.project_id != project_id:
        raise HTTPException(404, "Assignment not found")
    if assignment.revision != payload.expected_revision:
        raise HTTPException(409, "Assignment changed; refresh before updating")
    if payload.expires_at is not None and utc_expired(payload.expires_at):
        raise HTTPException(422, "Assignment expiry must be in the future")
    if payload.status is not None:
        assignment.status = payload.status
    if payload.expires_at is not None:
        assignment.expires_at = payload.expires_at
    assignment.revision += 1
    audit(db, "field_assignment_updated", user.id, project_id, "field_assignment", assignment.id,
          {"status": assignment.status, "expires_at": assignment.expires_at, "revision": assignment.revision})
    touch_project(db, project_id)
    db.commit()
    return assignment_response(assignment)


@app.get("/api/projects/{project_id}/field-assignments/{assignment_id}/reference")
def field_assignment_reference(project_id: str, assignment_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, capability="fieldwork")
    assignment = db.get(FieldAssignment, assignment_id)
    if not assignment or assignment.project_id != project_id or assignment.assignee_id != user.id:
        raise HTTPException(404, "Assignment not found")
    if assignment.status not in {"assigned", "active"} or utc_expired(assignment.expires_at):
        raise HTTPException(403, "Assignment is expired or revoked")
    allowed = set((assignment.reference_policy or {}).get("fields", []))
    if not allowed:
        allowed = {"parcel_id", "survey_number", "village", "village_code", "district", "ward", "recorded_area", "area_units"}
    output = []
    for parcel_id in assignment.parcel_entity_ids:
        entity = db.get(ParcelEntity, parcel_id)
        if not entity or entity.project_id != project_id or entity.status != "active":
            continue
        selection = db.scalar(select(ParcelSelection).where(ParcelSelection.parcel_entity_id == entity.id))
        if not selection:
            continue
        attribute = db.get(SourceFeature, selection.attribute_source_id)
        if not attribute:
            continue
        values = attribute.canonical_attributes or attribute.raw_attributes
        values = dict(values)
        for field, source_id in (selection.attribute_sources or {}).items():
            chosen = db.get(SourceFeature, source_id)
            if chosen:
                values[field] = (chosen.canonical_attributes or chosen.raw_attributes).get(field)
        from shapely.geometry import mapping as geometry_mapping
        resolved, origin = resolve_canonical_geometry(db, entity.id)
        output.append({"parcel_entity_id": entity.id, "attributes": {key: values.get(key) for key in allowed},
                       "geometry": geometry_mapping(resolved) if resolved is not None else None,
                       "geometry_origin": origin})
    return {"assignment_id": assignment.id, "expires_at": assignment.expires_at, "features": output}


@app.post("/api/projects/{project_id}/field-assignments/{assignment_id}/evidence", status_code=201)
def submit_field_evidence(project_id: str, assignment_id: str, payload: FieldEvidenceRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, capability="fieldwork")
    assignment = db.get(FieldAssignment, assignment_id)
    if not assignment or assignment.project_id != project_id or (user.role == "field" and assignment.assignee_id != user.id):
        raise HTTPException(404, "Assignment not found")
    if assignment.assignee_id != user.id and user.role not in {"admin", "reviewer", "steward"}:
        raise HTTPException(403, "Only the assigned officer can submit field evidence")
    if assignment.status not in {"assigned", "active"} or utc_expired(assignment.expires_at):
        raise HTTPException(403, "Assignment is expired or revoked")
    if payload.parcel_entity_id not in (assignment.parcel_entity_ids or []):
        raise HTTPException(422, "Evidence parcel is outside the bounded assignment")
    existing = db.scalar(select(FieldEvidence).where(FieldEvidence.client_event_id == payload.client_event_id))
    if existing:
        if (existing.created_by != user.id or existing.assignment_id != assignment_id or
                existing.parcel_entity_id != payload.parcel_entity_id or existing.payload != payload.payload):
            raise HTTPException(409, "Client event ID is already used by a different evidence payload")
        return field_evidence_response(existing)
    if len(json.dumps(payload.payload, default=str).encode()) > get_settings().field_max_queued_bytes:
        raise HTTPException(413, "Evidence payload exceeds the configured bound")
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


@app.post("/api/projects/{project_id}/field-assignments/{assignment_id}/evidence/{evidence_id}/resolve")
def resolve_field_evidence(project_id: str, assignment_id: str, evidence_id: str,
                           payload: FieldEvidenceResolutionRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, capability="fieldwork")
    assignment = db.get(FieldAssignment, assignment_id)
    evidence = db.get(FieldEvidence, evidence_id)
    if not assignment or assignment.project_id != project_id or not evidence or evidence.assignment_id != assignment_id:
        raise HTTPException(404, "Field evidence not found")
    if evidence.status not in {"revision_conflict", "submitted"} or evidence.expected_project_revision != payload.expected_revision:
        raise HTTPException(409, "Evidence revision is stale or already final")
    if payload.decision in {"accept", "reject"}:
        if user.role not in {"admin", "reviewer", "steward"}:
            raise HTTPException(403, "Reviewer permission is required to resolve evidence")
        evidence.status = "accepted" if payload.decision == "accept" else "rejected"
        audit(db, "field_evidence_resolved", user.id, project_id, "field_evidence", evidence.id,
              {"decision": payload.decision, "rationale": payload.rationale})
        db.commit()
        return field_evidence_response(evidence)
    if assignment.assignee_id != user.id:
        raise HTTPException(403, "Only the assigned officer can resubmit a draft")
    if assignment.status not in {"assigned", "active"} or utc_expired(assignment.expires_at):
        raise HTTPException(403, "Assignment is expired or revoked")
    if payload.new_expected_project_revision is None:
        raise HTTPException(422, "A current project revision is required for resubmission")
    project = db.get(Project, project_id)
    next_evidence = FieldEvidence(assignment_id=assignment_id, parcel_entity_id=evidence.parcel_entity_id,
                                  client_event_id=f"{evidence.client_event_id}:resubmit:{uid()}", payload=evidence.payload,
                                  status="submitted" if project and payload.new_expected_project_revision == project.workflow_revision else "revision_conflict",
                                  expected_project_revision=payload.new_expected_project_revision, created_by=user.id)
    evidence.status = "superseded"
    db.add(next_evidence)
    audit(db, "field_evidence_resubmitted", user.id, project_id, "field_evidence", next_evidence.id,
          {"supersedes": evidence.id, "rationale": payload.rationale})
    db.commit()
    return field_evidence_response(next_evidence)


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
    try:
        return execute_query(db, project_id, user, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/{project_id}/compliance-rules", status_code=201)
def create_compliance_rule(project_id: str, payload: ComplianceRuleRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=payload.confirm)
    formula = {**payload.formula}
    if payload.operator:
        formula["operator"] = payload.operator
    threshold = {**payload.threshold}
    if payload.units:
        threshold["units"] = payload.units
    try:
        validate_compliance_rule({"formula": formula, "threshold": threshold, "operator": payload.operator})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    version = (db.scalar(select(ComplianceRule.version).where(ComplianceRule.project_id == project_id,
                                                             ComplianceRule.name == payload.name)
                         .order_by(ComplianceRule.version.desc())) or 0) + 1
    rule = ComplianceRule(project_id=project_id, name=payload.name, jurisdiction=payload.jurisdiction,
                          category=payload.category, effective_from=payload.effective_from, effective_to=payload.effective_to,
                          version=version, inputs=payload.inputs, formula=formula, threshold=threshold,
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
    try:
        result = compliance_result(rule, payload.values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
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
    if utc_expired(payload.expires_at):
        raise HTTPException(422, "Grant expiry must be in the future")
    if (not citizen or citizen.role != "citizen" or not project_member(db, project_id, citizen.id)
            or not entity or entity.project_id != project_id):
        raise HTTPException(422, "An explicit citizen account and project parcel are required")
    latest_version = db.scalar(select(PublishedVersion).where(
        PublishedVersion.project_id == project_id,
        PublishedVersion.id.in_(select(PublicationFeature.version_id).where(
            PublicationFeature.parcel_entity_id == entity.id))).order_by(PublishedVersion.version_number.desc()))
    if not latest_version:
        raise HTTPException(409, "Citizen grants require a frozen published parcel record")
    frozen = db.scalar(select(PublicationFeature).where(PublicationFeature.version_id == latest_version.id,
                                                       PublicationFeature.parcel_entity_id == entity.id))
    if not frozen:
        raise HTTPException(409, "The parcel is not present in the latest published version")
    available_fields = set(frozen.attributes or {})
    if frozen.geometry:
        available_fields.add("geometry")
    if not set(payload.fields).issubset(available_fields):
        raise HTTPException(422, "Every granted field must exist in the frozen published record")
    grant = CitizenGrant(project_id=project_id, citizen_id=citizen.id, parcel_entity_id=entity.id,
                         published_version_id=latest_version.id,
                         fields=sorted(set(payload.fields)), expires_at=payload.expires_at, granted_by=user.id)
    db.add(grant)
    audit(db, "citizen_grant_created", user.id, project_id, "citizen_grant", grant.id,
          {"citizen_id": citizen.id, "parcel_entity_id": entity.id, "fields": grant.fields})
    db.commit()
    return citizen_grant_response(grant)


@app.get("/api/projects/{project_id}/citizen-grants")
def list_citizen_grants(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, capability="citizen")
    query = select(CitizenGrant).where(CitizenGrant.project_id == project_id)
    if user.role == "citizen":
        query = query.where(CitizenGrant.citizen_id == user.id, CitizenGrant.status == "active")
    grants = list(db.scalars(query.order_by(CitizenGrant.created_at.desc())))
    if user.role == "citizen":
        grants = [grant for grant in grants if not utc_expired(grant.expires_at)]
    return [citizen_grant_response(grant) for grant in grants]


@app.patch("/api/projects/{project_id}/citizen-grants/{grant_id}")
def update_citizen_grant(project_id: str, grant_id: str, payload: CitizenGrantUpdateRequest,
                         db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    grant = db.get(CitizenGrant, grant_id)
    if not grant or grant.project_id != project_id:
        raise HTTPException(404, "Citizen grant not found")
    allowed_fields = {"parcel_id", "survey_number", "property_account", "village", "village_code", "district", "ward",
                      "recorded_area", "area_units", "geometry", "status", "capture_date"}
    if payload.fields is not None and not set(payload.fields).issubset(allowed_fields):
        raise HTTPException(422, "Citizen grants can contain only approved public fields")
    if payload.expires_at is not None and utc_expired(payload.expires_at):
        raise HTTPException(422, "Grant expiry must be in the future")
    if payload.status is not None:
        grant.status = payload.status
    if payload.fields is not None:
        grant.fields = sorted(set(payload.fields))
    if payload.expires_at is not None:
        grant.expires_at = payload.expires_at
    audit(db, "citizen_grant_updated", user.id, project_id, "citizen_grant", grant.id,
          {"status": grant.status, "fields": grant.fields, "expires_at": grant.expires_at})
    db.commit()
    return citizen_grant_response(grant)


@app.get("/api/projects/{project_id}/citizen-records")
def citizen_records(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, capability="citizen")
    grants = list(db.scalars(select(CitizenGrant).where(CitizenGrant.project_id == project_id,
                                                        CitizenGrant.citizen_id == user.id,
                                                        CitizenGrant.status == "active")))
    grants = [grant for grant in grants if not utc_expired(grant.expires_at)]
    output = []
    for grant in grants:
        entity = db.get(ParcelEntity, grant.parcel_entity_id)
        if not entity:
            continue
        if grant.published_version_id:
            version = db.scalar(select(PublishedVersion).where(
                PublishedVersion.id == grant.published_version_id,
                PublishedVersion.project_id == project_id,
                PublishedVersion.status == "published"))
        else:
            # Legacy grants created before frozen-version binding are resolved
            # deterministically to the latest publication containing the parcel.
            version = db.scalar(select(PublishedVersion).where(
                PublishedVersion.project_id == project_id,
                PublishedVersion.status == "published",
                PublishedVersion.id.in_(select(PublicationFeature.version_id).where(
                    PublicationFeature.parcel_entity_id == grant.parcel_entity_id)))
                .order_by(PublishedVersion.version_number.desc()))
        if not version:
            continue
        published = db.scalar(select(PublicationFeature).where(
            PublicationFeature.version_id == version.id,
            PublicationFeature.parcel_entity_id == grant.parcel_entity_id))
        if not published:
            continue
        values = {field: published.attributes.get(field) for field in grant.fields if field != "geometry"}
        output.append({"parcel_entity_id": entity.id, "fields": values, "grant_id": grant.id,
                       "version_id": version.id, "version": version.version_number,
                       "geometry": published.geometry if "geometry" in grant.fields else None,
                       "provenance": {"published_version_id": version.id, "version": version.version_number,
                                      "published_at": version.created_at,
                                      "manifest_sha256": version.manifest_sha256}})
    return output


@app.post("/api/projects/{project_id}/citizen-cases", status_code=201)
def create_citizen_case(project_id: str, payload: CitizenCaseRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, capability="citizen")
    grant = db.scalar(select(CitizenGrant).where(CitizenGrant.project_id == project_id,
                                                CitizenGrant.citizen_id == user.id,
                                                CitizenGrant.parcel_entity_id == payload.parcel_entity_id,
                                                CitizenGrant.status == "active"))
    if not grant or utc_expired(grant.expires_at):
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
    ensure_project_access(db, project_id, user, capability="citizen")
    query = select(CitizenCase).where(CitizenCase.project_id == project_id)
    if user.role == "citizen":
        query = query.join(CitizenGrant, CitizenGrant.id == CitizenCase.grant_id).where(CitizenGrant.citizen_id == user.id)
    return [citizen_case_response(case) for case in db.scalars(query.order_by(CitizenCase.created_at.desc()))]


@app.patch("/api/projects/{project_id}/citizen-cases/{case_id}")
def respond_citizen_case(project_id: str, case_id: str, payload: CitizenCaseResponseRequest,
                         db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    case = db.get(CitizenCase, case_id)
    if not case or case.project_id != project_id:
        raise HTTPException(404, "Citizen case not found")
    case.status = payload.status
    case.response = payload.response
    audit(db, "citizen_case_responded", user.id, project_id, "citizen_case", case.id,
          {"status": case.status})
    db.commit()
    return citizen_case_response(case)


def citizen_grant_response(grant: CitizenGrant) -> dict:
    return {"id": grant.id, "citizen_id": grant.citizen_id, "parcel_entity_id": grant.parcel_entity_id,
            "published_version_id": grant.published_version_id, "fields": grant.fields, "expires_at": grant.expires_at,
            "status": grant.status, "created_at": grant.created_at}


def citizen_case_response(case: CitizenCase) -> dict:
    return {"id": case.id, "grant_id": case.grant_id, "parcel_entity_id": case.parcel_entity_id,
            "category": case.category, "description": case.description, "status": case.status,
            "response": case.response, "created_at": case.created_at}


@app.get("/api/projects/{project_id}/conflicts")
def conflicts(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": c.id, "type": c.conflict_type, "severity": c.severity, "description": c.description,
             "status": c.status, "revision": c.revision, "feature_id": c.feature_id, "dataset_id": c.dataset_id, "details": c.details}
             for c in db.scalars(select(TopologyConflict).where(TopologyConflict.project_id == project_id))
             if c.dataset_id in readable_dataset_ids(db, project_id, user) or (c.dataset_id is None and len(readable_dataset_ids(db, project_id, user)) == len(list(db.scalars(select(Dataset.id).where(Dataset.project_id == project_id)))))]


@app.post("/api/projects/{project_id}/changes/detect")
def changes(project_id: str, payload: ChangeRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    ensure_readable_inputs(db, project_id, user, [payload.before_dataset_id, payload.after_dataset_id])
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
             for c in db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id))
             if source_ids_readable(db, project_id, user, [c.source_feature_id, c.comparison_feature_id])]


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
        if not source_ids_readable(db, project_id, user, members):
            continue
        selected = db.scalar(select(ParcelSelection).where(ParcelSelection.parcel_entity_id == entity.id))
        try:
            geometry, geometry_origin = resolve_canonical_geometry(db, entity.id)
            from shapely.geometry import mapping as geometry_mapping
            geometry_data = geometry_mapping(geometry) if geometry is not None else None
        except ValueError:
            geometry_data, geometry_origin = None, None
        output.append({"id": entity.id, "source_feature_ids": members,
                       "geometry": geometry_data, "geometry_origin": geometry_origin,
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
    source_features = list(db.scalars(select(PublicationFeature).where(PublicationFeature.version_id == source.id)
                              .order_by(PublicationFeature.id)))
    rollback_at = datetime.now(timezone.utc).isoformat()
    cloned_lineages = [{**feature.lineage, "rollback_of_version_id": source.id,
                        "rollback_created_at": rollback_at} for feature in source_features]
    manifest = {**source.lineage_manifest, "rollback_of_version_id": source.id,
                "rollback_created_at": rollback_at,
                "rollback_source_manifest_trusted": bool(source.manifest_sha256 and source.output_sha256),
                "features": cloned_lineages}
    output_context = canonical_output_context([{"parcel_entity_id": feature.parcel_entity_id,
                                                "geometry": feature.geometry,
                                                "attributes": feature.attributes,
                                                "lineage": lineage}
                                               for feature, lineage in zip(source_features, cloned_lineages)])
    version = PublishedVersion(project_id=project_id, version_number=next_version(db, project_id), created_by=user.id,
                               project_revision=project.workflow_revision, base_version_id=source.id,
                               validation_report={"valid": True, "type": "traceable_rollback", "source_version": source.id},
                               lineage_manifest=manifest, excluded_records=source.excluded_records,
                               manifest_sha256=canonical_sha256(manifest), output_sha256=canonical_sha256(output_context))
    db.add(version)
    db.flush()
    for feature, lineage in zip(source_features, cloned_lineages):
        from shapely.geometry import shape
        from .services import spatial_column
        db.add(PublicationFeature(version_id=version.id, source_feature_id=feature.source_feature_id,
                                  parcel_entity_id=feature.parcel_entity_id, attributes=feature.attributes,
                                   geometry=feature.geometry, lineage=lineage,
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
    manifest_hash = canonical_sha256(version.lineage_manifest)
    manifest_valid = bool(version.manifest_sha256 and manifest_hash == version.manifest_sha256)
    features = list(db.scalars(select(PublicationFeature).where(PublicationFeature.version_id == version.id)))
    output_context = sorted([{"parcel_entity_id": feature.parcel_entity_id, "geometry": feature.geometry,
                              "attributes": feature.attributes, "lineage": feature.lineage} for feature in features],
                            key=lambda item: item["parcel_entity_id"] or "")
    output_hash = canonical_sha256(output_context)
    output_valid = bool(version.output_sha256 and output_hash == version.output_sha256)
    source_results = []
    for source in version.lineage_manifest.get("features", []):
        for item in source.get("sources", []):
            dataset = db.get(Dataset, item.get("dataset_id"))
            actual = hashlib.sha256(__import__("pathlib").Path(dataset.raw_path).read_bytes()).hexdigest() if dataset and dataset.raw_path and __import__("pathlib").Path(dataset.raw_path).exists() else None
            source_results.append({"dataset_id": item.get("dataset_id"), "expected_sha256": item.get("sha256"),
                                  "actual_sha256": actual, "valid": actual == item.get("sha256")})
    source_valid = bool(source_results) and all(item["valid"] for item in source_results)
    return {"version_id": version.id, "version": version.version_number, "manifest_sha256": manifest_hash,
            "expected_manifest_sha256": version.manifest_sha256, "manifest_valid": manifest_valid,
            "output_sha256": output_hash, "expected_output_sha256": version.output_sha256, "output_valid": output_valid,
            "sources": source_results, "source_bytes_valid": source_valid,
            "valid": manifest_valid and output_valid and source_valid,
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
                            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/landing-page"]}


@app.get("/api/ogc/collections")
def ogc_collections(db: Db, user: CurrentUser):
    if user.role in {"field", "citizen"}:
        raise HTTPException(403, "OGC departmental collections require a departmental read capability")
    projects = list_projects(db, user)
    return {"collections": [{"id": project.id, "title": project.name, "itemType": "feature",
                              "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
                              "links": [{"rel": "items", "href": f"/api/ogc/collections/{project.id}/items"}]}
                             for project in projects]}


@app.get("/api/ogc/collections/{project_id}")
def ogc_collection(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Collection not found")
    return {"id": project.id, "title": project.name, "description": "Reviewed published parcel features",
            "itemType": "feature", "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
            "links": [{"rel": "items", "href": f"/api/ogc/collections/{project.id}/items"}]}


@app.get("/api/ogc/collections/{project_id}/items")
def ogc_items(project_id: str, db: Db, user: CurrentUser, version_id: str | None = None,
              bbox: str | None = None, limit: int = 100, offset: int = 0):
    ensure_project_access(db, project_id, user)
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    version = db.get(PublishedVersion, version_id) if version_id else db.scalar(select(PublishedVersion).where(
        PublishedVersion.project_id == project_id).order_by(PublishedVersion.version_number.desc()))
    if not version or version.project_id != project_id:
        raise HTTPException(404, "Reviewed version not found")
    bounds = None
    if bbox:
        try:
            values = [float(value) for value in bbox.split(",")]
            if len(values) != 4 or values[0] > values[2] or values[1] > values[3]:
                raise ValueError
            bounds = values
        except ValueError as exc:
            raise HTTPException(422, "bbox must be minx,miny,maxx,maxy in CRS84") from exc
    feature_query = select(PublicationFeature).where(PublicationFeature.version_id == version.id).order_by(PublicationFeature.id)
    database_bbox = bool(bounds and hasattr(PublicationFeature, "spatial_geometry"))
    if database_bbox:
        feature_query = feature_query.where(
            func.ST_Intersects(PublicationFeature.spatial_geometry, func.ST_MakeEnvelope(*bounds, 4326))
        )
    if not bounds or database_bbox:
        matched = db.scalar(select(func.count()).select_from(feature_query.subquery())) or 0
        features = list(db.scalars(feature_query.offset(offset).limit(limit)))
    else:
        features = list(db.scalars(feature_query))

    def inside(feature):
        if not bounds or not feature.geometry:
            return True
        from shapely.geometry import shape
        minx, miny, maxx, maxy = shape(feature.geometry).bounds
        return not (maxx < bounds[0] or minx > bounds[2] or maxy < bounds[1] or miny > bounds[3])
    filtered = features if database_bbox else [feature for feature in features if inside(feature)]
    page = filtered if (not bounds or database_bbox) else filtered[offset:offset + limit]
    if not bounds and not database_bbox:
        matched = db.scalar(select(func.count()).where(PublicationFeature.version_id == version.id)) or 0
    elif not database_bbox:
        matched = len(filtered)
    next_query = f"version_id={version.id}&limit={limit}&offset={offset + limit}"
    if bbox:
        next_query += f"&bbox={bbox}"
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "id": feature.parcel_entity_id or feature.id,
              "geometry": feature.geometry, "properties": {**feature.attributes, "_lineage": feature.lineage}}
              for feature in page], "numberMatched": matched, "numberReturned": len(page),
             "links": ([{"rel": "next", "href": f"/api/ogc/collections/{project_id}/items?{next_query}"}]
                       if offset + limit < matched else [])}


@app.get("/api/ogc/collections/{project_id}/items/{item_id}")
def ogc_item(project_id: str, item_id: str, db: Db, user: CurrentUser, version_id: str | None = None):
    ensure_project_access(db, project_id, user)
    version = db.get(PublishedVersion, version_id) if version_id else db.scalar(select(PublishedVersion).where(
        PublishedVersion.project_id == project_id).order_by(PublishedVersion.version_number.desc()))
    if not version or version.project_id != project_id:
        raise HTTPException(404, "Reviewed version not found")
    feature = db.scalar(select(PublicationFeature).where(PublicationFeature.version_id == version.id,
                                                         (PublicationFeature.parcel_entity_id == item_id) | (PublicationFeature.id == item_id)))
    if not feature:
        raise HTTPException(404, "Published feature not found")
    return {"type": "Feature", "id": feature.parcel_entity_id or feature.id, "geometry": feature.geometry,
            "properties": {**feature.attributes, "_lineage": feature.lineage}}


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
    ensure_readable_inputs(db, project_id, user, [value for key, value in payload.items() if key.endswith("dataset_id")])
    configuration_hash = job_configuration_hash(db, job_type, payload)
    idempotency_key = idempotency_key or configuration_hash
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.project_id == project_id, Job.idempotency_key == idempotency_key))
        if existing:
            if existing.configuration_hash != configuration_hash:
                raise HTTPException(409, "Idempotency key refers to different inputs or configuration")
            return job_response(existing)
    job = Job(project_id=project_id, job_type=job_type, payload=payload, created_by=user.id,
              idempotency_key=idempotency_key, max_attempts=get_settings().job_max_attempts)
    existing = enforce_job_capacity(db, project_id, idempotency_key, configuration_hash)
    if existing:
        return job_response(existing)
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
    if job.attempts >= job.max_attempts:
        raise HTTPException(409, "Job has exhausted its bounded retry attempts")
    enforce_job_capacity(db, project_id)
    validate_job_payload(db, project_id, job.job_type, job.payload)
    ensure_readable_inputs(db, project_id, user, [value for key, value in job.payload.items() if key.endswith("dataset_id")])
    job.status = "queued"
    job.stage = "queued"
    job.cancellation_requested = False
    job.error = None
    job.owner_token = None
    job.lease_expires_at = None
    db.commit()
    dispatch_job(job_id, background)
    return job_response(job)


def enforce_job_capacity(db: Session, project_id: str, idempotency_key=None, configuration_hash=None):
    # A no-op write acquires the project lock on both SQLite and PostgreSQL;
    # queued/running work is bounded without changing its workflow revision.
    db.execute(update(Project).where(Project.id == project_id).values(workflow_revision=Project.workflow_revision))
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.project_id == project_id, Job.idempotency_key == idempotency_key))
        if existing:
            if existing.configuration_hash != configuration_hash:
                raise HTTPException(409, "Idempotency key refers to different inputs or configuration")
            return existing
    count = db.scalar(select(func.count()).select_from(Job).where(Job.project_id == project_id,
                                                                Job.status.in_(["queued", "running"])))
    if count >= get_settings().max_active_jobs_per_project:
        raise HTTPException(429, "Project processing queue is full", headers={"Retry-After": "30"})
    return None


@app.post("/api/projects/{project_id}/jobs/{job_id}/cancel")
def cancel_job(project_id: str, job_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    job = db.get(Job, job_id)
    if not job or job.project_id != project_id:
        raise HTTPException(404, "Job not found")
    if job.status in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(409, "Job is already final")
    if job.status == "queued":
        job.status = "cancelled"; job.stage = "cancelled"; job.finished_at = datetime.now(timezone.utc)
        job.cancelled_by = user.id; job.owner_token = None; job.lease_expires_at = None
    else:
        job.cancellation_requested = True
        job.cancelled_by = user.id
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
            "lease_expires_at": job.lease_expires_at, "max_attempts": job.max_attempts,
            "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at}


def ensure_readable_inputs(db, project_id, user, dataset_ids):
    if not set(dataset_ids).issubset(readable_dataset_ids(db, project_id, user)):
        raise HTTPException(403, "Processing inputs include restricted or unavailable source evidence")


def source_ids_readable(db, project_id, user, source_ids):
    identifiers = {value for value in source_ids if value}
    sources = list(db.scalars(select(SourceFeature).where(SourceFeature.id.in_(identifiers))))
    allowed = readable_dataset_ids(db, project_id, user)
    return len(sources) == len(identifiers) and all(source.dataset_id in allowed for source in sources)


def parcel_ids_readable(db, project_id, user, parcel_ids):
    source_ids = list(db.scalars(select(ParcelSourceLink.source_feature_id).where(
        ParcelSourceLink.project_id == project_id, ParcelSourceLink.parcel_entity_id.in_(parcel_ids))))
    return source_ids_readable(db, project_id, user, source_ids)


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
