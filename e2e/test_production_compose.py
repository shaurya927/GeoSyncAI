"""Production-built frontend smoke against an already-running Compose stack.

Run with a disposable stack and a seeded demo project:
  BASE_URL=http://127.0.0.1:5173 pytest e2e/test_production_compose.py -q
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import httpx

from playwright.sync_api import sync_playwright, expect


def test_production_nginx_role_scoped_workspace():
    base_url = os.environ.get("BASE_URL", "http://127.0.0.1:5173")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage",
                                                   "--use-gl=angle", "--use-angle=swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(base_url, wait_until="networkidle")
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['x-frame-options'] == 'DENY'
        assert "script-src 'self'" in response.headers['content-security-policy']
        page.get_by_label("Username", exact=True).fill("admin")
        page.get_by_label("Password", exact=True).fill("admin")
        page.get_by_role("button", name="Sign in to workspace").click()
        expect(page.get_by_role("button", name="Datasets", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Boundary editor", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Read-only queries", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Compliance screening", exact=True)).to_be_visible()
        page.get_by_role("button", name="Boundary editor", exact=True).click()
        expect(page.get_by_role("heading", name="Boundary editor", exact=True, level=1)).to_be_visible()
        page.get_by_role("button", name="Read-only queries", exact=True).click()
        expect(page.get_by_role("heading", name="Read-only query workspace", exact=True)).to_be_visible()
        page.get_by_role("button", name="Compliance screening", exact=True).click()
        expect(page.get_by_role("heading", name="Compliance screening", exact=True, level=1)).to_be_visible()
        page.get_by_role('button', name='Account & security', exact=True).click()
        expect(page.get_by_role('heading', name='Traffic protection', exact=True)).to_be_visible()
        expect(page.get_by_text('Counter backend: redis', exact=False)).to_be_visible()
        token = page.evaluate("sessionStorage.getItem('geosyncai_token')")
        assert token and page.evaluate("localStorage.getItem('geosyncai_token')") is None
        me = page.request.get(base_url + '/api/auth/me', headers={'Authorization': 'Bearer ' + token})
        assert me.status == 200 and me.headers['cache-control'] == 'no-store'
        page.get_by_role('button', name='Sign out', exact=True).click()
        expect(page.get_by_role('button', name='Sign in to workspace')).to_be_visible()
        assert page.request.get(base_url + '/api/auth/me', headers={'Authorization': 'Bearer ' + token}).status == 401
        assert page.request.get(base_url + '/api/health').status == 200
        assert page.request.get(base_url + '/api/ready').status == 200
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        print(json.dumps({"url": page.url, "errors": errors, "responsive": True}))
        browser.close()

    # Exercise only this disposable CI stack, after normal browser traffic.
    # Unauthenticated reads avoid creating accounts, jobs or source records.
    with httpx.Client(base_url=base_url, timeout=15) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: client.get('/api/auth/me'), range(100)))
    assert all(response.status_code in {401, 429, 503} for response in replies)
    throttled = [response for response in replies if response.status_code == 429]
    assert throttled, 'Nginx must reject traffic above its configured burst budget'
    assert all(int(response.headers['Retry-After']) > 0 for response in throttled)
