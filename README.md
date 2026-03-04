# AutoCFD — Solar Car CFD + Solar Array Pipeline

AutoCFD automates the full Luminary Cloud CFD workflow for solar-car geometries and integrates a custom solar array simulation (Shellpower CLI). Upload a CAD file, run aerodynamic and solar simulations concurrently, and log drag/lift/solar results directly to Google Sheets.

---

## Contents

1. [Features](#features)
2. [Architecture Overview](#architecture-overview)
3. [Quick Start](#quick-start)
4. [Building Shellpower CLI](#building-shellpower-cli)
5. [Configuration](#configuration)
6. [Shellpower Solar Integration](#shellpower-solar-integration)
7. [Google Sheets Integration](#google-sheets-integration)
8. [Backfilling Historical Runs](#backfilling-historical-runs)
9. [Deployment](#deployment)
10. [API Reference](#api-reference)
11. [Troubleshooting](#troubleshooting)
12. [Project Structure](#project-structure)

---

## Features

- **FastAPI dashboard** for uploads, job monitoring, and cancellation.
- **Luminary Cloud automation**: geometry import, farfield creation, meshing, simulation setup, and monitoring.
- **Smart meshing** with adaptive refinement, automatic farfield sizing, and optional rotating wheels (including auto-detected surfaces and motion frames).
- **Automatic post-processing**: drag, viscous drag, pressure drag, side force, lift, center of pressure, CdA/CdW, and convergence metadata.
- **Shellpower solar array simulation**: automatically places Maxeon Gen 7 solar cells on the car surface, optimizes layout for sun exposure across WSC racing hours, and reports peak power and daily energy.
- **Google Sheets logging** with structured headers, solar array results, array map images, and hyperlinks to Luminary simulations.
- **Historical backfill script** to import previous Luminary runs.
- **Ready-to-deploy container** with templates for Railway, Render, and Cloud Run.

---

## Architecture Overview

| Component | Purpose |
|-----------|---------|
| `app/main.py` | FastAPI routes, upload handling, background execution, job dashboard. |
| `app/luminary_pipeline.py` | Core orchestration: geometry processing, meshing, simulation, force extraction, Shellpower invocation. |
| `app/config.py` | Settings loaded from environment variables. |
| `app/job_store.py` | In-memory tracking of job metadata, logs, and cancellation. |
| `app/sheets_logger.py` | Authentication and logging to Google Sheets, including header management and Drive image upload. |
| `app/backfill_sheets.py` | CLI to import finished Luminary simulations into Sheets. |
| `src/` | Shellpower C source — headless solar array layout and simulation engine. |
| `src/cli_main.c` | CLI entry point; reads OBJ mesh, places cells, wires, simulates, writes JSON. |
| `src/core/app_core.c/h` | Core state machine: mesh loading, cell placement, string wiring, simulation. |
| `src/core/auto_layout_core.c` | Grid-based auto-layout with occlusion scoring across heading/time samples. |
| `src/simulation/` | IV-trace and string simulation (series/bypass-diode model). |
| `CMakeLists.txt` | CMake build for `shellpower-cli` (fetches raylib via FetchContent). |
| `data/base_simulation_params.json` | Baseline Luminary template parameterized per job. |

---

## Quick Start

### Prerequisites

- Python 3.10+
- Luminary Cloud API key with project access
- CMake 3.16+ and a C compiler (for Shellpower CLI)
- (Optional) Google service account credentials for Sheets logging

### Python Setup

```bash
git clone <your-repo-url>
cd autoCFD

python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
# Edit .env with LUMINARY_API_KEY and any optional settings
```

### Running the Server

```bash
uvicorn app.main:app --reload
```

Visit `http://localhost:8000` to upload CAD files, set parameters, and monitor jobs.

---

## Building Shellpower CLI

The solar array simulator is a standalone C binary built with CMake.

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target shellpower-cli
```

The binary is created at `build/shellpower-cli`. Point the pipeline to it via the `SHELLPOWER_CLI_PATH` environment variable.

### CLI Usage

```
./build/shellpower-cli --mesh <path.obj> [options]

Required:
  --mesh <path>           OBJ or STL mesh file

Key options:
  --output <path>         Output JSON (default: shellpower_result.json)
  --preset <name>         maxeon-gen3 | maxeon-gen5 | maxeon-gen7 | generic-silicon
                          (default: maxeon-gen7)
  --target-area <float>   Max cell area in m² (default: 6.0)
  --grid-spacing <float>  Grid spacing in m (default: 0.126)
  --ignore-curvature-limit Allow high-curvature placements (flagged in output)
  --daily-sim             Run daily energy simulation
  --lat / --lon           Location (default: Alice Springs -23.7, 133.9)
  --month / --day         Date (default: 8/25 = WSC race start)
  --sim-start-hour        Race start hour (default: 8.0)
  --sim-end-hour          Race end hour (default: 17.0)
  --heading-samples       Headings to average over (default: 7)
  --min-heading           Min car heading in degrees (default: 55 = SSE)
  --max-heading           Max car heading in degrees (default: 125 = SSW)
  --no-occlusion-opt      Disable occlusion-based placement scoring
```

#### Cell area note

`--target-area` is enforced using actual **cell dimensions** (0.125 × 0.125 m for Maxeon Gen 7), not grid spacing. With `--target-area 6.0` and the Gen 7 preset, the CLI places at most `floor(6.0 / 0.015625) = 384` cells = **exactly 6.00 m²**, within the WSC 6 m² array limit.

---

## Configuration

AutoCFD reads settings from environment variables (via `app/config.py`).

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `LUMINARY_API_KEY` | Yes | — | Luminary Cloud API token. |
| `LUMINARY_PROJECT_NAME` | No | `AutoCFD Solar Car` | Target project or fallback when `project_name` is omitted. |
| `DEFAULT_FARFIELD_SPEED` | No | `24.59` | Default wind speed shown in the dashboard (m/s). |
| `BASE_SIM_TEMPLATE_PATH` | No | `data/base_simulation_params.json` | Template copied before customization; must exist. |
| `SPEED_OF_SOUND` | No | `340.29` | Used to compute Mach number. |
| `UPLOADS_DIR` | No | `uploads` | Temporary CAD storage (auto-created). |
| `SHELLPOWER_CLI_PATH` | No | — | Absolute path to the `shellpower-cli` binary. Shellpower is skipped if unset. |
| `SHELLPOWER_TARGET_AREA` | No | `6.0` | Target solar array area in m² (WSC limit is 6 m²). |
| `SHELLPOWER_ENABLE_DAILY_SIM` | No | `true` | Run the 9-hour daily energy simulation. |
| `GOOGLE_SHEETS_CREDENTIALS` | No | — | Path or JSON string for gspread. |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | No | — | Sheet to append results to. |

---

## Shellpower Solar Integration

When `SHELLPOWER_CLI_PATH` is set and `shellpower_enabled=true` is passed to a job:

1. The pipeline exports the car body surfaces from the Luminary VTU result as a watertight OBJ mesh (excluding farfield, floor, inlet/outlet surfaces).
2. `shellpower-cli` is invoked to auto-place Maxeon Gen 7 cells on the mesh, wire them into a single string with per-cell bypass diodes, and run the solar simulation.
3. Results are reported in the job log and appended to Google Sheets.

### WSC defaults

| Parameter | Value |
|-----------|-------|
| Location | Alice Springs, NT (-23.7°, 133.9°) |
| Date | August 25 (approximate WSC race start) |
| Racing hours | 08:00 – 17:00 local (9 hours) |
| Car heading | 55°–125° (±35° around due south) |
| Heading samples | 7 per time step |
| Time steps | 48 over the 9-hour window |
| Cell type | Maxeon Gen 7 (26% efficiency, 0.125 × 0.125 m) |
| Max array area | 6.00 m² (WSC regulation) |

### Output fields

The status log shows, for example:
```
✓ Shellpower: 384 cells (6.00 m²), peak 938.7 W, 5597 Wh/day, 12% shaded (sun at peak: 55°)
```

The **sun at peak** angle is the solar altitude (degrees above horizon) at the moment peak power occurs during the 9-hour window — not a fixed noon snapshot. At Alice Springs on August 25, the theoretical maximum solar altitude is ~55°.

---

## Google Sheets Integration

1. **Create a Google service account** (Cloud Console → enable Sheets + Drive → create credentials).
2. **Share your spreadsheet** with the service account email (Editor access).
3. **Configure environment variables**:
   ```bash
   GOOGLE_SHEETS_CREDENTIALS=/path/to/credentials.json   # or JSON string
   GOOGLE_SHEETS_SPREADSHEET_ID=<spreadsheet_id>
   ```

Each completed simulation logs:
- Timestamp, job/simulation identifiers, wind speed, wind direction, frontal/wetted area.
- Drag (total, viscous, pressure), side force, lift, Cd, CdA, CdW.
- Center-of-pressure coordinates, moments, force magnitude/direction.
- Convergence status, iteration limit.
- **Solar**: cells placed, peak power (W), daily energy (Wh), array map image (uploaded to Drive).
- Hyperlink to the Luminary simulation.

---

## Backfilling Historical Runs

Import prior Luminary simulations into the Google Sheet:

```bash
# Preview changes without writing
python -m app.backfill_sheets --dry-run

# Process all completed simulations in the default project
python -m app.backfill_sheets

# Target a specific project or limit the number processed
python -m app.backfill_sheets --project "AutoCFD Solar Car" --limit 10
```

---

## Deployment

### Railway (recommended)

1. Run `./scripts/prepare_credentials_for_deployment.sh` to convert your Sheets credentials JSON into a single-line string.
2. Push the repository to GitHub.
3. Create a new Railway project → deploy from GitHub → set the environment variables.
4. Railway automatically builds the Dockerfile and exposes the app.

> **Note**: The Dockerfile does not build the Shellpower CLI. For cloud deployments, build the binary locally and set `SHELLPOWER_CLI_PATH` to a mounted volume or embed the binary in the image.

Required Railway variables:
```
LUMINARY_API_KEY=<api_key>
LUMINARY_PROJECT_NAME=<project_name>
DEFAULT_FARFIELD_SPEED=24.59
GOOGLE_SHEETS_SPREADSHEET_ID=<sheet_id>
GOOGLE_SHEETS_CREDENTIALS=<single-line-json>
SHELLPOWER_CLI_PATH=/app/shellpower-cli   # if bundled in image
```

### Other Options

- **Render**: follow `render.yaml`.
- **Docker**: `docker build -t autocfd . && docker run -p 8000:8000 autocfd`.

---

## API Reference

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard with upload form and job history. |
| `POST /run` | Accepts multipart form data (CAD file + parameters). Returns `{"job_id": ...}`. |
| `GET /jobs` | Lists all jobs (newest first) with logs, results, and status. |
| `GET /jobs/{job_id}` | Returns a single job. |
| `POST /jobs/{job_id}/cancel` | Cancels a pending/running job. |

Example job submission:

```bash
curl -F cad_file=@solar_car.step \
     -F cad_label="Array15" \
     -F project_name="AutoCFD Solar Car" \
     -F farfield_speed=24.59 \
     -F mesh_min_size=0.002 \
     -F mesh_max_size=0.05 \
     -F rotating_wheels=true \
     -F shellpower_enabled=true \
     http://localhost:8000/run
```

---

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| Shellpower not running | Ensure `SHELLPOWER_CLI_PATH` points to the built binary and is executable. |
| Shellpower CLI timed out | Large meshes can take >60 s; the timeout is 600 s. Check that `--target-area` is reasonable. |
| Cell count seems off | The area limit uses actual cell size (0.125 m for Gen 7), not grid spacing (0.126 m). `384 × 0.125² = 6.00 m²`. |
| Array still asymmetric | Some asymmetry is real — occlusion scoring detects genuine shadow from the cockpit. Use `--no-occlusion-opt` to compare with a shadow-unaware layout. |
| Sheets logging reports "not configured" | Ensure both `GOOGLE_SHEETS_CREDENTIALS` and `GOOGLE_SHEETS_SPREADSHEET_ID` are set and the service account has Editor access. |
| Meshing fails | Check CAD integrity (watertight, correct scale). Adjust `mesh_min_size`/`mesh_max_size` or simplify the geometry. |
| Deployment port errors | Use the provided Dockerfile so the server listens on `$PORT`. |

---

## Project Structure

```
autoCFD/
├── app/                          # Python FastAPI application
│   ├── backfill_sheets.py
│   ├── config.py
│   ├── job_store.py
│   ├── luminary_pipeline.py
│   ├── main.py
│   ├── sheets_logger.py
│   └── templates/
│       └── index.html
├── src/                          # Shellpower C source
│   ├── cli_main.c                # CLI entry point
│   ├── core/
│   │   ├── app_core.c/h          # Headless app state and simulation
│   │   ├── auto_layout_core.c    # Grid layout + occlusion scoring
│   │   ├── core_types.h          # Shared types (Vector3, CoreMesh, etc.)
│   │   └── mesh_loader.c/h       # OBJ/STL loader
│   └── simulation/
│       ├── iv_trace.c/h          # Single-cell IV curve
│       └── string_sim.c/h        # Series string + bypass diode model
├── data/
│   └── base_simulation_params.json
├── docs/
│   └── plans/                    # Design documents
├── tests/
│   └── test_shellpower_mesh_export.py
├── assets/
│   └── Inter-Regular.otf         # Font for GUI build
├── scripts/
│   └── prepare_credentials_for_deployment.sh
├── CMakeLists.txt                # Builds shellpower-cli
├── Dockerfile
├── requirements.txt
├── railway.json
├── render.yaml
├── USER_MANUAL.md
└── README.md
```

---

## Resources

- [Luminary Cloud API Reference](https://app.luminarycloud.com/docs/api/reference)
- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [Google Sheets API](https://developers.google.com/sheets/api)
- [Original Shellpower (SSCP)](https://github.com/sscp/shellpower)
