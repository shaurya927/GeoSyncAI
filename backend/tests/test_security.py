import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import os
from uuid import uuid4
import zipfile

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth import decode_access_token, hash_password, verify_password
from app.config import Settings, get_settings
from app.models import User, Job
from app.security import SecurityMiddleware, TrafficGuard, RedisCounters, client_identity
from app.services import _parse_shapefile_zip, _parse_csv, _parse_geojson

PASSPHRASE = "A separate synthetic passphrase 2026!"


def new_account(client, auth_token, role="viewer"):
    admin = {"Authorization": "Bearer " + auth_token("admin")}
    name = "security_" + uuid4().hex[:12]
    response = client.post('/api/admin/users', headers=admin, json={"username": name, "password": PASSPHRASE, "role": role})
    assert response.status_code == 201
    return response.json(), admin


def sign_in(client, name, password=PASSPHRASE):
    response = client.post('/api/auth/token', json={"username": name, "password": password})
    assert response.status_code == 200
    return response.json()['access_token']


def test_disabled_login_and_admin_boundaries(client, auth_token):
    account, admin = new_account(client, auth_token)
    bearer = {"Authorization": "Bearer " + sign_in(client, account['username'])}
    assert client.get('/api/admin/users', headers=bearer).status_code == 403
    assert client.get('/api/admin/security/traffic', headers=bearer).status_code == 403
    assert client.patch('/api/admin/users/'+account['id'], headers=admin, json={"role":"viewer", "is_active":False,"expected_auth_version":0,"rationale":"Disable synthetic account"}).status_code == 200
    assert client.get('/api/auth/me', headers=bearer).status_code == 401
    rejected = client.post('/api/auth/token', json={"username":account['username'],"password":PASSPHRASE})
    unknown = client.post('/api/auth/token', json={"username":"unknown_"+uuid4().hex,"password":PASSPHRASE})
    assert rejected.status_code == unknown.status_code == 401
    assert rejected.json()['detail'] == unknown.json()['detail']
    assert client.patch('/api/admin/users/'+account['id'], headers=admin, json={"role":"viewer", "is_active":True,"expected_auth_version":0,"rationale":"Stale update"}).status_code == 409
    assert client.patch('/api/admin/users/'+account['id'], headers=admin, json={"role":"viewer", "is_active":True,"expected_auth_version":1,"rationale":"Re-enable synthetic account"}).status_code == 200
    assert client.get('/api/auth/me', headers=bearer).status_code == 401
    assert sign_in(client, account['username'])


def test_logout_and_password_changes_revoke_real_sessions(client, auth_token):
    account, admin = new_account(client, auth_token)
    first, second = [sign_in(client, account['username']) for _ in range(2)]
    headers = {"Authorization":"Bearer "+first}
    assert client.post('/api/auth/logout', headers=headers).status_code == 200
    assert client.get('/api/auth/me', headers=headers).status_code == 401
    headers['Authorization'] = 'Bearer '+second
    assert client.get('/api/auth/me', headers=headers).status_code == 200
    replacement = PASSPHRASE + " replaced"
    assert client.post('/api/auth/password', headers=headers, json={'current_password':PASSPHRASE,'new_password':replacement}).status_code == 200
    assert client.get('/api/auth/me', headers=headers).status_code == 401
    assert client.post('/api/auth/token',json={'username':account['username'],'password':PASSPHRASE}).status_code == 401
    fresh = sign_in(client,account['username'],replacement)
    assert client.post('/api/admin/users/'+account['id']+'/revoke-sessions',headers=admin).status_code == 200
    assert client.get('/api/auth/me',headers={'Authorization':'Bearer '+fresh}).status_code == 401


@pytest.mark.parametrize('change', ['missing_exp','wrong_issuer','wrong_audience','expired','legacy'])
def test_token_claim_requirements(client, auth_token, change):
    settings = get_settings()
    claims = decode_access_token(auth_token('viewer'))
    if change == 'missing_exp': claims.pop('exp')
    if change == 'wrong_issuer': claims['iss']='another-service'
    if change == 'wrong_audience': claims['aud']='another-client'
    if change == 'expired': claims['exp']=datetime.now(timezone.utc)-timedelta(seconds=1)
    if change == 'legacy': claims.pop('sv')
    forged = jwt.encode(claims,settings.jwt_secret,algorithm='HS256')
    assert client.get('/api/auth/me',headers={'Authorization':'Bearer '+forged}).status_code == 401


