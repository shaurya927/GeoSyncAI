"""Optional real broker/worker test; CI supplies Redis and PostGIS."""
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.db import SessionLocal
from app.models import Job, MatchProposal
from app.tasks import configuration_hash
from test_integrity import prepare_pair


@pytest.mark.skipif(not os.environ.get('BROKER_TEST_URL'), reason='BROKER_TEST_URL is required for real Celery worker test')
def test_real_worker_duplicate_delivery(client, auth_token, tmp_path):
    from celery import Celery
    project, admin, reviewer, left, right, _, _ = prepare_pair(client, auth_token, 'real-worker')
    payload = {'left_dataset_id':left['id'],'right_dataset_id':right['id']}
    with SessionLocal() as db:
        job = Job(project_id=project, job_type='match', payload=payload, configuration_hash=configuration_hash(db,'match',payload))
        db.add(job); db.commit(); job_id = job.id
    queue = f'geosyncai-test-{uuid4()}'
    broker = os.environ['BROKER_TEST_URL']
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ,'CELERY_BROKER_URL':broker}
    with (tmp_path/'worker.log').open('w') as log:
        worker = subprocess.Popen([sys.executable,'-m','celery','-A','app.tasks.celery_app','worker',
                                   '--pool=solo','--concurrency=1','--queues',queue,'--loglevel=INFO'],
                                  cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            celery = Celery('test-dispatch',broker=broker)
            for _ in range(2): celery.send_task('geosyncai.execute_job',args=[job_id],queue=queue)
            for _ in range(100):
                assert worker.poll() is None, (tmp_path/'worker.log').read_text()
                with SessionLocal() as db:
                    receipt = db.get(Job,job_id)
                    if receipt.status=='succeeded': break
                    assert receipt.status != 'failed', receipt.error
                time.sleep(.2)
            else:
                pytest.fail((tmp_path/'worker.log').read_text())
            time.sleep(.3)
            with SessionLocal() as db:
                assert db.get(Job,job_id).attempts==1
                assert db.scalar(select(func.count()).select_from(MatchProposal).where(MatchProposal.project_id==project))==2
        finally:
            worker.terminate()
            worker.wait(timeout=15)
