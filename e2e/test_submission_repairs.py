"""Production UI + real HTTP API; isolated database/storage, no mocked app APIs.

Build first: VITE_API_BASE_URL=/api npm run build (in frontend).
"""
import json
import base64
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright
from shapely import union_all
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
GEOMETRY = {"type": "Polygon", "coordinates": [[[73,20],[73.002,20],[73.002,20.001],[73.001,20.001],[73.001,20.002],[73,20.002],[73,20]]]}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("production-repairs")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0)); api_port = sock.getsockname()[1]
    env = {**os.environ,"DATABASE_URL":f"sqlite:///{(workspace/'app.db').as_posix()}","STORAGE_DIR":str(workspace/'storage'),"AUTO_BOOTSTRAP":"true","JWT_SECRET":"production-browser-isolated-test-secret-32","CELERY_BROKER_URL":""}
    log = (workspace/"api.log").open("w")
    process = subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--host","127.0.0.1","--port",str(api_port)],cwd=ROOT/"backend",env=env,stdout=log,stderr=subprocess.STDOUT)
    api = httpx.Client(base_url=f"http://127.0.0.1:{api_port}",timeout=30)
    proxy = httpx.Client(base_url=f"http://127.0.0.1:{api_port}",timeout=30)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/"frontend"/"dist"),**kwargs)
        def end_headers(self):
            if not self.path.startswith('/api/'):
                csp = re.search(r'Content-Security-Policy "([^"]+)"', (ROOT/'frontend'/'security-headers.conf').read_text()).group(1)
                self.send_header('Content-Security-Policy', csp)
                self.send_header('X-Frame-Options', 'DENY')
            super().end_headers()
        def forward(self):
            response = proxy.request(self.command,self.path,content=self.rfile.read(int(self.headers.get("Content-Length","0"))),headers={key:value for key,value in self.headers.items() if key.lower() in {"authorization","content-type"}})
            self.send_response(response.status_code)
            for key,value in response.headers.items():
                if key.lower() not in {"content-length","transfer-encoding","content-encoding"}: self.send_header(key,value)
            self.send_header("Content-Length",str(len(response.content))); self.end_headers(); self.wfile.write(response.content)
        def do_GET(self):
            if self.path.startswith("/api/"): self.forward()
            else: super().do_GET()
        def do_POST(self): self.forward()
        def do_PATCH(self): self.forward()
        def log_message(self,*args): pass
    server = None
    try:
        for _ in range(120):
            try:
                if api.get("/health").status_code == 200: break
            except httpx.TransportError: pass
            if process.poll() is not None: raise RuntimeError((workspace/"api.log").read_text())
            time.sleep(.2)
        else: raise RuntimeError("Backend readiness timeout")
        assert (ROOT/"frontend"/"dist"/"index.html").exists(), "Build frontend with VITE_API_BASE_URL=/api first"
        server = ThreadingHTTPServer(("127.0.0.1",0),Handler); thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        token = api.post("/api/auth/token",json={"username":"admin","password":"admin"}).json()["access_token"]
        api.headers["Authorization"] = f"Bearer {token}"
        yield api,f"http://127.0.0.1:{server.server_port}",workspace
    finally:
        if server: server.shutdown(); server.server_close()
        proxy.close(); api.close(); process.terminate(); process.wait(timeout=15); log.close()


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--use-gl=angle","--use-angle=swiftshader","--enable-unsafe-swiftshader"])
        yield browser
        browser.close()


def checked(response,status=200):
    assert response.status_code == status, response.text
    return response.json()


def login(page,origin,username,project):
    page.goto(origin,wait_until="networkidle")
    page.get_by_label("Username",exact=True).fill(username); page.get_by_label("Password",exact=True).fill(username)
    page.get_by_role("button",name="Sign in to workspace").click()
    expect(page.get_by_role("button",name="Sign out",exact=True)).to_be_visible()
    page.get_by_label("Project",exact=True).select_option(project)


