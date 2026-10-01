from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import CurrentUser, ensure_project_access, hash_password, verify_password, create_access_token
from .config import get_settings
from .db import get_db, init_db
from .models import (Dataset, Job, MatchProposal, Project, ProjectMember, ReviewDecision, SourceFeature,
                     TopologyConflict, User, ChangeProposal, PublishedVersion, PublicationFeature)
from .schemas import (ChangeRequest, DatasetRegister, LoginRequest, MatchRequest, MemberCreate, ProjectCreate,
                      ProjectOut, ReviewRequest, Token)
from .services import (audit, bootstrap_synthetic, ingest_dataset, publish, run_change_detection, run_matching,
                       run_topology, validate_project)
from .tasks import dispatch_job, recover_jobs


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


@app.post("/api/projects/{project_id}/members")
def add_member(project_id: str, payload: MemberCreate, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, review=True)
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
                      capture_date=payload.capture_date, declared_crs=payload.declared_crs, metadata_json=payload.metadata)
    db.add(dataset)
    audit(db, "dataset_registered", user.id, project_id, "dataset", dataset.id)
    db.commit()
    return dataset_response(dataset)


@app.post("/api/projects/{project_id}/datasets/upload", status_code=201)
async def upload_dataset(project_id: str, db: Db, user: CurrentUser, file: UploadFile = File(...),
                         name: str | None = Form(None), source_organization: str | None = Form(None),
                         capture_date: date | None = Form(None), declared_crs: str | None = Form(None)):
    ensure_project_access(db, project_id, user, write=True)
    data = await file.read()
    dataset = Dataset(project_id=project_id, name=name or file.filename or "uploaded dataset",
                      source_organization=source_organization, capture_date=capture_date, declared_crs=declared_crs)
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
            "status": dataset.status, "content_hash": dataset.content_hash, "original_filename": dataset.original_filename,
            "capture_date": dataset.capture_date, "uploaded_at": dataset.uploaded_at, "record_count": dataset.record_count,
            "normalized_count": dataset.normalized_count, "declared_crs": dataset.declared_crs,
            "normalized_crs": dataset.normalized_crs, "validation_report": dataset.validation_report}


@app.get("/api/projects/{project_id}/datasets")
def list_datasets(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [dataset_response(d) for d in db.scalars(select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.uploaded_at.desc()))]


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
    return [{"id": f.id, "original_id": f.original_id, "attributes": f.raw_attributes, "geometry": f.normalized_geometry,
             "status": f.status, "processing_reason": f.processing_reason} for f in db.scalars(select(SourceFeature).where(SourceFeature.dataset_id == dataset_id))]


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
    result = run_matching(db, project_id, payload.left_dataset_id, payload.right_dataset_id,
                          payload.id_fields, payload.max_distance, payload.ambiguity_margin)
    audit(db, "matching_completed", user.id, project_id, details={"result": result})
    db.commit()
    return result


@app.post("/api/projects/{project_id}/topology/{dataset_id}")
def topology(project_id: str, dataset_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    validate_dataset(db, project_id, dataset_id)
    return run_topology(db, project_id, dataset_id)


@app.get("/api/projects/{project_id}/matches")
def matches(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": p.id, "left_feature_id": p.left_feature_id, "right_feature_id": p.right_feature_id,
             "score": p.score, "score_type": p.score_type, "status": p.status, "evidence": p.evidence}
            for p in db.scalars(select(MatchProposal).where(MatchProposal.project_id == project_id))]


@app.get("/api/projects/{project_id}/conflicts")
def conflicts(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": c.id, "type": c.conflict_type, "severity": c.severity, "description": c.description, "status": c.status, "details": c.details}
            for c in db.scalars(select(TopologyConflict).where(TopologyConflict.project_id == project_id))]


@app.post("/api/projects/{project_id}/changes/detect")
def changes(project_id: str, payload: ChangeRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True)
    validate_dataset_pair(db, project_id, payload.before_dataset_id, payload.after_dataset_id)
    return run_change_detection(db, project_id, payload.before_dataset_id, payload.after_dataset_id, payload.id_fields, payload.geometry_tolerance)


@app.get("/api/projects/{project_id}/changes")
def list_changes(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user)
    return [{"id": c.id, "change_type": c.change_type, "boundary_change": c.boundary_change, "status": c.status,
             "before_geometry": c.before_geometry, "after_geometry": c.after_geometry, "evidence": c.evidence}
            for c in db.scalars(select(ChangeProposal).where(ChangeProposal.project_id == project_id))]


