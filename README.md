## AutoCFD Solar Car Pipeline

This project spins up a small FastAPI website that accepts a CAD upload of a solar car and pushes it through the Luminary Cloud API:

1. Creates (or reuses) a Luminary project.
2. Imports the CAD as a geometry and wraps it in an axis-aligned rectangular farfield whose footprint is 25× the vehicle dimensions and whose floor sits 1 mm below the lowest point (with optional overrides in the UI).
3. Generates a mesh via `Project.create_or_get_mesh`.
4. Builds a simulation template from `data/base_simulation_params.json`, patches the farfield velocity to **24.59 m/s** (or any value you type in the form), and maps the discovered farfield/body/floor surfaces.
5. Launches the simulation and streams the status back to the UI.

The long‑running CFD job is tracked in-memory so you can refresh the dashboard and monitor log entries while the mesh or simulation runs.

> The implementation uses the official SDK documented at [app.luminarycloud.com/docs/api/reference](https://app.luminarycloud.com/docs/api/reference/index.html). Key calls include `Project.create_geometry`, `Geometry.add_farfield`, `Project.create_or_get_mesh`, `Mesh.wait`, and `Project.create_simulation` as described in the API reference.

---

### Prerequisites

- Python 3.10+
- A Luminary Cloud account with API access and an API key.

---

### Installation

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install --upgrade pip
pip install -r requirements.txt
```

Copy the sample environment file and fill in your API key (and optional overrides):

```bash
cp .env.example .env
```

Required settings:

| Variable | Description |
| --- | --- |
| `LUMINARY_API_KEY` | Token from the Luminary API portal. |
| `LUMINARY_PROJECT_NAME` | Project to reuse for all uploads. It will be created if it does not exist. |
| `DEFAULT_FARFIELD_SPEED` | Default wind speed shown in the UI (24.59 m/s). |
| `BASE_SIM_TEMPLATE_PATH` | JSON used as the baseline template (editable). |
| `SPEED_OF_SOUND` | Used to derive the farfield Mach number. |

---

### Running the server

```bash
uvicorn app.main:app --reload
```

Browse to [http://localhost:8000](http://localhost:8000) and use the form to:

1. Upload a CAD file.
2. Pick the project, farfield multiplier (defaults to 25× the largest car dimension), mesh sizing, and optional surface names.
3. Submit the job. The service extracts the car bounding box, creates a rectangular farfield whose width/length are each `multiplier × dimension` (e.g., a 2 m × 5 m planform becomes 50 m × 125 m), places the floor 1 mm below the lowest z, and tags the body/floor/farfield surfaces.

The dashboard will display:

- Live job log (geometry upload, meshing status, simulation status).
- Final IDs for the geometry, mesh, template, and simulation.
- Any validation errors (e.g., when the SDK cannot infer your farfield/body surfaces).

Every CFD run is executed in a worker thread so you can submit multiple cases; the `/jobs` endpoint (polled by the UI) exposes the current state.

---

### Adjusting the pipeline

- **Simulation template** – edit `data/base_simulation_params.json` to reflect your preferred turbulence model, reference values, stopping conditions, etc. The pipeline patches:
  - `boundaryConditionsFluid` (separate car body wall, moving floor wall traveling with the freestream at 24.59 m/s, and farfield).
  - `referenceValues.vRef`.
  - `initializationFluid.uniformV`.
- **Moving floor** – provide floor surface names in the UI (otherwise the code looks for `floor/ground/road` tokens). The floor boundary receives a translational velocity equal to the freestream vector.
- **Surface mapping** – if your CAD/mesh uses different boundary names, enter them manually in the optional fields. Otherwise the code auto-detects farfield surfaces by matching “far/domain” and treats the remaining surfaces as the car body.
- **Farfield volume** – the farfield is now a rectangular box sized to 25× the car’s x/y dimensions (height gets the same multiplier but can be taller if needed) with the floor fixed 1 mm below the lowest z. Use the form’s multiplier/padding inputs (padding adds absolute meters to each face, and you can optionally shift the x/y center) to change the coverage if your geometry needs a different clearance.
- **Mesh sizing** – update `mesh_min_size`/`mesh_max_size` in the form or set new defaults in `.env`.
- **Adaptive mesh** – the simulation template now forces `adaptiveMeshRefinement` to `MESH_METHOD_AUTO` with a `target_cv_millions` cap of 10 to follow the “minimal mesh + Lumi adaptation” request.

---

### Local test without the UI

You can also call the FastAPI endpoints directly:

```bash
curl -F cad_file=@solar_car.step \
     -F cad_label="SolarCar" \
     -F project_name="AutoCFD Solar Car" \
     -F farfield_speed=24.59 \
     http://localhost:8000/run
```

This returns a `job_id` that you can poll via `GET /jobs/{job_id}`.

---

### Notes

- The SDK prompts for browser authentication the first time it runs (see Luminary docs). Keep a terminal nearby to follow the link.
- The job store is in-memory; restart the server to clear it.
- The automation assumes a single fluid volume (`volumeIdentifier.id == "0"`). If your geometry contains additional volumes, adjust the base template accordingly.
