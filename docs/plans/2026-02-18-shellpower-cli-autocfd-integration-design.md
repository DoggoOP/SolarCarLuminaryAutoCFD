# Shellpower CLI + AutoCFD Integration — Design

**Date:** 2026-02-18
**Branch:** autoshellpower
**Status:** Approved

---

## Overview

This design adds a headless `shellpower-cli` binary to the Shellpower++ C project and integrates it into the AutoCFD Python pipeline so that every CFD run optionally produces a solar array layout with daily energy estimates alongside the aerodynamic results.

---

## Part 1: C Architecture — Core Library Extraction (Approach A)

### Goal

Decouple all physics/computation code from Raylib's window/GPU subsystem so that the CLI target requires no display context.

### Directory Structure

```
src/
  core/
    mesh_loader.c/h   ← pure-C OBJ + STL parser (extends existing stl_loader.c, adds OBJ)
    auto_layout.c/h   ← moved from src/; logic unchanged
    simulation/
      iv_trace.c/h    ← moved; logic unchanged
      string_sim.c/h  ← moved; logic unchanged
    app_core.c/h      ← AppState struct + AppInit/AppClose, cell/string ops, layout, sim
                         (no GUI calls, no tinyfiledialogs, no curl)
  cli_main.c          ← NEW: CLI entry point
  main.c              ← unchanged GUI entry point (links core + Raylib + GUI code)
  app.c               ← unchanged GUI code; includes app_core.h
  gui.c               ← unchanged
  camera.c            ← unchanged
  updater.c           ← unchanged
  lib/
    tinyfiledialogs.c ← unchanged (GUI only)
```

`raymath.h` is a header-only math library with no GPU dependency; it is used freely in `src/core/`.

### Raylib Dependency Replacements

| Raylib call (current) | Replacement in core |
|----------------------|---------------------|
| `LoadModel()` / `LoadMesh()` | New pure-C `LoadOBJMesh()` / `LoadSTLMesh()` in `mesh_loader.c` populating `vertices[]`, `normals[]`, `indices[]` arrays |
| `GetRayCollisionMesh()` | ~50-line Möller–Trumbore triangle intersection in `mesh_loader.c` |
| `GetRayCollisionSphere()` | Inline sphere test (3 lines) |
| `CheckCollisionBoxSphere()` | Inline AABB-sphere test |

### CMake Targets

```cmake
# Existing GUI target (unchanged)
add_executable(shellpower <all sources> src/lib/tinyfiledialogs.c)
target_link_libraries(shellpower PRIVATE raylib CURL::libcurl ...)

# New CLI target
set(CORE_SOURCES
    src/core/mesh_loader.c
    src/core/auto_layout.c
    src/core/app_core.c
    src/core/simulation/iv_trace.c
    src/core/simulation/string_sim.c
    src/cli_main.c
)
add_executable(shellpower-cli ${CORE_SOURCES})
target_include_directories(shellpower-cli PRIVATE src src/core ...)
target_link_libraries(shellpower-cli PRIVATE m)  # math only; no raylib, no curl
```

---

## Part 2: CLI Interface

### Invocation

```bash
shellpower-cli \
  --mesh car.obj \
  --scale 1.0 \
  --rotate-x 0 --rotate-y 0 --rotate-z 0 \
  --preset maxeon-gen5 \
  --grid-spacing 0.13 \
  --target-area 6.0 \
  --min-angle 62 --max-angle 90 \
  --daily-sim \
  --lat 37.4 --lon -122.2 \
  --month 6 --day 21 \
  --time-samples 48 --heading-samples 12 \
  --output shellpower_result.json
```

Preset name options: `maxeon-gen3`, `maxeon-gen5`, `generic-silicon`.
Exit code: `0` on success, non-zero on error (missing mesh, layout failure, etc.).
Errors are written to stderr; the JSON output file is only written on success.

### Automated Workflow (cli_main.c)

1. Parse CLI args (use `getopt_long` or simple manual parsing).
2. `AppState app = {0}; AppInit(&app);` — no window initialization.
3. Apply settings: `app.mesh_scale`, `app.mesh_rotation`, `app.auto_layout.*`, `app.sim_settings.*`.
4. `LoadVehicleMesh(&app, mesh_path); UpdateMeshTransform(&app);`
5. `RunAutoLayout(&app);`
6. Auto-wire all placed cells in snake-pattern row order: `StartNewString(&app)`, iterate cells sorted by (row, col), `AddCellToString`, `EndCurrentString(&app)`.
7. `RunStaticSimulation(&app);` (instant snapshot at noon on provided date/location).
8. If `--daily-sim`: `RunTimeSimulationAnimated(&app);`
9. Serialize results to JSON → write to `--output` path.
10. `AppClose(&app);` — exit.

### Output JSON Schema