def test_password_hash_upgrade_and_admin_self_protection(client, auth_token, db):
    import hashlib
    salt = b'legacy-test-salt!'
    legacy = 'pbkdf2_sha256$210000$'+salt.hex()+'$'+hashlib.pbkdf2_hmac('sha256',PASSPHRASE.encode(),salt,210000).hex()
    account = User(username='legacy_'+uuid4().hex[:10],password_hash=legacy,role='viewer')
    db.add(account);db.commit()
    assert verify_password(PASSPHRASE,legacy)
    sign_in(client,account.username)
    db.refresh(account)
    assert account.password_hash.startswith('pbkdf2_sha256$600000$')
    admin = {'Authorization':'Bearer '+auth_token('admin')}
    current = client.get('/api/auth/me',headers=admin).json()
    assert client.patch('/api/admin/users/'+current['id'],headers=admin,json={'role':'viewer','is_active':False,'expected_auth_version':0,'rationale':'Reject self-lockout'}).status_code == 409
    assert client.get('/api/auth/me',headers=admin).status_code == 200


def traffic_app(**overrides):
    settings = Settings(_env_file=None, rate_limit_enabled=True, jwt_secret=get_settings().jwt_secret,
                        security_redis_url=None, celery_broker_url=None, **overrides)
    application = FastAPI()
    application.state.hits = 0
    @application.post('/api/echo')
    async def echo(request:Request):
        application.state.hits += 1
        return {'bytes':len(await request.body())}
    @application.get('/api/echo')
    async def read():
        return {'ok':True}
    guard = TrafficGuard(settings)
    application.add_middleware(SecurityMiddleware,guard=guard)
    return application,guard


def test_rate_limit_ignores_forged_forwarded_headers_and_resets():
    application, guard = traffic_app(api_requests_per_minute=2)
    with TestClient(application) as client:
        assert client.get('/api/echo',headers={'X-Forwarded-For':'1.1.1.1','X-GeoSyncAI-Client-IP':'1.1.1.1'}).status_code == 200
        assert client.get('/api/echo',headers={'X-Forwarded-For':'2.2.2.2','X-GeoSyncAI-Client-IP':'2.2.2.2'}).status_code == 200
        rejected = client.get('/api/echo',headers={'X-GeoSyncAI-Client-IP':'3.3.3.3'})
        assert rejected.status_code == 429 and 1 <= int(rejected.headers['Retry-After']) <= 60
        assert rejected.headers['X-Content-Type-Options'] == 'nosniff'
        assert rejected.headers['Cache-Control'] == 'no-store'
    assert guard.active == 0


def test_explicit_proxy_trust_only():
    settings = Settings(_env_file=None,trusted_proxy_cidrs=['10.20.30.4/32'])
    scope={'client':('10.20.30.4',123),'headers':[(b'x-geosyncai-client-ip',b'203.0.113.7')]}
    assert client_identity(scope,settings)=='203.0.113.7'
    scope['client']=('10.20.30.5',123)
    assert client_identity(scope,settings)=='10.20.30.5'


def test_login_ip_budget_cannot_be_rotated_with_valid_tokens(client, auth_token):
    viewer, processor = auth_token('viewer'), auth_token('processor')
    application, _ = traffic_app(login_requests_per_minute=1)
    @application.post('/api/auth/token')
    def login_stub(): return {'ok': True}
    with TestClient(application) as isolated:
        assert isolated.post('/api/auth/token', headers={'Authorization': 'Bearer ' + viewer}).status_code == 200
        assert isolated.post('/api/auth/token', headers={'Authorization': 'Bearer ' + processor}).status_code == 429


@pytest.mark.parametrize('streamed',[False,True])
def test_body_limits_before_application_parsing(streamed):
    application, guard = traffic_app(max_json_body_bytes=1024)
    with TestClient(application) as client:
        body = (chunk for chunk in [b'x'*700,b'y'*700]) if streamed else b'x'*1400
        response = client.post('/api/echo',content=body)
        assert response.status_code == 413
        assert application.state.hits == 0
        assert client.post('/api/echo',content=b'ok').status_code == 200
    assert guard.active == 0


@pytest.mark.asyncio
async def test_concurrency_rejection_and_permit_recovery():
    application,guard=traffic_app(max_concurrent_requests=1)
    entered,release=asyncio.Event(),asyncio.Event()
    @application.get('/api/slow')
    async def slow():
        entered.set();await release.wait();return {'ok':True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='http://testserver') as client:
        first=asyncio.create_task(client.get('/api/slow'))
        await asyncio.wait_for(entered.wait(),3)
        assert (await client.get('/api/echo')).status_code==503
        release.set();assert (await first).status_code==200
        assert (await client.get('/api/echo')).status_code==200
    assert guard.active==0


