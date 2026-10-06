"""Transactional durable stages: side effects and completion commit together.

A conditional UPDATE holds the job row lock until the stage commits. Duplicate
workers wait and recheck succeeded status. A killed worker rolls back both the
claim and effects, leaving the queued receipt recoverable.
"""
import threading
import hashlib
import json
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlalchemy import select, update

from .config import get_settings
from .db import SessionLocal
from .models import Job, Dataset
from .services import run_change_detection, run_matching, run_topology


def configuration_hash(db, job_type, payload):
    from .policy import processing_policy
    dataset = next((db.get(Dataset, str(v)) for k,v in payload.items() if k.endswith('dataset_id')), None)
    if dataset and payload.get('policy_version', 0) != processing_policy(db, dataset.project_id)['version']:
        raise ValueError("Processing policy changed; submit a new job")
    ids = sorted({str(value) for key, value in payload.items() if key.endswith("dataset_id")})
    inputs = []
    for dataset_id in ids:
        dataset = db.get(Dataset, dataset_id)
        if not dataset:
            raise ValueError("Job input no longer exists")
        inputs.append((dataset.id, dataset.content_hash, dataset.declared_crs, dataset.schema_mapping_version))
    return hashlib.sha256(json.dumps({"job_type": job_type, "payload": payload, "inputs": inputs,
                                     "rules": "rules-v2"}, sort_keys=True).encode()).hexdigest()


def execute_job(job_id: str) -> None:
    with SessionLocal() as db:
        try:
            claimed = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(("queued", "running", "failed")))
                                 .values(status="running", stage="claimed", heartbeat_at=datetime.now(timezone.utc),
                                         started_at=datetime.now(timezone.utc), attempts=Job.attempts + 1,
                                         warnings=[]))
            if not claimed.rowcount:
                db.rollback()
                return
            # Make truthful stage/heartbeat telemetry visible before the potentially
            # long stage. Effects still commit atomically with succeeded status.
            db.commit()
            job = db.get(Job, job_id)
            if job.configuration_hash != configuration_hash(db, job.job_type, job.payload):
                raise ValueError("Job input metadata or mapping changed; submit a new job with current inputs")
            if job.cancellation_requested:
                job.status = "cancelled"; job.stage = "cancelled"; job.finished_at = datetime.now(timezone.utc)
                db.commit()
                return
            job.stage = {"match": "matching", "topology": "topology", "change_detection": "change_detection"}.get(job.job_type, job.job_type)
            job.progress = {"known": False, "message": "Stage running; final counts are committed with effects"}
            job.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            db.info["job_transaction"] = True
            payload = job.payload
            if job.job_type == "match":
                result = run_matching(db, job.project_id, payload["left_dataset_id"], payload["right_dataset_id"],
                                      payload.get("id_fields", []), payload.get("max_distance", 75.0),
                                      payload.get("ambiguity_margin", 0.08), payload.get("namespace_fields", []))
            elif job.job_type == "topology":
                result = run_topology(db, job.project_id, payload["dataset_id"])
            elif job.job_type == "change_detection":
                result = run_change_detection(db, job.project_id, payload["before_dataset_id"], payload["after_dataset_id"],
                                              payload.get("id_fields", []), payload.get("geometry_tolerance", 0.5))
            else:
                raise ValueError(f"Unsupported job type: {job.job_type}")
            if job.cancellation_requested:
                raise RuntimeError("Cancellation requested before stage commit")
            job.status, job.result, job.error = "succeeded", result, None
            job.stage = "completed"
            job.progress = {"known": True, "result": result}
            job.heartbeat_at = datetime.now(timezone.utc)
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
        except Exception as exc:
            db.rollback()
            # A failed receipt is persisted separately only after all stage
            # effects have been rolled back. It never reports partial success.
            db.execute(update(Job).where(Job.id == job_id, Job.status != "succeeded")
                       .values(status="failed", stage="failed", error=str(exc),
                               warnings=[str(exc)], heartbeat_at=datetime.now(timezone.utc),
                                finished_at=datetime.now(timezone.utc)))
            db.commit()


settings = get_settings()
celery_app = None
if settings.celery_broker_url:
    from celery import Celery
    celery_app = Celery("geosyncai", broker=settings.celery_broker_url)
    celery_app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True,
                           worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True)
    celery_app.task(name="geosyncai.execute_job")(execute_job)


def dispatch_job(job_id: str, background: BackgroundTasks) -> None:
    if celery_app:
        celery_app.send_task("geosyncai.execute_job", args=[job_id])
    else:
        background.add_task(execute_job, job_id)


def recover_jobs() -> None:
    with SessionLocal() as db:
        job_ids = list(db.scalars(select(Job.id).where(Job.status.in_(("queued", "running")))))
    for job_id in job_ids:
        if celery_app:
            celery_app.send_task("geosyncai.execute_job", args=[job_id])
        else:
            threading.Thread(target=execute_job, args=(job_id,), daemon=True).start()
