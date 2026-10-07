"""Production-built frontend smoke against an already-running Compose stack.

Run with a disposable stack and a seeded demo project:
  BASE_URL=http://127.0.0.1:5173 pytest e2e/test_production_compose.py -q
"""
import json
import os

from playwright.sync_api import sync_playwright, expect


def test_production_nginx_role_scoped_workspace():
    base_url = os.environ.get("BASE_URL", "http://127.0.0.1:5173")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage",
                                                   "--use-gl=angle", "--use-angle=swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base_url, wait_until="networkidle")
        page.get_by_label("Username", exact=True).fill("admin")
        page.get_by_label("Password", exact=True).fill("admin")
        page.get_by_role("button", name="Sign in to workspace").click()
        expect(page.get_by_role("button", name="Datasets", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Boundary editor", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Read-only queries", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Compliance screening", exact=True)).to_be_visible()
        page.get_by_role("button", name="Boundary editor", exact=True).click()
        expect(page.get_by_role("heading", name="Boundary editor", exact=True)).to_be_visible()
        page.get_by_role("button", name="Read-only queries", exact=True).click()
        expect(page.get_by_role("heading", name="Read-only query workspace", exact=True)).to_be_visible()
        page.get_by_role("button", name="Compliance screening", exact=True).click()
        expect(page.get_by_role("heading", name="Compliance screening", exact=True)).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        print(json.dumps({"url": page.url, "errors": errors, "responsive": True}))
        browser.close()
