"""Durable worker stages with fenced ownership and transactional effects.

Message delivery is at-least-once. A lease token is the fencing authority: only
the current unexpired owner may finalize a stage, and domain effects plus the
successful receipt commit in one database transaction. An expired owner can no
longer overwrite a newer retry.
"""
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import BackgroundTasks
from sqlalchemy import and_, or_, select, update

from .config import get_settings
from .db import SessionLocal
from .models import Job, Dataset
from .services import run_change_detection, run_matching, run_topology


def configuration_hash(db, job_type, payload):
    from .policy import processing_policy
    dataset = next((db.get(Dataset, str(v)) for k, v in payload.items() if k.endswith("dataset_id")), None)
    if dataset and payload.get("policy_version", 0) != processing_policy(db, dataset.project_id)["version"]:
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


def _heartbeat(job_id: str, owner_token: str, stop: threading.Event) -> None:
    settings = get_settings()
    interval = max(1, settings.job_heartbeat_seconds)
    while not stop.wait(interval):
        now = datetime.now(timezone.utc)
        with SessionLocal() as heartbeat_db:
            heartbeat_db.execute(update(Job).where(Job.id == job_id, Job.status == "running",
                                                    Job.owner_token == owner_token)
                                  .values(heartbeat_at=now,
                                          lease_expires_at=now + timedelta(seconds=max(settings.job_lease_seconds, interval * 2))))
            heartbeat_db.commit()


def _finalize_cancelled(job_id: str, owner_token: str, reason: str) -> None:
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.owner_token == owner_token,
                                     Job.cancellation_requested.is_(True))
                   .values(status="cancelled", stage="cancelled", error=reason,
                           owner_token=None, lease_expires_at=None, finished_at=datetime.now(timezone.utc)))
        db.commit()


def execute_job(job_id: str) -> None:
    settings = get_settings()
    owner_token = uuid4().hex
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=settings.job_lease_seconds)
    with SessionLocal() as db:
        try:
            claim_condition = or_(
                Job.status == "queued",
                and_(Job.status == "failed", Job.attempts < Job.max_attempts),
                and_(Job.status == "running", or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now)),
            )
            claimed = db.execute(update(Job).where(Job.id == job_id, claim_condition)
                                 .values(status="running", stage="claimed", owner_token=owner_token,
                                         lease_expires_at=lease_until, heartbeat_at=now,
                                         started_at=now, attempts=Job.attempts + 1,
                                         warnings=[], error=None))
            if not claimed.rowcount:
                db.rollback()
                return
            db.commit()
            job = db.get(Job, job_id)
            if not job or job.owner_token != owner_token:
                return
            if job.configuration_hash != configuration_hash(db, job.job_type, job.payload):
                raise ValueError("Job input metadata or mapping changed; submit a new job with current inputs")
            if job.cancellation_requested:
                db.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.owner_token == owner_token)
                           .values(status="cancelled", stage="cancelled", owner_token=None,
                                   lease_expires_at=None, finished_at=datetime.now(timezone.utc)))
                db.commit()
                return
            job.stage = {"match": "matching", "topology": "topology", "change_detection": "change_detection"}.get(job.job_type, job.job_type)
            job.progress = {"known": False, "message": "Stage running; final counts commit with effects"}
            job.heartbeat_at = datetime.now(timezone.utc)
            db.commit()
            db.info["job_transaction"] = True
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(target=_heartbeat, args=(job_id, owner_token, heartbeat_stop), daemon=True)
            heartbeat_thread.start()
            try:
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
                # The conditional update is the final fencing check. If a cancel
                # or newer owner won the race, this transaction rolls back all stage effects.
                final = db.execute(update(Job).where(Job.id == job_id, Job.status == "running",
                                                      Job.owner_token == owner_token,
                                                      Job.lease_expires_at >= datetime.now(timezone.utc),
                                                      Job.cancellation_requested.is_(False))
                                   .values(status="succeeded", result=result, error=None, stage="completed",
                                           progress={"known": True, "result": result},
                                           heartbeat_at=datetime.now(timezone.utc), owner_token=None,
                                           lease_expires_at=None, finished_at=datetime.now(timezone.utc))
                                   .execution_options(synchronize_session=False))
                if not final.rowcount:
                    db.rollback()
                    _finalize_cancelled(job_id, owner_token, "Cancellation or ownership changed before commit")
                    return
                db.commit()
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=max(1, settings.job_heartbeat_seconds + 1))
        except Exception as exc:
            db.rollback()
            # A stale/expired owner must never overwrite a replacement owner or
            # a terminal cancellation/success receipt.
            current = db.get(Job, job_id)
            if current and current.status == "running" and current.owner_token == owner_token:
                if current.cancellation_requested:
                    current.status = "cancelled"; current.stage = "cancelled"; current.error = str(exc)
                else:
                    current.status = "failed"; current.stage = "failed"; current.error = str(exc)
                    current.warnings = [str(exc)]
                current.owner_token = None; current.lease_expires_at = None; current.finished_at = datetime.now(timezone.utc)
                db.commit()


settings = get_settings()
celery_app = None
if settings.celery_broker_url:
    from celery import Celery
    celery_app = Celery("geosyncai", broker=settings.celery_broker_url)
    celery_app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True,
                           worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True,
                           task_time_limit=max(settings.job_lease_seconds * 4, 300))
    celery_app.task(name="geosyncai.execute_job")(execute_job)


def dispatch_job(job_id: str, background: BackgroundTasks) -> None:
    if celery_app:
        celery_app.send_task("geosyncai.execute_job", args=[job_id])
    else:
        background.add_task(execute_job, job_id)


def recover_jobs() -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        job_ids = list(db.scalars(select(Job.id).where(or_(Job.status == "queued",
                                                           and_(Job.status == "running", Job.lease_expires_at < now)))))
    for job_id in job_ids:
        if celery_app:
            celery_app.send_task("geosyncai.execute_job", args=[job_id])
        else:
            threading.Thread(target=execute_job, args=(job_id,), daemon=True).start()