@app.post("/api/projects/{project_id}/reviews/{target_type}/{target_id}")
def review(project_id: str, target_type: str, target_id: str, payload: ReviewRequest, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    target = {"match": MatchProposal, "change": ChangeProposal, "conflict": TopologyConflict}.get(target_type)
    if not target:
        raise HTTPException(status_code=400, detail="target_type must be match, change, or conflict")
    record = db.get(target, target_id)
    if not record or record.project_id != project_id:
        raise HTTPException(status_code=404, detail="Review target not found")
    if target_type == "change" and payload.decision == "accepted" and not record.boundary_change:
        # Non-boundary changes can still be accepted, but this check makes target semantics explicit.
        pass
    if target_type == "conflict" and payload.decision == "accepted":
        record.status = "resolved"
    else:
        record.status = payload.decision
    db.add(ReviewDecision(project_id=project_id, target_type=target_type, target_id=target_id, actor_id=user.id,
                          decision=payload.decision, rationale=payload.rationale))
    audit(db, "review_decision", user.id, project_id, target_type, target_id, {"decision": payload.decision})
    db.commit()
    return {"target_id": target_id, "decision": payload.decision, "status": record.status}


@app.post("/api/projects/{project_id}/publish")
def publish_version(project_id: str, db: Db, user: CurrentUser):
    ensure_project_access(db, project_id, user, write=True, review=True)
    project = db.get(Project, project_id)
    try:
        version = publish(db, project, user)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": version.id, "project_id": version.project_id, "version": version.version_number,
            "lineage_manifest": version.lineage_manifest, "created_at": version.created_at}


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
def export_version(project_id: str, version_id: str, db: Db, user: CurrentUser, format: str = "geojson"):
    ensure_project_access(db, project_id, user)
    version = db.get(PublishedVersion, version_id)
    if not version or version.project_id != project_id:
        raise HTTPException(status_code=404, detail="Published version not found")
    features = list(db.scalars(select(PublicationFeature).where(PublicationFeature.version_id == version_id)))
    if format == "geojson":
        data = {"type": "FeatureCollection", "features": [{"type": "Feature", "id": f.id,
                "geometry": f.geometry, "properties": {**f.attributes, "_lineage": f.lineage}} for f in features]}
        return data
    if format == "csv":
        fields = sorted({key for f in features for key in f.attributes})
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["publication_feature_id", *fields, "lineage"])
        writer.writeheader()
        for f in features:
            writer.writerow({"publication_feature_id": f.id, **f.attributes, "lineage": json.dumps(f.lineage)})
        return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="geosyncai-v{version.version_number}.csv"'})
    if format == "lineage":
        return {"version": version.version_number, "manifest": version.lineage_manifest,
                "features": [{"id": f.id, "lineage": f.lineage} for f in features]}
    raise HTTPException(status_code=400, detail="format must be geojson, csv, or lineage")


@app.get("/api/projects/{project_id}/exports/{format}")
def export_latest(project_id: str, format: str, db: Db, user: CurrentUser):
    """Resolve the latest immutable publication for browser export convenience."""
    ensure_project_access(db, project_id, user)
    version = db.scalar(select(PublishedVersion).where(PublishedVersion.project_id == project_id)
                        .order_by(PublishedVersion.version_number.desc()))
    if not version:
        raise HTTPException(status_code=404, detail="No published version is available for export")
    return export_version(project_id, version.id, db, user, format)


@app.post("/api/projects/{project_id}/jobs")
def create_job(project_id: str, job_type: str, payload: dict, background: BackgroundTasks, db: Db, user: CurrentUser,
               idempotency_key: str | None = None):
    ensure_project_access(db, project_id, user, write=True)
    if job_type not in {"match", "topology", "change_detection"}:
        raise HTTPException(status_code=400, detail="Unsupported job type")
    validate_job_payload(db, project_id, job_type, payload)
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.project_id == project_id, Job.idempotency_key == idempotency_key))
        if existing:
            return job_response(existing)
    job = Job(project_id=project_id, job_type=job_type, payload=payload, created_by=user.id, idempotency_key=idempotency_key)
    db.add(job)
    db.commit()
    dispatch_job(job.id, background)
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
            "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at}


def validate_dataset(db: Session, project_id: str, dataset_id: str) -> Dataset:
    dataset = db.get(Dataset, dataset_id)
    if not dataset or dataset.project_id != project_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


def validate_dataset_pair(db: Session, project_id: str, left_id: str, right_id: str) -> None:
    validate_dataset(db, project_id, left_id)
    validate_dataset(db, project_id, right_id)


def validate_job_payload(db: Session, project_id: str, job_type: str, payload: dict) -> None:
    try:
        if job_type == "topology":
            validate_dataset(db, project_id, payload["dataset_id"])
        elif job_type == "match":
            validate_dataset_pair(db, project_id, payload["left_dataset_id"], payload["right_dataset_id"])
        else:
            validate_dataset_pair(db, project_id, payload["before_dataset_id"], payload["after_dataset_id"])
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"Missing job payload field: {exc.args[0]}") from exc