def test_production_security_account_controls_and_revocation(stack,browser):
    api,origin,_=stack
    project=checked(api.post('/api/projects',json={'name':'Security browser acceptance'}))['id']
    username='secure_'+project[:8]
    passphrase='Browser acceptance passphrase 2026!'
    replacement='Replacement browser passphrase 2026!'
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    tile=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGN48f7lfwAJQwPAmGARvgAAAABJRU5ErkJggg==')
    page.route('https://tile.openstreetmap.org/**',lambda route:route.fulfill(status=200,content_type='image/png',body=tile))
    try:
        login(page,origin,'admin',project)
        assert page.evaluate("localStorage.getItem('geosyncai_token') === null && !!sessionStorage.getItem('geosyncai_token')")
        admin_session=page.evaluate("sessionStorage.getItem('geosyncai_token')")
        page.get_by_role('button',name='Account & security',exact=True).click()
        expect(page.get_by_role('heading',name='Traffic protection',exact=True)).to_be_visible()
        page.get_by_label('New username').fill(username)
        page.get_by_label('Initial passphrase').fill(passphrase)
        page.get_by_role('button',name='Create secure account',exact=True).click()
        expect(page.locator('body')).to_contain_text('Account created.')
        checked(api.post(f'/api/projects/{project}/members',json={'username':username,'project_role':'viewer'}))
        page.get_by_role('button',name='Sign out',exact=True).click()
        expect(page.get_by_role('button',name='Sign in to workspace')).to_be_visible()
        assert api.get('/api/auth/me',headers={'Authorization':'Bearer '+admin_session}).status_code==401
        page.get_by_label('Username',exact=True).fill(username);page.get_by_label('Password',exact=True).fill(passphrase)
        page.get_by_role('button',name='Sign in to workspace').click()
        expect(page.get_by_role('button',name='Sign out',exact=True)).to_be_visible()
        user_session=page.evaluate("sessionStorage.getItem('geosyncai_token')")
        second=checked(api.post('/api/auth/token',json={'username':username,'password':passphrase}))['access_token']
        page.get_by_role('button',name='Account & security',exact=True).click()
        expect(page.get_by_role('heading',name='Create an account',exact=True)).to_have_count(0)
        page.get_by_label('Current passphrase').fill(passphrase);page.get_by_label('New passphrase',exact=True).fill(replacement)
        page.get_by_role('button',name='Change passphrase and revoke sessions').click()
        expect(page.get_by_role('button',name='Sign in to workspace')).to_be_visible()
        for token in (user_session,second):
            assert api.get('/api/auth/me',headers={'Authorization':'Bearer '+token}).status_code==401
        assert api.post('/api/auth/token',json={'username':username,'password':passphrase}).status_code==401
        assert api.post('/api/auth/token',json={'username':username,'password':replacement}).status_code==200
        login(page,origin,'admin',project)
        page.get_by_role('button',name='Account & security',exact=True).click()
        page.get_by_label('Account change rationale').fill('Disable isolated browser acceptance account')
        page.get_by_label('Active account '+username,exact=True).uncheck()
        page.get_by_role('button',name='Save access for '+username,exact=True).click()
        expect(page.locator('body')).to_contain_text('Access updated and previous sessions revoked.')
        assert api.post('/api/auth/token',json={'username':username,'password':replacement}).status_code==401
        headers=page.request.get(origin).headers
        assert "frame-ancestors 'none'" in headers['content-security-policy'] and headers['x-frame-options']=='DENY'
        assert api.get('/api/auth/me').headers['cache-control']=='no-store'
        assert not errors,errors
    finally:page.close()


def upload(api,prefix,name,body,mime="application/geo+json"):
    ds = checked(api.post(prefix+"/datasets/upload",files={"file":(name,body,mime)},data={"declared_crs":"EPSG:4326"}),201)
    features = checked(api.get(prefix+f"/datasets/{ds['id']}/features"))
    checked(api.post(prefix+f"/datasets/{ds['id']}/mapping",json={"mapping":{"parcel_id":"parcel_id","recorded_area":"recorded_area"},"confirm":True}))
    return ds,features[0]


