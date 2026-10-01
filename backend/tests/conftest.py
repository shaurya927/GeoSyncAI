import os
import sys
from pathlib import Path

TEST_DB = Path(__file__).parent / "test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["STORAGE_DIR"] = str(Path(__file__).parent / "storage")
os.environ["JWT_SECRET"] = "test-secret-longer-than-thirty-two-bytes"
os.environ["AUTO_BOOTSTRAP"] = "true"
sys.path.insert(0, str(Path(__file__).parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def token(client: TestClient, username: str) -> str:
    response = client.post("/api/auth/token", json={"username": username, "password": username})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth_token(client):
    return lambda username: token(client, username)
