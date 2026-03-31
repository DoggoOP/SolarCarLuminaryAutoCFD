#!/usr/bin/env python3
"""
Download Luminary Cloud CFD surface/volume data for every simulation.

This version uses the official `luminarycloud` SDK so it works wherever
the SDK works (no direct REST hostname assumptions).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from typing import Dict, Iterable, Optional

import luminarycloud as lc


def load_env(path: pathlib.Path) -> Dict[str, str]:
    """Parse KEY=VALUE pairs from .env (simple implementation)."""
    env: Dict[str, str] = {}
    if path.is_dir():
        path = path / ".env"
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def ensure_token(env_path: pathlib.Path) -> str:
    env = load_env(env_path)
    for key, value in env.items():
        os.environ.setdefault(key, value)
    token = os.environ.get("LUMINARY_API_KEY")
    if not token:
        raise SystemExit(
            "Missing LUMINARY_API_KEY. Set it in the environment or supply a .env file."
        )
    return token


def slugify(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return safe or "unnamed"


def iter_projects(client: lc.Client, only: Optional[str]) -> Iterable[lc.Project]:
    if only:
        norm = only.lower()
        for project in lc.iterate_projects():
            if project.name.lower() == norm:
                yield project
                return
        raise SystemExit(f"Project '{only}' not found.")
    yield from lc.iterate_projects()


def extract_tar(dest_dir: pathlib.Path, fetch_tar, overwrite: bool, label: str) -> None:
    if dest_dir.exists():
        if not overwrite:
            print(f"      Skipping {label} (exists): {dest_dir}")
            return
    dest_dir.mkdir(parents=True, exist_ok=True)
    with fetch_tar() as tar:
        tar.extractall(dest_dir)
    print(f"      Saved {label} -> {dest_dir}")


def download_solution_data(
    solution: lc.Solution,
    dest_root: pathlib.Path,
    project: lc.Project,
    simulation: lc.Simulation,
    overwrite: bool,
    kinds: Iterable[str],
) -> None:
    solution_label = f"solution_{solution.iteration}" if solution.iteration is not None else solution.id
    base_dir = (
        dest_root
        / slugify(project.name or project.id)
        / slugify(simulation.name or simulation.id)
        / str(solution_label)
    )
    print(f"    Solution {solution_label}: downloading {', '.join(kinds)}")

    for kind in kinds:
        dest_dir = base_dir / kind
        try:
            if kind == "surface":
                extract_tar(dest_dir, solution.download_surface_data, overwrite, "surface data")
            elif kind == "volume":
                extract_tar(dest_dir, solution.download_volume_data, overwrite, "volume data")
            else:
                print(f"      Unknown export type '{kind}' (skipped)")
        except Exception as exc:
            print(f"      ! Failed to download {kind}: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Luminary Cloud CFD data via SDK.")
    parser.add_argument("--env-path", default=".env",
                        help="Path to .env file or folder containing it.")
    parser.add_argument("--dest", required=True,
                        help="Destination directory (e.g., external drive).")
    parser.add_argument("--project", help="Only download from this project name.")
    parser.add_argument("--kinds", nargs="+", choices=["surface", "volume"],
                        default=["surface", "volume"],
                        help="Which solution datasets to download.")
    parser.add_argument("--latest-only", action="store_true",
                        help="Only download the newest solution per simulation.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-download even if destination exists.")
    args = parser.parse_args()

    dest_root = pathlib.Path(args.dest).expanduser()
    dest_root.mkdir(parents=True, exist_ok=True)

    token = ensure_token(pathlib.Path(args.env_path))
    client = lc.Client(api_key=token)
    lc.set_default_client(client)

    total_projects = 0
    total_sims = 0
    total_solutions = 0

    for project in iter_projects(client, args.project):
        total_projects += 1
        print(f"[PROJECT] {project.name} ({project.id})")
        simulations = list(project.list_simulations())
        if not simulations:
            print("  No simulations.")
            continue

        for simulation in simulations:
            total_sims += 1
            print(f"  [SIM] {simulation.name} ({simulation.id}) — status {simulation.status.name}")
            solutions = simulation.list_solutions()
            if not solutions:
                print("    (no solutions yet)")
                continue

            selected = [solutions[-1]] if args.latest_only else solutions
            for solution in selected:
                total_solutions += 1
                download_solution_data(
                    solution,
                    dest_root=dest_root,
                    project=project,
                    simulation=simulation,
                    overwrite=args.overwrite,
                    kinds=args.kinds,
                )

    print("\nDone.")
    print(f"Projects: {total_projects}, simulations: {total_sims}, solutions downloaded: {total_solutions}")


if __name__ == "__main__":
    main()