def test_unknown_upload_rejected_before_body_consumption():
    application,guard=traffic_app()
    with TestClient(application) as client:
        assert client.post('/api/projects/x/datasets/upload',content=b'x'*4096).status_code==401
    assert guard.active==0


def test_account_throttle_and_restricted_origins(client,monkeypatch):
    settings=get_settings()
    monkeypatch.setattr(settings,'rate_limit_enabled',True)
    monkeypatch.setattr(settings,'account_login_requests_per_window',2)
    name='absent_'+uuid4().hex[:12]
    for _ in range(2): assert client.post('/api/auth/token',json={'username':name,'password':PASSPHRASE}).status_code==401
    assert client.post('/api/auth/token',json={'username':name,'password':PASSPHRASE}).status_code==429
    response=client.options('/api/auth/token',headers={'Origin':'https://attacker.example','Access-Control-Request-Method':'POST'})
    assert response.status_code==400
    assert 'access-control-allow-origin' not in response.headers
    assert client.get('/health',headers={'Host':'attacker.example'}).status_code==400


def test_archive_and_record_limits(monkeypatch):
    settings=get_settings()
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z: z.writestr('hidden.exe',b'not a shapefile')
    with pytest.raises(ValueError,match='Unsafe archive'): _parse_shapefile_zip(archive.getvalue())
    monkeypatch.setattr(settings,'max_source_records',2)
    with pytest.raises(ValueError,match='record limit'): _parse_csv(b'parcel_id\n1\n2\n3\n')
    with pytest.raises(ValueError,match='record limit'): _parse_geojson(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature'}]*3}).encode())
    with pytest.raises(ValueError,match='list'): _parse_geojson(b'{"type":"FeatureCollection","features":1}')


def test_redis_shared_atomic_budget():
    url=os.environ.get('BROKER_TEST_URL')
    if not url: pytest.skip('Redis service required for cross-worker atomic traffic test')
    stores=[RedisCounters(url),RedisCounters(url)]
    key='isolated-security-'+uuid4().hex
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda i:stores[i%2].consume(key,7,60),range(40)))
        assert sum(allowed for allowed,_ in results)==7
        assert all(1<=delay<=60 for _,delay in results)
    finally: stores[0].client.delete('geosyncai:security:rate:'+key)


def test_project_queue_capacity_and_idempotent_receipt(client,auth_token,db,monkeypatch):
    admin={'Authorization':'Bearer '+auth_token('admin')}
    project=client.post('/api/projects',headers=admin,json={'name':'Isolated queue capacity'}).json()['id']
    dataset=client.post(f'/api/projects/{project}/datasets/upload',headers=admin,files={'file':('queue.csv',b'parcel_id\nQUEUE-1\n','text/csv')}).json()['id']
    job=Job(project_id=project,job_type='topology',payload={'dataset_id':dataset},status='queued',idempotency_key='already-queued',configuration_hash='unused')
    db.add(job);db.commit()
    monkeypatch.setattr(get_settings(),'max_active_jobs_per_project',1)
    denied=client.post(f'/api/projects/{project}/jobs?job_type=topology',headers=admin,json={'dataset_id':dataset})
    assert denied.status_code==429 and denied.headers['Retry-After']=='30'
    assert db.get(Job,job.id).status=='queued'


def test_bad_framing_and_compression_rejected():
    application,_=traffic_app()
    with TestClient(application) as client:
        assert client.post('/api/echo',content=b'ok',headers={'Content-Length':'5'}).status_code==400
        assert client.post('/api/echo',content=b'ok',headers={'Content-Encoding':'gzip'}).status_code==415
    assert application.state.hits==0


@pytest.mark.asyncio
async def test_slow_body_timeout_releases_capacity():
    application,guard=traffic_app(request_read_timeout_seconds=1)
    messages=[]
    async def slow_receive():
        await asyncio.sleep(2)
        return {'type':'http.request','body':b'','more_body':False}
    async def send(message): messages.append(message)
    scope={'type':'http','asgi':{'version':'3.0'},'method':'POST','path':'/api/echo','headers':[], 'client':('127.0.0.1',1),'scheme':'http','query_string':b''}
    await application(scope,slow_receive,send)
    assert next(message['status'] for message in messages if message['type']=='http.response.start')==408
    assert guard.active==0 and application.state.hits==0
