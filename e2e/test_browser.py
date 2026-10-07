"""Real browser + real HTTP API, isolated data and free loopback ports.

Run: python -m pytest e2e -q (install playwright and its Chromium first).
Only processes created by this test are terminated.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f'Service exited before readiness: {url}')
        try:
            with urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(.2)
    raise RuntimeError(f'Readiness timed out: {url}')


def test_browser_officer_workflow(tmp_path):
    api_port, web_port = free_port(), free_port()
    env = {**os.environ, 'DATABASE_URL': os.environ.get('E2E_DATABASE_URL', f'sqlite:///{tmp_path / "browser.db"}'),
           'STORAGE_DIR': str(tmp_path / 'storage'), 'AUTO_BOOTSTRAP': 'true',
           'JWT_SECRET': 'isolated-browser-test-secret-at-least-32', 'CELERY_BROKER_URL': '',
           'VITE_API_BASE_URL': f'http://127.0.0.1:{api_port}/api'}
    processes = []
    with (tmp_path / 'api.log').open('w') as api_log, (tmp_path / 'web.log').open('w') as web_log:
        try:
            backend = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(api_port)],
                                       cwd=ROOT / 'backend', env=env, stdout=api_log, stderr=subprocess.STDOUT)
            processes.append(backend)
            wait_for(f'http://127.0.0.1:{api_port}/health', backend)
            frontend = subprocess.Popen(['node', 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', str(web_port)],
                                        cwd=ROOT / 'frontend', env=env, stdout=web_log, stderr=subprocess.STDOUT)
            processes.append(frontend)
            wait_for(f'http://127.0.0.1:{web_port}', frontend)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                # Decode actual raster tiles in MapLibre without depending on an
                # external provider's network availability in CI.
                tile = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGN48f7lfwAJQwPAmGARvgAAAABJRU5ErkJggg==')
                tile_requests = []
                tile_state = {'fail': False}
                def serve_tile(route):
                    tile_requests.append(route.request.url)
                    if tile_state['fail']:
                        route.abort()
                    else:
                        route.fulfill(status=200, content_type='image/png', body=tile)
                page.route('https://basemaps.cartocdn.com/**', serve_tile)
                page.goto(f'http://127.0.0.1:{web_port}', wait_until='domcontentloaded')
                page.get_by_label('Username', exact=True).fill('admin')
                page.get_by_label('Password', exact=True).fill('admin')
                page.get_by_role('button', name='Sign in to workspace').click()
                page.get_by_label('Project name').fill('Browser acceptance project')
                page.get_by_role('button', name='Create project', exact=True).click()
                expect(page.get_by_label('Project', exact=True)).to_have_value(__import__('re').compile('.+'))
                page.get_by_role('button', name='Datasets', exact=True).click()
                geometry = {'type': 'Polygon', 'coordinates': [[[73,20],[73.001,20],[73.001,20.001],[73,20.001],[73,20]]]}
                body = json.dumps({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'id': '0001',
                    'properties': {'parcel_id': '0001', 'village_code': 'TEST'}, 'geometry': geometry}]}).encode()
                for name, date in [('baseline.geojson', '2024-01-01'), ('comparison.geojson', '2025-01-01')]:
                    page.get_by_label('Dataset file').set_input_files({'name': name, 'mimeType': 'application/geo+json', 'buffer': body})
                    page.get_by_label('Capture date', exact=True).fill(date)
                    page.get_by_label('Source organization').fill('Synthetic browser acceptance')
                    page.get_by_role('button', name='Upload dataset', exact=True).click()
                    expect(page.get_by_role('heading', name=name, exact=True)).to_be_visible()
                    dataset_section = page.locator('section').filter(has=page.get_by_role('heading', name=name, exact=True))
                    dataset_section.get_by_label('Source CRS', exact=True).fill('EPSG:4326')
                    dataset_section.get_by_label('CRS rationale').fill('Test fixture documented longitude/latitude')
                    dataset_section.get_by_role('button', name='Confirm CRS and reprocess').click()
                    expect(page.locator('main.page-content')).to_have_attribute('aria-busy', 'false')
                    dataset_section.get_by_label('Map parcel_id').select_option('parcel_id')
                    dataset_section.get_by_label('Map village_code').select_option('village_code')
                    dataset_section.get_by_role('button', name='Confirm mapping', exact=True).click()
                    expect(page.get_by_role('heading', name='Schema mapping · confirmed v1')).to_be_visible()
                page.get_by_label('Baseline', exact=True).select_option(label='baseline.geojson · 2024-01-01')
                page.get_by_label('Comparison', exact=True).select_option(label='comparison.geojson · 2025-01-01')
                page.get_by_role('button', name='Generate matches').click()
                expect(page.get_by_text('Processing completed', exact=True)).to_be_visible(timeout=30000)
                page.get_by_role('button', name='Review queue', exact=True).click()
                expect(page.get_by_label('Show basemap')).to_be_checked()
                expect(page.get_by_text('Basemap loaded', exact=True)).to_be_visible()
                assert tile_requests, 'Basemap must request tiles on first opening'
                expect(page.locator('.live-map canvas')).to_have_count(1)
                page.get_by_label('Show basemap').uncheck()
                expect(page.get_by_text('Basemap off · parcel overlays available', exact=True)).to_be_visible()
                tile_state['fail'] = True
                page.get_by_role('button', name='Datasets', exact=True).click()
                page.get_by_role('button', name='Review queue', exact=True).click()
                expect(page.get_by_text('Basemap tiles could not load.', exact=False)).to_be_visible()
                expect(page.locator('.live-map')).to_have_attribute('data-map-ready', 'true')
                tile_state['fail'] = False
                page.get_by_role('button', name='Retry map', exact=True).click()
                expect(page.get_by_text('Basemap loaded', exact=True)).to_be_visible()
                expect(page.locator('.live-map canvas')).to_have_count(1)
                # A rendered parcel must still be clickable after tile failure
                # and map recreation; checking only a canvas misses blank maps.
                canvas = page.locator('.live-map canvas')
                bounds = canvas.bounding_box()
                assert bounds
                canvas.click(position={'x': bounds['width'] / 2, 'y': bounds['height'] / 2})
                expect(page.get_by_role('heading', name='Parcel evidence card')).to_be_visible()
                page.get_by_label('Review rationale').fill('Verified identity only; baseline selected separately')
                page.get_by_role('button', name='accepted', exact=True).click()
                expect(page.get_by_text('Review decision saved', exact=True)).to_be_visible()
                page.get_by_label('Geometry source', exact=True).select_option(index=1)
                page.get_by_label('Attribute source', exact=True).select_option(index=1)
                page.get_by_label('Selection rationale').fill('Explicit reviewed baseline source')
                page.get_by_role('button', name='Approve baseline selection').click()
                expect(page.locator('main.page-content')).to_have_attribute('aria-busy', 'false')
                page.get_by_role('button', name='Versions & history', exact=True).click()
                page.get_by_role('button', name='Run validation', exact=True).click()
                expect(page.get_by_role('heading', name='Validation: passed')).to_be_visible()
                page.get_by_role('button', name='Publish version', exact=True).click()
                expect(page.get_by_role('heading', name='Version 1', exact=True)).to_be_visible()
                with page.expect_download() as receipt:
                    page.get_by_label('Export', exact=True).select_option('geojson')
                download = receipt.value
                document = json.loads(Path(download.path()).read_text())
                assert len(document['features']) == 1
                assert len(document['features'][0]['properties']['_lineage']['sources']) == 2
                # Restart the service without reseeding/replacing its database;
                # authentication and publication must still be retrievable.
                backend.terminate(); backend.wait(timeout=10)
                backend = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(api_port)],
                                           cwd=ROOT / 'backend', env=env, stdout=api_log, stderr=subprocess.STDOUT)
                processes.append(backend)
                wait_for(f'http://127.0.0.1:{api_port}/health', backend)
                page.get_by_role('button', name='Refresh', exact=True).click()
                expect(page.get_by_role('heading', name='Version 1', exact=True)).to_be_visible()
                assert not errors, errors
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                browser.close()
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
