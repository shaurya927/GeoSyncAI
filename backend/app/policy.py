"""Append-only project processing policies retained as typed audit records."""
from sqlalchemy import select
from .models import AuditEvent

DEFAULT = {"version": 0, "max_distance": 75.0, "ambiguity_margin": 0.08,
           "geometry_tolerance": 0.5, "overlap_tolerance_m2": 0.01,
           "coverage_boundary": None}


def processing_policy(db, project_id):
    latest = db.scalar(select(AuditEvent).where(AuditEvent.project_id == project_id,
                       AuditEvent.action == "processing_policy_saved").order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()))
    return {**DEFAULT, **(latest.details if latest else {})}
