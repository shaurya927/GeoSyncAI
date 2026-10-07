import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .db import get_db
from .models import Project, ProjectMember, User, RevokedToken

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
ROLES = {"viewer", "processor", "reviewer", "steward", "field", "citizen", "admin"}
PASSWORD_ROUNDS = 600_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 10_000 <= int(rounds) <= 2_000_000 or len(password.encode()) > 1024:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


DUMMY_PASSWORD_HASH = hash_password("unusable-random-login-" + os.urandom(24).hex())


def password_needs_rehash(encoded: str) -> bool:
    return encoded.split("$")[1] != str(PASSWORD_ROUNDS)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    if len(token) > 8192:
        raise jwt.InvalidTokenError("Token too long")
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                         issuer=settings.jwt_issuer, audience=settings.jwt_audience,
                         options={"require": ["sub", "exp", "iat", "nbf", "iss", "aud", "jti", "sv"]})
    if type(payload["sv"]) is not int or not isinstance(payload["jti"], str) or len(payload["jti"]) != 36:
        raise jwt.InvalidTokenError("Invalid session claims")
    return payload


def create_access_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user.id, "username": user.username, "role": user.role,
               "iat": now, "nbf": now, "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
               "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
               "jti": str(uuid4()), "sv": user.auth_version}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: Annotated[Session, Depends(get_db)]) -> User:
    credentials_error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials",
                                      headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
    except jwt.PyJWTError as exc:
        raise credentials_error from exc
    user = db.get(User, user_id) if user_id else None
    if not user or not user.is_active or user.auth_version != payload["sv"] or db.get(RevokedToken, payload["jti"]):
        raise credentials_error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: str):
    def dependency(user: CurrentUser) -> User:
        if user.role not in roles and user.role != "admin":
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user
    return dependency


CAPABILITY_ROLES = {
    "project": {"viewer", "processor", "reviewer", "steward", "field", "citizen", "admin"},
    "departmental": {"viewer", "processor", "reviewer", "steward", "admin"},
    "fieldwork": {"field", "processor", "reviewer", "steward", "admin"},
    "citizen": {"citizen", "reviewer", "steward", "admin"},
}
PROJECT_CAPABILITY_ROLES = {
    "project": {"viewer", "member", "processor", "reviewer", "steward", "field", "citizen", "owner"},
    "departmental": {"viewer", "member", "processor", "reviewer", "steward", "owner"},
    "fieldwork": {"field", "processor", "reviewer", "steward", "owner"},
    "citizen": {"citizen", "reviewer", "steward", "owner"},
}


def capability_allowed(user: User, member: ProjectMember | None, capability: str) -> bool:
    return user.role == "admin" or bool(member and user.role in CAPABILITY_ROLES.get(capability, set())
                                        and member.project_role in PROJECT_CAPABILITY_ROLES.get(capability, set()))


def project_member(db: Session, project_id: str, user_id: str) -> ProjectMember | None:
    return db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id,
                                                  ProjectMember.user_id == user_id))


def ensure_project_access(db: Session, project_id: str, user: User, write: bool = False, review: bool = False,
                          capability: str = "departmental") -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "admin":
        return project
    member = project_member(db, project_id, user.id)
    if not member:
        raise HTTPException(status_code=403, detail="Project access denied")
    allowed_roles = CAPABILITY_ROLES.get(capability)
    if allowed_roles is None:
        raise HTTPException(status_code=500, detail=f"Unknown access capability: {capability}")
    if not capability_allowed(user, member, capability):
        raise HTTPException(status_code=403, detail=f"This account has no {capability} capability")
    project_role = member.project_role
    if review and (user.role not in {"reviewer", "steward"} or project_role not in {"reviewer", "steward", "owner"}):
        raise HTTPException(status_code=403, detail="Reviewer role required")
    if write and (user.role not in {"processor", "reviewer", "steward"} or project_role not in {"processor", "reviewer", "steward", "owner"}):
        raise HTTPException(status_code=403, detail="Write access denied")
    return project


def ensure_classification_access(db: Session, project_id: str, user: User, classification: str) -> None:
    ensure_project_access(db, project_id, user)
    if classification == "restricted":
        ensure_project_access(db, project_id, user, review=True)


def ensure_project_membership(db: Session, project_id: str, user: User) -> Project:
    return ensure_project_access(db, project_id, user, capability="project")


def utc_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return current <= datetime.now(timezone.utc)
