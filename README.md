# AutoCFD Solar Car Pipeline

AutoCFD automates the entire Luminary Cloud CFD workflow for solar-car geometries. Upload a CAD file, track progress in a FastAPI dashboard, and log the resulting drag, lift, and side forces (including coefficients) directly to Google Sheets or your deployment platform.

---

## Contents

1. [Features](#features)
2. [Architecture Overview](#architecture-overview)
3. [Quick Start](#quick-start)
4. [Configuration](#configuration)
5. [Google Sheets Integration](#google-sheets-integration)
6. [Backfilling Historical Runs](#backfilling-historical-runs)
7. [Deployment](#deployment)
8. [API Reference](#api-reference)
9. [Advanced Usage](#advanced-usage)
10. [Troubleshooting](#troubleshooting)
11. [Project Structure](#project-structure)
12. [Contributing](#contributing)

---

## Features

- **FastAPI dashboard** for uploads, job monitoring, and cancellation.
- **Luminary Cloud automation**: geometry import, farfield creation, meshing, simulation setup, and monitoring.
- **Smart meshing** with adaptive refinement, automatic farfield sizing, and optional rotating wheels (including auto-detected surfaces and motion frames).
- **Automatic post-processing**: drag, viscous drag, pressure drag, side force, lift, center of pressure, CdA/CdW, and convergence metadata.
- **Google Sheets logging** with structured headers and hyperlinks to Luminary simulations.
- **Historical backfill script** to import previous Luminary runs.
- **Ready-to-deploy container** with templates for Railway, Render, and Cloud Run.

---

## Architecture Overview

| Component | Purpose |
|-----------|---------|
| `app/main.py` | FastAPI routes, upload handling, background execution, job dashboard. |
| `app/luminary_pipeline.py` | Core orchestration: geometry processing, meshing, simulation setup, stopping conditions, results extraction. |
| `app/job_store.py` | In-memory tracking of job metadata, logs, and cancellation. |
| `app/sheets_logger.py` | Authentication and logging into Google Sheets, including header management. |
| `app/backfill_sheets.py` | CLI to import finished Luminary simulations into Sheets. |
| `data/base_simulation_params.json` | Baseline Luminary template copied and parametrized per job. |

---

## Quick Start

### Prerequisites

- Python 3.10+
- Luminary Cloud API key with project access
- (Optional) Google service account credentials for Sheets logging

### Installation

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

### Local Development Server

```bash
uvicorn app.main:app --reload
```

Visit `http://localhost:8000` to upload CAD files, set parameters, and monitor jobs.

---

## Configuration

AutoCFD reads settings from environment variables (via `app/config.py`). Key values:

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `LUMINARY_API_KEY` | Yes | — | Luminary Cloud API token. |
| `LUMINARY_PROJECT_NAME` | No | `AutoCFD Solar Car` | Target project or fallback when `project_name` is omitted. |
| `DEFAULT_FARFIELD_SPEED` | No | `24.59` | Default wind speed shown in the dashboard. |
| `BASE_SIM_TEMPLATE_PATH` | No | `data/base_simulation_params.json` | Template copied before customization; must exist. |
| `SPEED_OF_SOUND` | No | `340.29` | Used to compute Mach number. |
| `UPLOADS_DIR` | No | `uploads` | Temporary CAD storage (auto-created). |
| `GOOGLE_SHEETS_CREDENTIALS` | No | — | Path or JSON string for gspread. |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | No | — | Sheet to append results to. |

Modify `data/base_simulation_params.json` if you need to change turbulence options, solver controls, or baseline boundary conditions. Most convergence criteria and force outputs are injected later through the SDK, not the JSON file.

---

## Google Sheets Integration

1. **Create a Google service account** (Cloud Console → enable Sheets + Drive → create credentials).
2. **Share your spreadsheet** with the service account email (Editor access).
3. **Configure environment variables**:
   ```bash
   GOOGLE_SHEETS_CREDENTIALS=/path/to/credentials.json   # or JSON string
   GOOGLE_SHEETS_SPREADSHEET_ID=<spreadsheet_id>
   ```
4. On first run, `SheetsLogger` creates headers and formats row 1. Each completed simulation logs:
   - Timestamp, job/simulation identifiers, wind speed, wind direction, frontal area.
   - Drag/viscous drag/pressure drag, side force, lift, CdA, CdW.
   - Center-of-pressure coordinates, moments, force magnitude/direction.
   - Convergence status, iteration limit, and a hyperlink to the Luminary result.

See `GOOGLE_SHEETS_SETUP.md` for screenshots and additional instructions.

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

The script loads reference values from each simulation, reuses the same force extraction logic as live runs, and logs the results via `SheetsLogger`. Errors are reported per simulation so a single failure does not abort the entire backfill.

---

## Deployment

### Railway (recommended)

1. Run `./scripts/prepare_credentials_for_deployment.sh` to convert your Sheets credentials JSON into a single-line string.
2. Push the repository to GitHub.
3. Create a new Railway project → deploy from GitHub → set the environment variables listed below.
4. Railway automatically builds the Dockerfile and exposes the app at `https://<project>.railway.app`.

Required Railway variables:
```
LUMINARY_API_KEY=<api_key>
LUMINARY_PROJECT_NAME=<project_name>
DEFAULT_FARFIELD_SPEED=24.59
GOOGLE_SHEETS_SPREADSHEET_ID=<sheet_id>
GOOGLE_SHEETS_CREDENTIALS=<single-line-json>
```

### Other Options

- **Render**: follow `render.yaml` and the instructions in `QUICKSTART_DEPLOY.md`.
- **Google Cloud Run**: see `DEPLOYMENT.md` for container registry steps and service configuration.
- **Docker**: build and run locally using `docker build -t autocfd .` and `docker run -p 8000:8000 autocfd`.

The Dockerfile installs all dependencies, copies only `app/` and `data/`, and honors the platform-provided `PORT`.

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
     -F cad_label="TestRun" \
     -F project_name="AutoCFD Solar Car" \
     -F farfield_speed=24.59 \
     -F mesh_min_size=0.002 \
     -F mesh_max_size=0.05 \
     -F farfield_multiplier=25.0 \
     -F rotating_wheels=true \
     http://localhost:8000/run
```

Polling job status:

```bash
curl http://localhost:8000/jobs/<job_id>
```

---

## Advanced Usage

- **Custom surface mapping**: override inferred body/floor/farfield names via `body_surfaces`, `floor_surfaces`, and `farfield_surfaces` form fields.
- **Farfield control**: adjust `farfield_multiplier`, add padding, or specify `farfield_center`.
- **Crosswind and yaw**: pass `wind_direction="x,y,z"`; the pipeline normalizes it to compute the farfield vector while keeping the moving-floor speed constant.
- **Rotating wheels**: enable `rotating_wheels=true` to auto-detect surfaces near the floor, or provide `wheel_surfaces` explicitly. The pipeline builds separate motion frames for front and rear wheels and excludes those surfaces from the body BC before computing forces.

Refer to the comments in `app/luminary_pipeline.py` for implementation details and additional tuning options (boundary-layer parameters, adaptive meshing toggle, etc.).

---

## Troubleshooting

| Issue | Resolution |
|-------|------------|
| Sheets logging reports “not configured” | Ensure both `GOOGLE_SHEETS_CREDENTIALS` and `GOOGLE_SHEETS_SPREADSHEET_ID` are set and the service account has editor access. |
| Meshing fails | Check CAD integrity (watertight, correct scale). Adjust `mesh_min_size`/`mesh_max_size` or simplify the geometry. |
| Simulation stuck or fails | Review the Luminary log link included in the job log. Verify farfield size, boundary naming, and turbulence settings. |
| Deployment port errors | Use the provided Dockerfile so the server listens on `$PORT`. Platforms such as Railway and Render set this dynamically. |
| Force download errors | Ensure you’re running the current codebase; older versions attempted to read deprecated SDK attributes. |

When debugging, inspect the running server logs (`uvicorn` output locally or platform logs in Railway/Render) and the per-job log in the dashboard. The pipeline also saves each generated simulation payload under `dumps/` for reproducibility.

---

## Project Structure

```
autoCFD/
├── app/
│   ├── backfill_sheets.py
│   ├── config.py
│   ├── job_store.py
│   ├── luminary_pipeline.py
│   ├── main.py
│   ├── sheets_logger.py
│   └── templates/
│       └── index.html
├── data/
│   └── base_simulation_params.json
├── scripts/
│   └── prepare_credentials_for_deployment.sh
├── requirements.txt
├── Dockerfile
├── railway.json
├── render.yaml
├── DEPLOYMENT.md
├── QUICKSTART_DEPLOY.md
├── GOOGLE_SHEETS_SETUP.md
└── README.md
```

---

## Contributing

Contributions are welcome:

1. Fork the repository.
2. Create a feature branch.
3. Make changes and add tests if applicable.
4. Open a pull request describing the change and validation steps.

For bug reports or feature requests, file an issue with reproduction details and relevant logs.

---

## Resources

- [Luminary Cloud API Reference](https://app.luminarycloud.com/docs/api/reference)
- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [Google Sheets API](https://developers.google.com/sheets/api)
- [Railway Docs](https://docs.railway.app)
- [Render Docs](https://render.com/docs)

This project uses the Luminary Cloud SDK and adheres to Luminary’s terms of service.
