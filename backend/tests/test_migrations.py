import os
from pathlib import Path
import subprocess
import sys


def test_original_schema_upgrade_and_restart_preserve_data(tmp_path):
    root = Path(__file__).resolve().parents[2]
    database = tmp_path / 'legacy.db'
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{database}', 'PYTHONPATH': str(root / 'backend') + os.pathsep + str(root)}
    original = """
from sqlalchemy import create_engine, text
from migrations.baseline_schema import baseline_metadata
import os
engine=create_engine(os.environ['DATABASE_URL'])
baseline_metadata().create_all(engine)
with engine.begin() as db:
    db.execute(text("INSERT INTO users (id,username,password_hash,role,is_active,created_at) VALUES ('retained','retained','test','viewer',1,'2024-01-01')"))
"""
    subprocess.run([sys.executable, '-c', original], env=env, cwd=root, check=True)
    for _ in range(2):
        subprocess.run([sys.executable, '-m', 'alembic', '-c', str(root / 'alembic.ini'), 'upgrade', 'head'], env=env, cwd=root, check=True)
    check = """
from app.db import SessionLocal
from app.models import User, Project
from sqlalchemy import text
with SessionLocal() as db:
    assert db.get(User,'retained').username=='retained'
    assert db.scalar(text('SELECT version_num FROM alembic_version'))=='0005_raster_assets'
    assert db.execute(text('SELECT workflow_revision,validated_revision FROM projects')).all()==[]
"""
    subprocess.run([sys.executable, '-c', check], env=env, cwd=root, check=True)
