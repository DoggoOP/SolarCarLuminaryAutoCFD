#!/usr/bin/env python3
"""
Automate downloads of Luminary Cloud CFD surface/volume exports.

The script:
  1. Loads the API token from environment/.env (expects LUMINARY_API_KEY).
  2. Enumerates all projects and runs.
  3. Requests surface + volume export jobs for each run (configurable).
  4. Polls until the export succeeds, then downloads the artifact
     to a destination directory (e.g., mounted external drive).

Usage example:
  python scripts/download_luminary_data.py \
      --dest /Volumes/LuminaryCFD \
      --surface-fields pressure shear \
      --volume-fields velocity temperature
"""

import argparse
import os
import sys
import time
import json
import pathlib
import re
import urllib.parse
from typing import Dict, Iterable, List, Optional

import requests


DEFAULT_BASE_URL = "https://api.luminarycloud.com"
API_TIMEOUT = 60
POLL_INTERVAL = 20
PAGE_SIZE = 100
EXPORT_TYPES = ("cfd_surface", "cfd_volume")


def load_env_file(path: pathlib.Path) -> Dict[str, str]:
    """Minimal .env parser (KEY=VALUE, ignoring comments)."""
    env: Dict[str, str] = {}
    if path.is_dir():
        candidate = path / ".env"
    else:
        candidate = path

    if not candidate.exists():
        return env

    for raw in candidate.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def slugify(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return safe or "unnamed"


class LuminaryClient:
    def __init__(self, token: str, base_url: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "luminary-downloader/1.0",
        })
        self.base_url = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{path}"

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self.session.get(self._url(path), params=params,
                                timeout=API_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(
            self._url(path),
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}

    def paginate(self, path: str, params: Optional[dict] = None) -> Iterable[dict]:
        next_url: Optional[str] = self._url(path)
        query = params.copy() if params else None
        while next_url:
            resp = self.session.get(next_url, params=query,
                                    timeout=API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or data.get("items")
            if items is None:
                raise RuntimeError(f"Unexpected pagination payload: {data}")
            for item in items:
                yield item
            next_url = (
                data.get("next")
                or data.get("next_page")
                or data.get("next_url")
                or (data.get("links") or {}).get("next")
            )
            if next_url and next_url.startswith("/"):
                next_url = f"{self.base_url}{next_url}"
            query = None  # only include params on first request

    def iter_projects(self) -> Iterable[dict]:
        return self.paginate("/v1/projects", params={"page_size": PAGE_SIZE})

    def iter_runs(self, project_id: str) -> Iterable[dict]:
        return self.paginate(
            f"/v1/projects/{project_id}/runs",
            params={"page_size": PAGE_SIZE},
        )

    def request_export(self, run_id: str, payload: dict) -> dict:
        return self.post(f"/v1/runs/{run_id}/exports", payload)

    def get_export(self, export_id: str) -> dict:
        return self.get(f"/v1/exports/{export_id}")


def ensure_token(env_path: pathlib.Path) -> str:
    env = load_env_file(env_path)
    for key, value in env.items():
        os.environ.setdefault(key, value)
    token = os.environ.get("LUMINARY_API_KEY")
    if not token:
        raise SystemExit(
            "Missing LUMINARY_API_KEY. Set it in the environment or .env file."
        )
    return token


def request_and_download(
    client: LuminaryClient,
    run: dict,
    project: dict,
    dest: pathlib.Path,
    export_name: str,
    payload: dict,
    overwrite: bool,
) -> None:
    run_id = run["id"]
    summary = f"{project.get('name') or project.get('id')} / {run.get('name') or run_id}"
    print(f"[+] Requesting {export_name} export for {summary}")
    export = client.request_export(run_id, payload)
    export_id = export.get("id") or export.get("export_id")
    if not export_id:
        raise RuntimeError(f"No export id returned for run {run_id}: {export}")

    url = wait_for_export(client, export_id, summary)
    save_export(url, dest, project, run, export_name, overwrite)


def wait_for_export(client: LuminaryClient, export_id: str, summary: str) -> str:
    while True:
        status = client.get_export(export_id)
        state = status.get("status") or status.get("state")
        if state == "succeeded":
            url = status.get("download_url") or status.get("url")
            if not url:
                raise RuntimeError(f"Export {export_id} succeeded without download url")
            print(f"    Export ready for {summary}")
            return url
        if state == "failed":
            raise RuntimeError(f"Export {export_id} failed: {status}")
        print(f"    Waiting for export {export_id} ({state})...")
        time.sleep(POLL_INTERVAL)


def save_export(
    url: str,
    dest_root: pathlib.Path,
    project: dict,
    run: dict,
    export_name: str,
    overwrite: bool,
) -> None:
    parsed = urllib.parse.urlparse(url)
    filename = pathlib.Path(parsed.path).name or f"{export_name}.bin"
    project_dir = slugify(project.get("name") or project.get("id", "project"))
    run_dir = slugify(run.get("name") or run.get("id", "run"))
    dest_dir = dest_root / project_dir / run_dir / export_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    if dest_path.exists() and not overwrite:
        print(f"    Skipping download (exists): {dest_path}")
        return

    print(f"    Downloading to {dest_path}")
    with requests.get(url, stream=True, timeout=API_TIMEOUT) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)
    print(f"    Saved {dest_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Luminary Cloud CFD exports.")
    parser.add_argument("--env-path", default=".env",
                        help="Path to .env file or directory containing it.")
    parser.add_argument("--dest", required=True,
                        help="Destination directory (e.g., mounted external drive).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="Luminary API base URL (default: %(default)s).")
    parser.add_argument("--surface-fields", nargs="+",
                        default=["pressure", "shear"],
                        help="Fields for surface exports.")
    parser.add_argument("--surface-format", default="vtk",
                        help="File format for surface exports (default: %(default)s).")
    parser.add_argument("--volume-fields", nargs="+",
                        default=["velocity", "temperature"],
                        help="Fields for volume exports.")
    parser.add_argument("--volume-format", default="vtk",
                        help="File format for volume exports (default: %(default)s).")
    parser.add_argument("--overwrite", action="store_true",
                        help="Redownload files even if they already exist.")
    args = parser.parse_args()

    dest_root = pathlib.Path(args.dest).expanduser()
    dest_root.mkdir(parents=True, exist_ok=True)

    token = ensure_token(pathlib.Path(args.env_path))
    client = LuminaryClient(token=token, base_url=args.base_url)

    projects = list(client.iter_projects())
    if not projects:
        print("No projects found.")
        return

    for project in projects:
        project_id = project["id"]
        print(f"[PROJECT] {project.get('name', project_id)} ({project_id})")
        for run in client.iter_runs(project_id):
            run_id = run["id"]
            print(f"  [RUN] {run.get('name', run_id)} ({run_id})")
            export_payloads: List[tuple[str, dict]] = [
                ("surface", {
                    "type": EXPORT_TYPES[0],
                    "fields": args.surface_fields,
                    "format": args.surface_format,
                }),
                ("volume", {
                    "type": EXPORT_TYPES[1],
                    "fields": args.volume_fields,
                    "format": args.volume_format,
                }),
            ]

            for export_name, payload in export_payloads:
                try:
                    request_and_download(
                        client, run, project, dest_root, export_name, payload, args.overwrite
                    )
                except Exception as exc:
                    print(f"    ! Failed {export_name} export for run {run_id}: {exc}",
                          file=sys.stderr)


if __name__ == "__main__":
    main()
