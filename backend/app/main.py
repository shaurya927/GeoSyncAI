from __future__ import annotations

import csv
import io
import json
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
                     ParcelEntity, ParcelSourceLink, ParcelSelection)
from .schemas import (CRSConfirmation, ChangeRequest, DatasetRegister, LoginRequest, MatchRequest, MemberCreate,
                      ProjectCreate, ProjectOut, ReviewRequest, SchemaMappingRequest, SelectionRequest, PolicyRequest, Token)
from .services import (audit, bootstrap_synthetic, confirm_dataset_crs, ingest_dataset, materialize_identity_link,
                       run_change_detection, run_matching, run_topology, administrative_context, touch_project, next_version,
                       invalidate_dataset_evidence)
from .publication import publish, validate_project
from .policy import processing_policy
from .tasks import dispatch_job, recover_jobs, configuration_hash as job_configuration_hash


DEMO_USERS = {"viewer": "viewer", "processor": "processor", "reviewer": "reviewer", "admin": "admin"}


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
                      accuracy_metadata=payload.accuracy_metadata, metadata_json=payload.metadata)
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
                         parent_dataset_id: str | None = Form(None)):
    ensure_project_access(db, project_id, user, write=True)
    data = await file.read(get_settings().max_upload_bytes + 1)
    if len(data) > get_settings().max_upload_bytes:
        raise HTTPException(status_code=413, detail=f"Upload exceeds the {get_settings().max_upload_bytes} byte limit")
    dataset = Dataset(project_id=project_id, name=name or file.filename or "uploaded dataset",
                      source_organization=source_organization, capture_date=capture_date, declared_crs=declared_crs,
                      parent_dataset_id=parent_dataset_id)
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
            "validation_report": dataset.validation_report}


@app.get("/api/projects/{project_id}/datasets")
def list_datasets(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [dataset_response(d) for d in db.scalars(select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.uploaded_at.desc()))]


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
def list_features(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return [{"id": f.id, "original_id": f.original_id, "attributes": f.raw_attributes,
             "original_geometry": f.original_geometry, "geometry": f.normalized_geometry,
             "administrative_context": f.administrative_context, "canonical_attributes": f.canonical_attributes, "status": f.status,
             "processing_reason": f.processing_reason}
            for f in db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id))]


@app.get("/api/projects/{project_id}/datasets/{dataset_id}/raw")
def download_raw(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id or not dataset.raw_path:
        raise HTTPException(status_code=404, detail="Raw file not found")
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
    db.commit()
    dispatch_job(job_id, background)
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