def test_production_offline_reload_scope_reconnect_and_expiry(stack,browser):
    api,origin,workspace = stack
    project = checked(api.post("/api/projects",json={"name":"Production offline acceptance"}))["id"]; prefix=f"/api/projects/{project}"
    field_user = checked(api.post("/api/auth/token",json={"username":"field","password":"field"}))["user"]
    for username,role in [("field","field"),("processor","viewer")]: checked(api.post(prefix+"/members",json={"username":username,"project_role":role}))
    body=json.dumps({"type":"FeatureCollection","features":[{"type":"Feature","properties":{"parcel_id":"OFFLINE-1","recorded_area":100},"geometry":GEOMETRY}]}).encode()
    _,feature=upload(api,prefix,"offline.geojson",body)
    checked(api.post(prefix+f"/features/{feature['id']}/baseline",json={"geometry_source_id":feature['id'],"attribute_source_id":feature['id'],"rationale":"Synthetic acceptance baseline"}))
    parcel=checked(api.get(prefix+"/parcels"))[0]["id"]
    assignment=checked(api.post(prefix+"/field-assignments",json={"assignee_id":field_user["id"],"parcel_entity_ids":[parcel]}),201)
    context=browser.new_context(viewport={"width":1440,"height":1000},permissions=["geolocation"],geolocation={"latitude":20.0005,"longitude":73.0005,"accuracy":3})
    page=context.new_page(); errors=[]; page.on("pageerror",lambda e:errors.append(str(e)))
    try:
        login(page,origin,"field",project)
        page.get_by_role("button",name="Fieldwork / citizen",exact=True).click()
        page.get_by_label("Assigned parcel").select_option(parcel)
        page.evaluate("async () => { await navigator.serviceWorker.ready }"); page.wait_for_function("navigator.serviceWorker.controller !== null")
        page.get_by_label("Field note",exact=True).fill("Persisted synthetic evidence through offline reload")
        context.set_offline(True); page.get_by_role("button",name="Save offline draft").click()
        expect(page.locator("body")).to_contain_text("queued drafts: 1")
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading",name="Authorized fieldwork")).to_be_visible()
        expect(page.locator("body")).to_contain_text("queued drafts: 1")
        page.get_by_label("Assigned parcel").select_option(parcel)
        page.screenshot(path=str(workspace/"offline-restored.png"),full_page=True)
        # Change revision while device is offline; conflict must remain reviewable.
        upload(api,prefix,"revision.csv",b"parcel_id,recorded_area\nREVISION,0\n","text/csv")
        context.set_offline(False)
        expect(page.get_by_role("button",name="Retry queued sync")).to_be_enabled(timeout=15000)
        page.get_by_role("button",name="Retry queued sync").click()
        expect(page.locator("body")).to_contain_text("Revision conflict",timeout=15000)
        page.get_by_label("Revision resubmission rationale").fill("Reviewed revised project and retained observation")
        page.get_by_role("button",name="Resolve revision conflict").click()
        expect(page.locator("body")).to_contain_text("queued drafts: 0")
        # Revocation cannot be bypassed by falling back to old cached assignments.
        page.get_by_label("Assigned parcel").select_option(parcel); page.get_by_label("Field note",exact=True).fill("Retain after revocation")
        context.set_offline(True)
        page.evaluate("() => {window.originalSetItem=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k.startsWith('geosyncai-field-queue-'))throw new DOMException('Quota exceeded','QuotaExceededError');return window.originalSetItem.call(this,k,v)}}")
        page.get_by_role("button",name="Save offline draft").click()
        expect(page.locator("body")).to_contain_text("Device storage is unavailable or full")
        expect(page.get_by_label("Field note",exact=True)).to_have_value("Retain after revocation")
        page.evaluate("() => {Storage.prototype.setItem=window.originalSetItem}")
        page.get_by_role("button",name="Save offline draft").click(); expect(page.locator("body")).to_contain_text("queued drafts: 1")
        checked(api.patch(prefix+f"/field-assignments/{assignment['id']}",json={"status":"revoked","expected_revision":assignment["revision"]}))
        context.set_offline(False); page.get_by_role("button",name="Retry queued sync").click()
        expect(page.locator("body")).to_contain_text("No current authorized assignment",timeout=15000); expect(page.locator("body")).to_contain_text("queued drafts: 1")
        # Sign-out locks but does not silently destroy queued evidence.
        page.get_by_role("button",name="Sign out",exact=True).click(); login(page,origin,"processor",project)
        page.get_by_role("button",name="Datasets",exact=True).click(); expect(page.get_by_role("button",name="Upload dataset",exact=True)).to_be_disabled()
        expect(page.locator("body")).not_to_contain_text("Retain after revocation")
        retained=page.evaluate("Object.entries(localStorage).filter(([k])=>k.startsWith('geosyncai-field-queue-')).map(([,v])=>JSON.parse(v).length)")
        assert retained == [1]
        page.get_by_role("button",name="Sign out",exact=True).click(); login(page,origin,"field",project)
        # Expired cached authorization cannot restore an offline workspace.
        page.evaluate("() => {const key='geosyncai-session-v1';const s=JSON.parse(localStorage.getItem(key));s.expiresAt=Date.now()-1;localStorage.setItem(key,JSON.stringify(s))}")
        context.set_offline(True); page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("button",name="Sign in to workspace")).to_be_visible(); assert retained==[1]
        assert not errors,errors
    finally:
        if page.is_closed() is False: page.screenshot(path=str(workspace/"offline-final.png"),full_page=True)
        context.close()


