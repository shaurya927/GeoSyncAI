"""Account controls: authorization uses current database state on every request."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import (CurrentUser, decode_access_token, hash_password, oauth2_scheme,
                   require_roles, verify_password)
from .db import get_db
from .models import RevokedToken, User
from .schemas import PasswordChangeRequest, UserCreateRequest, UserSecurityRequest
from .security import TrafficUnavailable
from .services import audit

router = APIRouter(prefix="/api")
Db = Annotated[Session, Depends(get_db)]
Admin = Annotated[User, Depends(require_roles("admin"))]


def user_summary(user):
    return {"id": user.id, "username": user.username, "role": user.role,
            "is_active": user.is_active, "auth_version": user.auth_version, "created_at": user.created_at}


@router.post("/auth/logout")
def logout(user: CurrentUser, db: Db, token: Annotated[str, Depends(oauth2_scheme)]):
    claims = decode_access_token(token)
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(timezone.utc)))
    db.add(RevokedToken(jti=claims["jti"], user_id=user.id, expires_at=datetime.fromtimestamp(claims["exp"], timezone.utc)))
    audit(db, "session_revoked", user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # Concurrent logout of the same token is already revoked.
    return {"revoked": True}


@router.post("/auth/password")
def change_password(payload: PasswordChangeRequest, user: CurrentUser, db: Db):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(401, "Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(422, "Choose a different password")
    changed = db.execute(update(User).where(User.id == user.id, User.auth_version == user.auth_version)
                         .values(password_hash=hash_password(payload.new_password), auth_version=User.auth_version + 1))
    if changed.rowcount != 1:
        raise HTTPException(409, "Account changed; sign in again before changing the password")
    audit(db, "password_changed_sessions_revoked", user.id)
    db.commit()
    return {"changed": True, "reauthentication_required": True}


@router.get("/admin/users")
def users(db: Db, admin: Admin):
    return [user_summary(user) for user in db.scalars(select(User).order_by(User.username).limit(1000))]


@router.post("/admin/users", status_code=201)
def create_user(payload: UserCreateRequest, db: Db, admin: Admin):
    user = User(username=payload.username, password_hash=hash_password(payload.password), role=payload.role)
    db.add(user)
    audit(db, "account_created", admin.id, target_type="user", details={"username": payload.username, "role": payload.role})
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Username already exists") from exc
    return user_summary(user)


@router.patch("/admin/users/{user_id}")
def update_user(user_id: str, payload: UserSecurityRequest, db: Db, admin: Admin):
    if user_id == admin.id and (not payload.is_active or payload.role != "admin"):
        raise HTTPException(409, "An administrator cannot disable or demote their own account")
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Account not found")
    if target.role == 'admin' and (not payload.is_active or payload.role != 'admin'):
        # Serialize administrator removals, including SQLite, and recheck the
        # caller after waiting so two administrators cannot disable each other.
        db.execute(update(User).where(User.role == 'admin').values(auth_version=User.auth_version))
        db.refresh(admin)
        if not admin.is_active or admin.role != 'admin':
            raise HTTPException(403, "Administrator access was revoked")
        active_admins = list(db.scalars(select(User.id).where(User.role == 'admin', User.is_active.is_(True))))
        if target.is_active and len(active_admins) <= 1:
            raise HTTPException(409, "At least one active administrator must remain")
    changed = db.execute(update(User).where(User.id == user_id, User.auth_version == payload.expected_auth_version)
                         .values(role=payload.role, is_active=payload.is_active, auth_version=User.auth_version + 1)
                         .execution_options(synchronize_session="fetch"))
    if changed.rowcount != 1:
        raise HTTPException(409, "Account changed; refresh before retrying")
    audit(db, "account_security_changed_sessions_revoked", admin.id, target_type="user", target_id=user_id,
          details={"role": payload.role, "is_active": payload.is_active, "rationale": payload.rationale})
    db.commit()
    return user_summary(target)


@router.post("/admin/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: str, db: Db, admin: Admin):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Account not found")
    db.execute(update(User).where(User.id == user_id).values(auth_version=User.auth_version + 1))
    audit(db, "all_account_sessions_revoked", admin.id, target_type="user", target_id=user_id)
    db.commit()
    return {"revoked": True}


@router.get("/admin/security/traffic")
def traffic(request: Request, admin: Admin):
    try:
        return request.state.traffic_guard.snapshot()
    except TrafficUnavailable as exc:
        raise HTTPException(503, "Traffic metrics unavailable") from exc
