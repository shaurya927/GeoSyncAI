"""Durable-job seam. Uses Celery when configured, otherwise FastAPI background work."""
import threading
from datetime import datetime, timezone
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .db import SessionLocal
from .models import Job
from .services import run_change_detection, run_matching, run_topology


def execute_job(job_id: str) -> None:
    db: Session = SessionLocal()
    job = db.get(Job, job_id)
    if not job:
        db.close()
        return
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    db.commit()
    try:
        payload = job.payload
        if job.job_type == "match":
            result = run_matching(db, job.project_id, payload["left_dataset_id"], payload["right_dataset_id"],
                                  payload.get("id_fields", []), payload.get("max_distance", 75.0), payload.get("ambiguity_margin", 0.08))
        elif job.job_type == "topology":
            result = run_topology(db, job.project_id, payload["dataset_id"])
        elif job.job_type == "change_detection":
            result = run_change_detection(db, job.project_id, payload["before_dataset_id"], payload["after_dataset_id"],
                                          payload.get("id_fields", []), payload.get("geometry_tolerance", 1e-8))
        else:
            raise ValueError(f"Unsupported job type: {job.job_type}")
        job.status, job.result = "succeeded", result
    except Exception as exc:  # durable receipt retains failure for later inspection
        job.status, job.error = "failed", str(exc)
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.close()


settings = get_settings()
celery_app = None
if settings.celery_broker_url:
    try:
        from celery import Celery
        celery_app = Celery("geosyncai", broker=settings.celery_broker_url)
        celery_app.task(name="geosyncai.execute_job")(execute_job)
    except ImportError:
        celery_app = None


def dispatch_job(job_id: str, background: BackgroundTasks) -> None:
    if celery_app:
        celery_app.send_task("geosyncai.execute_job", args=[job_id])
    else:
        background.add_task(execute_job, job_id)


def recover_jobs() -> None:
    """Replay persisted queued/interrupted local jobs after an application restart."""
    if celery_app:
        return
    db = SessionLocal()
    job_ids = list(db.scalars(select(Job.id).where(Job.status.in_(("queued", "running")))))
    db.close()
    for job_id in job_ids:
        threading.Thread(target=execute_job, args=(job_id,), daemon=True).start()