```json
{
  "metadata": {
    "mesh_path": "car.obj",
    "preset": "Maxeon Gen 5",
    "cell_width_m": 0.125,
    "cell_height_m": 0.125,
    "cell_count": 84,
    "total_area_m2": 1.3125
  },
  "layout": [
    {
      "id": 0,
      "position": [x, y, z],
      "normal": [nx, ny, nz],
      "string_id": 0,
      "order_in_string": 0
    }
  ],
  "instant_power": {
    "total_power_w": 312.4,
    "shaded_pct": 3.1,
    "sun_altitude": 72.0,
    "sun_azimuth": 180.0
  },
  "daily_energy": {
    "total_energy_wh": 1820.0,
    "average_power_w": 227.5,
    "peak_power_w": 361.0,
    "average_shaded_pct": 5.2
  }
}
```

`daily_energy` key is only present when `--daily-sim` was passed.

---

## Part 3: Python Mesh Conversion (VTU → OBJ)

Runs after the Luminary simulation completes (VTU already downloaded for post-processing).

### New Helper

`_export_shellpower_mesh(case_config, vtu_path, out_obj_path)` in `luminary_pipeline.py`:

1. `meshio.read(vtu_path)` — load the full VTU.
2. Filter zones: exclude meshio blocks/tags whose names match `farfield*`, `floor*`, and any surface names in `case_config.wheel_surfaces` (or the auto-detected wheel surface list if `rotating_wheels=True`).
3. Collect only triangle cells from the remaining zones.
4. Apply coordinate transform to all vertices and normals:
   ```
   R = [[1, 0,  0],
        [0, 0,  1],
        [0,-1,  0]]
   ```
   This maps Luminary's (X_fwd, Y_side, Z_up) → Shellpower's (X_fwd, Y_up, Z_right).
5. Shift vertices so `min(x) = 0` and `min(z) = 0`.
6. Write to `out_obj_path` in Wavefront OBJ format (vertices, normals, faces).

---

## Part 4: AutoCFD Pipeline Integration

### Config (`app/config.py`)

New `Settings` fields:
```python
shellpower_cli_path: Optional[str] = None   # Path to shellpower-cli binary; disables feature if None
shellpower_target_area: float = 6.0         # Default target array area (m²)
shellpower_enable_daily_sim: bool = True    # Whether to request daily energy sim
```

### `CaseConfig` Additions

```python
shellpower_enabled: bool = False
shellpower_target_area: float  # inherited from Settings default
```

Exposed as an optional checkbox on the `/run` form.

### Pipeline Sequence

After `_compute_results()` (VTU already downloaded):

```python
if config.shellpower_enabled and settings.shellpower_cli_path:
    obj_path = uploads_dir / job_id / "shellpower_input.obj"
    _export_shellpower_mesh(config, vtu_path, obj_path)

    sp_json_path = uploads_dir / job_id / "shellpower_result.json"
    cmd = [
        settings.shellpower_cli_path,
        "--mesh", str(obj_path),
        "--scale", "1.0",          # VTU is already in meters
        "--target-area", str(config.shellpower_target_area),
        "--daily-sim",
        "--lat", str(config.sim_lat),
        "--lon", str(config.sim_lon),
        "--output", str(sp_json_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    # append proc.stdout/stderr to job logs
    result["shellpower"] = json.loads(sp_json_path.read_text())
    # copy artifacts to dumps/<job_id>/shellpower/
```

Errors in this block are caught and logged as warnings — a Shellpower failure must never abort the CFD result.

### Dashboard (`app/templates/index.html`)

When `job.result.shellpower` is present, render a "Solar Array" results card showing:
- Cell count and total array area
- Instant power (W) and daily energy (Wh)
- Average and peak power
- Download links: `shellpower_result.json`, `shellpower_input.obj`

### Google Sheets (`app/sheets_logger.py`)

New columns appended after existing force columns:
`array_area_m2`, `daily_energy_wh`, `avg_power_w`, `peak_power_w`

Written as `N/A` when Shellpower was not run for the job.

### Backfill (`app/backfill_sheets.py`)

No retroactive CLI execution. If a `shellpower_result.json` exists under `dumps/<job_id>/shellpower/`, it is read and logged. Otherwise, new columns are written as `N/A`.

---

## Error Handling

| Failure point | Behavior |
|--------------|----------|
| `shellpower_cli_path` not set | Shellpower silently skipped for all jobs |
| `shellpower_enabled=False` on job | Shellpower skipped for that job |
| VTU mesh export fails | Warning logged; Shellpower skipped; CFD result unaffected |
| `shellpower-cli` non-zero exit | stderr appended to job log; `result["shellpower"]` not set |
| `shellpower-cli` timeout (>120s) | Process killed; warning logged |
| Sheets columns missing | `SheetsLogger` adds them on next write (existing header logic) |

---

## Testing Strategy

### C / CLI
- Unit test `mesh_loader.c` with a known OBJ cube: verify vertex/normal counts and triangle winding.
- CLI integration test: run `shellpower-cli` against a simple box mesh, assert JSON output has non-zero `cell_count` and `daily_energy.total_energy_wh > 0`.
- Build both targets on macOS and Linux CI; confirm `shellpower-cli` has no dynamic Raylib dependency.

### Python
- Unit test `_export_shellpower_mesh` with a synthetic VTU (generated by meshio): verify farfield/floor/wheel zones are excluded, coordinate transform is correct.
- Integration test: mock `subprocess.run` to return a fixture JSON; verify `result["shellpower"]` is populated and Sheets columns are written.
