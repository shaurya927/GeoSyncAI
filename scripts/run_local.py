"""Run the real API and production web build on loopback, using isolated demo data.

python scripts/run_local.py --seed-demo
Use an activated Python 3.12 environment with backend/requirements.txt installed.
"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def available_port(value):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", value))
        except OSError as exc:
            raise SystemExit(f"Port {value} is occupied. Choose --api-port / --web-port alternatives; existing services are preserved.") from exc
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-demo", action="store_true", help="Explicitly create isolated demonstration accounts/project")
    parser.add_argument("--build", action="store_true", help="Rebuild frontend from editable source with Node/npm")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local-demo")
    args = parser.parse_args()
    data = args.data_dir.resolve(); data.mkdir(parents=True, exist_ok=True)
    secret_file = data / "jwt-secret.txt"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        if os.name != "nt": secret_file.chmod(0o600)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{(data/'geosyncai.db').as_posix()}",
           "STORAGE_DIR": str(data / "storage"), "JWT_SECRET": secret_file.read_text(encoding="utf-8").strip(),
           "AUTO_BOOTSTRAP": "false", "CELERY_BROKER_URL": "", "VITE_API_BASE_URL": "/api"}
    if args.build or not (ROOT / "frontend" / "dist" / "index.html").exists():
        npm = "npm.cmd" if os.name == "nt" else "npm"
        if not (ROOT / "frontend" / "node_modules").exists():
            subprocess.run([npm, "ci"], cwd=ROOT / "frontend", env=env, check=True)
        subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend", env=env, check=True)
    if args.seed_demo:
        subprocess.run([sys.executable, "-m", "app.seed_demo"], cwd=ROOT / "backend", env=env, check=True)
    api_port = available_port(args.api_port)
    web_port = available_port(args.web_port)
    if api_port == web_port: raise SystemExit("API and web ports must differ")
    api_url = f"http://127.0.0.1:{api_port}"
    server = None; process = None
    stop_token = secrets.token_urlsafe(32)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *params, **kwargs): super().__init__(*params, directory=str(ROOT / "frontend" / "dist"), **kwargs)
        def forward(self):
            headers = {key:value for key,value in self.headers.items() if key.lower() in {"authorization", "content-type"}}
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = Request(api_url + self.path, data=body if body else None, headers=headers, method=self.command)
            try:
                response = urlopen(request, timeout=60)
            except HTTPError as error:
                response = error
            except URLError:
                self.send_error(503, "Local API unavailable; see api.log"); return
            with response:
                output = response.read(); self.send_response(response.status)
                for key,value in response.headers.items():
                    if key.lower() not in {"content-length", "transfer-encoding", "connection", "cache-control"}: self.send_header(key, value)
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(output))); self.end_headers()
                try: self.wfile.write(output)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        def do_GET(self):
            if self.path.startswith("/api/"): self.forward()
            else: super().do_GET()
        def do_POST(self):
            if self.path == "/__stop" and secrets.compare_digest(self.headers.get("X-GeoSyncAI-Launcher", ""), stop_token):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"stopped":true}')
                threading.Thread(target=server.shutdown, daemon=True).start()
            elif self.path.startswith("/api/"): self.forward()
            else: self.send_error(404)
        def do_PATCH(self): self.forward()
        def do_DELETE(self): self.forward()
    with (data / "api.log").open("a", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port)], cwd=ROOT / "backend", env=env, stdout=log, stderr=subprocess.STDOUT)
            for _ in range(150):
                if process.poll() is not None: raise RuntimeError(f"API exited; inspect {data/'api.log'}")
                try:
                    with urlopen(api_url + "/health", timeout=1) as response:
                        if response.status == 200: break
                except URLError: time.sleep(.2)
            else: raise RuntimeError("API readiness timeout")
            server = ThreadingHTTPServer(("127.0.0.1", web_port), Handler)
            (data / "running.json").write_text(json.dumps({"launcher_pid":os.getpid(), "api_pid":process.pid, "api_url":api_url, "web_url":f"http://127.0.0.1:{web_port}", "stop_token":stop_token}, indent=2), encoding="utf-8")
            print(f"GeoSyncAI is running at http://127.0.0.1:{web_port}\nData: {data}\nStop with Ctrl+C. Original backend databases/uploads are preserved.", flush=True)
            monitor = threading.Thread(target=lambda: (process.wait(), server.shutdown()), daemon=True)
            monitor.start(); server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            print("Stopping the local GeoSyncAI processes.", flush=True)
        finally:
            if server: server.server_close()
            if process and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            (data / "running.json").unlink(missing_ok=True)


if __name__ == "__main__": main()
