"""Stop only the launcher owning the specified local data directory."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / ".local-demo")
args = parser.parse_args()
record = args.data_dir.resolve() / "running.json"
if not record.exists(): raise SystemExit("No running GeoSyncAI launcher recorded for this data directory.")
state = json.loads(record.read_text(encoding="utf-8"))
url = state["web_url"]
parsed = urlsplit(url)
if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path:
    raise SystemExit("Invalid launcher address; no process was touched.")
request = Request(url + "/__stop", data=b"", headers={"X-GeoSyncAI-Launcher":state["stop_token"]}, method="POST")
with urlopen(request, timeout=10) as response:
    if response.status == 200: print("Stopped this GeoSyncAI launcher. Its database and uploads are retained.")