def test_production_reconciliation_true_split_merge_vertex_and_alignment(stack,browser):
    api,origin,workspace=stack
    project=checked(api.post("/api/projects",json={"name":"Production reviewed geometry acceptance"}))["id"];prefix=f"/api/projects/{project}"
    datasets=[];features=[]
    for i in range(2):
        body=json.dumps({"type":"FeatureCollection","features":[{"type":"Feature","properties":{"parcel_id":"L-001","recorded_area":100+i*25},"geometry":GEOMETRY}]}).encode()
        ds,feature=upload(api,prefix,f"survey-{i+1}.geojson",body);datasets.append(ds);features.append(feature)
    ds,feature=upload(api,prefix,"revenue.csv",b"parcel_id,recorded_area\nL-001,150\n","text/csv");datasets.append(ds);features.append(feature)
    context=browser.new_context(viewport={"width":1440,"height":1000});page=context.new_page();errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
    try:
        login(page,origin,"admin",project);page.get_by_role("button",name="Datasets",exact=True).click()
        tools=page.locator("section").filter(has=page.get_by_role("heading",name="Source harmonization tools",exact=True)).first
        for checkbox in tools.locator(".record-preview input[type=checkbox]").all(): checkbox.check()
        page.get_by_label("Comparison rationale").fill("Compare independent survey and revenue evidence")
        page.get_by_role("button",name="Compare 3 independent sources").click()
        page.get_by_label("Explicitly approve a publication baseline with this identity decision").check()
        page.get_by_label("Reconciliation boundary source").select_option(features[0]["id"])
        page.get_by_label("Reconciliation default attribute source").select_option(features[2]["id"])
        case=checked(api.get(prefix+"/reconciliations"))[0]
        page.get_by_label(f"Case {case['id']} field recorded_area",exact=True).select_option(features[1]["id"])
        page.get_by_label("Reconciliation rationale",exact=True).fill("Verified identity; survey boundary and explicit area precedence")
        page.get_by_role("button",name="Accept reconciliation").click()
        expect(page.locator("body")).to_contain_text("explicit baseline: true")
        parcel=checked(api.get(prefix+"/parcels"))[0]; assert parcel["selection"]["attribute_sources"]["recorded_area"]==features[1]["id"]
        page.get_by_role("button",name="Boundary editor",exact=True).click()
        page.get_by_label("Operation").select_option("split")
        page.locator(".record-preview input[type=checkbox]").first.check()
        page.get_by_role("button",name="Draw cut on map").click()
        expect(page.locator('[data-editor-ready="true"]')).to_be_visible()
        canvas=page.get_by_role("region",name="Interactive boundary editor");box=canvas.bounding_box()
        canvas.click(position={"x":box["width"]/2-20,"y":15});canvas.click(position={"x":box["width"]/2-20,"y":box["height"]-15})
        page.get_by_role("button",name="Create draft geometry").click()
        expect(page.get_by_role("region",name="Measured geometry preview")).to_be_visible()
        page.get_by_label("Successor attribute source").select_option(features[2]["id"])
        page.get_by_label("Submission rationale",exact=True).fill("True cut of concave approved boundary")
        page.get_by_role("button",name="Submit geometry draft").click()
        page.get_by_label("Geometry review rationale").fill("Reviewed conserved area and retained concavity")
        page.get_by_role("button",name="Approve",exact=True).click()
        expect(page.locator("body")).to_contain_text("split · approved")
        children=checked(api.get(prefix+"/parcels"));assert len(children)==2
        assert union_all([shape(p["geometry"]) for p in children]).symmetric_difference(shape(GEOMETRY)).area<1e-12
        page.get_by_label("Operation").select_option("merge")
        for checkbox in page.locator(".record-preview input[type=checkbox]").all():checkbox.check()
        page.get_by_role("button",name="Create draft geometry").click();page.get_by_label("Successor attribute source").select_option(features[2]["id"])
        page.get_by_label("Submission rationale",exact=True).fill("Union reviewed children preserving original boundaries");page.get_by_role("button",name="Submit geometry draft").click()
        page.get_by_label("Geometry review rationale").fill("Measured union matches parent coverage");page.get_by_role("button",name="Approve",exact=True).click()
        expect(page.locator("body")).to_contain_text("merge · approved")
        merged=checked(api.get(prefix+"/parcels"));assert len(merged)==1;assert shape(merged[0]["geometry"]).equals(shape(GEOMETRY))
        page.get_by_label("Operation").select_option("move");page.locator(".record-preview input[type=checkbox]").first.check();page.get_by_role("button",name="Create draft geometry").click()
        handle=page.locator(".vertex-handle").first
        expect(handle).to_be_visible()
        initial=page.get_by_label("Draft GeoJSON").input_value();position=handle.bounding_box()
        page.mouse.move(position["x"]+position["width"]/2,position["y"]+position["height"]/2);page.mouse.down();page.mouse.move(position["x"]+position["width"]/2+6,position["y"]+position["height"]/2+3,steps=5);page.mouse.up()
        expect(page.get_by_label("Draft GeoJSON")).not_to_have_value(initial)
        page.get_by_role("button",name="Undo draft edit",exact=True).click()
        expect(page.get_by_label("Draft GeoJSON")).to_have_value(initial)
        page.get_by_text("Accessible vertex coordinates and snapping",exact=True).click()
        page.get_by_label("Vertex",exact=True).select_option("1");point=page.get_by_label("Vertex longitude").input_value();page.get_by_label("Vertex longitude").fill(str(float(point)+.000005))
        page.get_by_role("button",name="Apply vertex with snapping").click();page.get_by_role("button",name="Validate current preview").click()
        page.get_by_label("Submission rationale",exact=True).fill("Synthetic vertex correction using map handles and snapping");page.get_by_role("button",name="Submit geometry draft").click()
        page.get_by_label("Geometry review rationale").fill("Defer pending field verification");page.get_by_role("button",name="Defer",exact=True).click()
        expect(page.locator("body")).to_contain_text("move · deferred")
        page.get_by_label("Geometry review rationale").fill("Field evidence reviewed; approve corrected vertex");page.get_by_role("button",name="Approve",exact=True).click()
        expect(page.locator("body")).to_contain_text("move · approved")
        updated=checked(api.get(prefix+"/parcels"))[0];assert not shape(updated["geometry"]).equals(shape(GEOMETRY))
        page.screenshot(path=str(workspace/"reviewed-geometry.png"),full_page=True)
        # Alignment controls/coverage in UI; approval must refresh the registry.
        page.get_by_role("button",name="Datasets",exact=True).click();page.get_by_label("Source dataset",exact=True).select_option(datasets[0]["id"])
        page.get_by_role("button",name="Fit controls",exact=True).click()
        expect(page.locator("body")).to_contain_text("checkpoints 1")
        page.get_by_text("similarity · draft · checkpoints 1",exact=True).click()
        page.get_by_label("Alignment approval rationale").fill("Independent checkpoint and no extrapolation verified")
        page.get_by_role("button",name="Approve alignment",exact=True).click()
        expect(page.locator("body")).to_contain_text("similarity · approved")
        aligned=[d for d in checked(api.get(prefix+"/datasets")) if d["id"] not in [d["id"] for d in datasets]];assert len(aligned)==1
        assert aligned[0]["schema_mapping_version"]==1
        page.get_by_role("button",name="Read-only queries",exact=True).click();page.get_by_label("Query",exact=True).fill("show parcels with area above 110");page.get_by_role("button",name="Run bounded query").click()
        expect(page.locator("body")).to_contain_text("area_threshold")
        assert not errors,errors
    finally:
        page.screenshot(path=str(workspace/"geometry-final.png"),full_page=True);context.close()
