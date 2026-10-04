import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .db import get_db
from .models import Project, ProjectMember, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
ROLES = {"viewer", "processor", "reviewer", "admin"}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return f"pbkdf2_sha256$210000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_access_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": user.id, "username": user.username, "role": user.role,
               "iat": now, "exp": now + timedelta(minutes=settings.jwt_expire_minutes)}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: Annotated[Session, Depends(get_db)]) -> User:
    credentials_error = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials",
                                      headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        user_id = payload.get("sub")
    except jwt.PyJWTError as exc:
        raise credentials_error from exc
    user = db.get(User, user_id) if user_id else None
    if not user or not user.is_active:
        raise credentials_error
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: str):
    def dependency(user: CurrentUser) -> User:
        if user.role not in roles and user.role != "admin":
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user
    return dependency


def ensure_project_access(db: Session, project_id: str, user: User, write: bool = False, review: bool = False) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.role == "admin":
        return project
    member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id))
    if not member:
        raise HTTPException(status_code=403, detail="Project access denied")
    project_role = member.project_role
    if review and (user.role != "reviewer" or project_role not in {"reviewer", "owner"}):
        raise HTTPException(status_code=403, detail="Reviewer role required")
    if write and (user.role not in {"processor", "reviewer"} or project_role not in {"processor", "reviewer", "owner"}):
        raise HTTPException(status_code=403, detail="Write access denied")
    return project
