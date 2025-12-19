"""Backfill historical simulation results to Google Sheets."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import luminarycloud as lc

from .config import get_settings
from .luminary_pipeline import LuminaryCFDPipeline
from .sheets_logger import SheetsLogger


def backfill_results(
    project_name: Optional[str] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> None:
    """
    Fetch historical simulation results and log them to Google Sheets.

    Parameters
    ----------
    project_name : str, optional
        Name of the Luminary Cloud project. If None, uses default from settings.
    limit : int, optional
        Maximum number of simulations to backfill. If None, processes all.
    dry_run : bool
        If True, show what would be logged without actually logging.
    """
    settings = get_settings()

    # Initialize sheets logger
    sheets_logger = SheetsLogger.from_env(settings)
    if not sheets_logger:
        print("❌ Google Sheets is not configured.")
        print("Set GOOGLE_SHEETS_CREDENTIALS and GOOGLE_SHEETS_SPREADSHEET_ID in .env")
        sys.exit(1)

    # Connect to Luminary Cloud
    print("Connecting to Luminary Cloud...")
    client = lc.Client(api_key=settings.luminary_api_key)
    lc.set_default_client(client)

    # Find the project
    target_project_name = project_name or settings.luminary_project_name
    print(f"Looking for project: {target_project_name}")

    project = None
    for proj in lc.iterate_projects():
        if proj.name == target_project_name:
            project = proj
            break

    if not project:
        print(f"❌ Project '{target_project_name}' not found.")
        print("\nAvailable projects:")
        for proj in lc.iterate_projects():
            print(f"  - {proj.name}")
        sys.exit(1)

    print(f"✓ Found project: {project.name} (ID: {project.id})")

    # Get all simulations
    print("\nFetching simulations...")
    simulations = list(project.list_simulations())

    if not simulations:
        print("No simulations found in this project.")
        sys.exit(0)

    print(f"Found {len(simulations)} simulation(s)")

    # Filter to completed simulations
    completed_sims = [s for s in simulations if s.status.name == "COMPLETED"]
    print(f"  - {len(completed_sims)} completed")
    print(f"  - {len(simulations) - len(completed_sims)} incomplete/failed")

    if not completed_sims:
        print("\nNo completed simulations to backfill.")
        sys.exit(0)

    # Apply limit if specified
    sims_to_process = completed_sims[:limit] if limit else completed_sims

    if dry_run:
        print("\n🔍 DRY RUN - Would process these simulations:")
    else:
        print(f"\n📊 Processing {len(sims_to_process)} simulation(s)...")

    # Process each simulation
    success_count = 0
    error_count = 0

    for idx, simulation in enumerate(sims_to_process, 1):
        sim_name = simulation.name
        sim_id = simulation.id

        print(f"\n[{idx}/{len(sims_to_process)}] {sim_name}")
        print(f"  ID: {sim_id}")

        try:
            # Get reference values from simulation parameters
            ref_velocity = 24.59  # Default
            ref_area = 1.174  # Default

            try:
                # Get actual reference values from simulation
                sim_params = simulation.get_parameters()
                if hasattr(sim_params, 'reference_values'):
                    ref_values = sim_params.reference_values
                    if hasattr(ref_values, 'v_ref'):
                        ref_velocity = ref_values.v_ref
                    if hasattr(ref_values, 'area_ref'):
                        ref_area = ref_values.area_ref
                print(f"  Using: V={ref_velocity:.2f} m/s, A={ref_area:.4f} m²")
            except Exception:
                print(f"  Using defaults: V={ref_velocity:.2f} m/s, A={ref_area:.4f} m²")

            # Fetch force results
            force_results = LuminaryCFDPipeline._fetch_force_results(
                simulation,
                ref_area=ref_area,
                ref_velocity=ref_velocity,
                project=project,
            )

            if "error" in force_results:
                print(f"  ⚠️  Could not fetch forces: {force_results['error']}")
                error_count += 1
                continue

            # Display results
            drag = force_results.get("drag_force", "N/A")
            lift = force_results.get("lift_force", "N/A")
            side = force_results.get("sideforce", "N/A")
            cd = force_results.get("drag_coefficient", "N/A")

            print(f"  Results: Drag={drag:.3f}N (Cd={cd:.4f})")

            if not dry_run:
                # Log to sheets
                convergence_info = {
                    "status": simulation.status.name,
                    "iterations": 7500,  # Default max
                }

                sheets_logger.append_result(
                    job_name=sim_name,
                    simulation_id=sim_id,
                    force_results=force_results,
                    wind_speed=ref_velocity,
                    frontal_area=ref_area,
                    convergence_info=convergence_info,
                )
                print("  ✓ Logged to Google Sheets")

            success_count += 1

        except Exception as exc:
            print(f"  ❌ Error: {exc}")
            error_count += 1
            continue

    # Summary
    print("\n" + "=" * 60)
    if dry_run:
        print("DRY RUN COMPLETE")
        print(f"Would log {success_count} simulation(s)")
    else:
        print("BACKFILL COMPLETE")
        print(f"✓ Successfully logged: {success_count}")
    if error_count > 0:
        print(f"❌ Errors: {error_count}")
    print("=" * 60)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill historical CFD simulation results to Google Sheets"
    )
    parser.add_argument(
        "--project",
        type=str,
        help="Project name (default: from .env LUMINARY_PROJECT_NAME)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of simulations to process",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be logged without actually logging",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("CFD RESULTS BACKFILL TO GOOGLE SHEETS")
    print("=" * 60)

    backfill_results(
        project_name=args.project,
        limit=args.limit,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
