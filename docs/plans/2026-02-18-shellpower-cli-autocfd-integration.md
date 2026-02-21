# Shellpower CLI + AutoCFD Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a headless `shellpower-cli` binary that runs auto-layout + simulation on an OBJ mesh and outputs JSON, then integrate it into AutoCFD so every CFD run optionally produces solar array metrics alongside drag/lift results.

**Architecture:** Extract all physics/computation code into `src/core/` (no Raylib GPU deps) so `shellpower-cli` links against only pure C math. Existing GUI sources (`src/app.c`, `src/main.c`, etc.) are **not changed**. On the Python side, after the Luminary simulation completes, the VTU mesh is filtered and exported as OBJ, then passed to the CLI; results are attached to the job and logged to Sheets.

**Tech Stack:** C99 + raymath.h (Vector3 math, no GPU), meshio (Python VTU → OBJ), FastAPI + Jinja2 (dashboard), gspread (Sheets)

**Design doc:** `docs/plans/2026-02-18-shellpower-cli-autocfd-integration-design.md`

---

## Part 1 — C Core Library

### Task 1: Create `src/core/core_types.h`

**Files:**
- Create: `src/core/core_types.h`

**Step 1: Write the header**

```c
#ifndef CORE_TYPES_H
#define CORE_TYPES_H

/* Pure-C types for the headless core library.
 * raymath.h is header-only (no GPU deps) — safe to include here.
 */
#include "raymath.h"
#include <stdbool.h>

/* Replaces Raylib's Model + Mesh combo.
 * Vertices/normals are stored in world space (transform baked in). */
typedef struct {
    float    *vertices;    /* xyz triplets, count = vertex_count * 3 */
    float    *normals;     /* xyz triplets, count = vertex_count * 3 */
    int      *indices;     /* triangle index triplets, count = tri_count * 3 */
    int       vertex_count;
    int       tri_count;
} CoreMesh;

/* Replaces Raylib's Ray */
typedef struct {
    Vector3 origin;
    Vector3 direction;
} CoreRay;

/* Replaces Raylib's RayCollision */
typedef struct {
    bool    hit;
    float   distance;
    Vector3 point;
    Vector3 normal;
} CoreHit;

#endif /* CORE_TYPES_H */
```

**Step 2: Verify the file compiles cleanly** (no dependencies to link yet)

```bash
cc -std=c99 -I/opt/homebrew/include -c src/core/core_types.h -o /dev/null 2>&1 || true
```

---

### Task 2: Create `src/core/mesh_loader.h` and `src/core/mesh_loader.c`

**Files:**
- Create: `src/core/mesh_loader.h`
- Create: `src/core/mesh_loader.c`

**Step 1: Write `mesh_loader.h`**

```c
#ifndef MESH_LOADER_H
#define MESH_LOADER_H

#include "core_types.h"

/* Load OBJ (or STL) file into a CoreMesh.
 * Returns a zeroed struct on failure (tri_count == 0). */
CoreMesh CoreMesh_Load(const char *path);

/* Free heap memory inside mesh. */
void CoreMesh_Free(CoreMesh *m);

/* Möller–Trumbore ray-triangle intersection.
 * Returns a CoreHit; hit.hit == false if no intersection. */
CoreHit CoreMesh_Raycast(const CoreMesh *m, CoreRay ray);

/* Apply a transform to all vertices and normals in-place.
 * Use this after loading to bake scale/rotation. */
void CoreMesh_Transform(CoreMesh *m, Matrix transform);

/* Compute axis-aligned bounding box. */
BoundingBox CoreMesh_BoundingBox(const CoreMesh *m);

#endif /* MESH_LOADER_H */
```

**Step 2: Write `mesh_loader.c` — OBJ parser**

```c
#include "mesh_loader.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- helpers ---- */
static bool _ends_with_ci(const char *path, const char *ext) {
    int pl = (int)strlen(path), el = (int)strlen(ext);
    if (pl < el) return false;
    for (int i = 0; i < el; i++)
        if (tolower((unsigned char)path[pl-el+i]) != tolower((unsigned char)ext[i])) return false;
    return true;
}

/* ---- STL binary loader (adapted from existing stl_loader.c logic) ---- */
static CoreMesh _load_stl(const char *path) {
    CoreMesh m = {0};
    FILE *f = fopen(path, "rb");
    if (!f) return m;

    char header[80]; fread(header, 1, 80, f);
    unsigned int n_tri = 0; fread(&n_tri, 4, 1, f);
    if (n_tri == 0 || n_tri > 2000000) { fclose(f); return m; }

    m.tri_count    = (int)n_tri;
    m.vertex_count = (int)n_tri * 3;
    m.vertices = (float *)malloc(m.vertex_count * 3 * sizeof(float));
    m.normals  = (float *)malloc(m.vertex_count * 3 * sizeof(float));
    m.indices  = (int   *)malloc(m.tri_count    * 3 * sizeof(int));
    if (!m.vertices || !m.normals || !m.indices) { CoreMesh_Free(&m); fclose(f); return (CoreMesh){0}; }

    for (int t = 0; t < (int)n_tri; t++) {
        float nx, ny, nz, v[9]; unsigned short attr;
        fread(&nx, 4, 1, f); fread(&ny, 4, 1, f); fread(&nz, 4, 1, f);
        fread(v, 4, 9, f); fread(&attr, 2, 1, f);
        for (int vi = 0; vi < 3; vi++) {
            int idx = t * 3 + vi;
            m.vertices[idx*3+0] = v[vi*3+0];
            m.vertices[idx*3+1] = v[vi*3+1];
            m.vertices[idx*3+2] = v[vi*3+2];
            m.normals [idx*3+0] = nx;
            m.normals [idx*3+1] = ny;
            m.normals [idx*3+2] = nz;
            m.indices [t*3+vi]  = idx;
        }
    }
    fclose(f);
    return m;
}

/* ---- OBJ loader ---- */
static CoreMesh _load_obj(const char *path) {
    CoreMesh m = {0};
    FILE *f = fopen(path, "r");
    if (!f) return m;

    /* Two-pass: count then fill */
    int n_v = 0, n_vn = 0, n_f = 0;
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        if      (strncmp(line, "v ", 2)  == 0) n_v++;
        else if (strncmp(line, "vn ", 3) == 0) n_vn++;
        else if (strncmp(line, "f ", 2)  == 0) n_f++;
    }
    rewind(f);

    float *verts = (float *)malloc(n_v  * 3 * sizeof(float));
    float *norms = (float *)malloc(n_vn * 3 * sizeof(float));
    if (!verts || !norms) { free(verts); free(norms); fclose(f); return m; }

    int vi = 0, ni = 0;
    m.tri_count    = n_f;
    m.vertex_count = n_f * 3;
    m.vertices = (float *)malloc(m.vertex_count * 3 * sizeof(float));
    m.normals  = (float *)malloc(m.vertex_count * 3 * sizeof(float));
    m.indices  = (int   *)malloc(m.tri_count    * 3 * sizeof(int));
    if (!m.vertices || !m.normals || !m.indices) {
        free(verts); free(norms); CoreMesh_Free(&m); fclose(f); return (CoreMesh){0};
    }

    int fi = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "v ", 2) == 0 && vi < n_v) {
            sscanf(line+2, "%f %f %f", &verts[vi*3], &verts[vi*3+1], &verts[vi*3+2]); vi++;
        } else if (strncmp(line, "vn ", 3) == 0 && ni < n_vn) {
            sscanf(line+3, "%f %f %f", &norms[ni*3], &norms[ni*3+1], &norms[ni*3+2]); ni++;
        } else if (strncmp(line, "f ", 2) == 0 && fi < n_f) {
            /* Support: v, v//vn, v/vt/vn */
            int v_idx[3] = {0}, vn_idx[3] = {0};
            int matched = 0;
            char *p = line + 2;
            for (int k = 0; k < 3; k++) {
                int a = 0, b = 0, c = 0;
                int r = sscanf(p, "%d/%d/%d", &a, &b, &c);
                if (r < 3) { b = 0; c = 0; sscanf(p, "%d//%d", &a, &c); }
                if (r < 1 && c == 0) sscanf(p, "%d", &a);
                v_idx[k]  = a - 1;  /* OBJ is 1-indexed */
                vn_idx[k] = c - 1;
                while (*p && *p != ' ' && *p != '\n') p++;
                while (*p == ' ') p++;
                matched++;
            }
            if (matched == 3) {
                for (int k = 0; k < 3; k++) {
                    int out = fi * 3 + k;
                    m.vertices[out*3+0] = verts[v_idx[k]*3+0];
                    m.vertices[out*3+1] = verts[v_idx[k]*3+1];
                    m.vertices[out*3+2] = verts[v_idx[k]*3+2];
                    if (vn_idx[k] >= 0 && vn_idx[k] < n_vn) {
                        m.normals[out*3+0] = norms[vn_idx[k]*3+0];
                        m.normals[out*3+1] = norms[vn_idx[k]*3+1];
                        m.normals[out*3+2] = norms[vn_idx[k]*3+2];
                    }
                    m.indices[out] = out;
                }
                fi++;
            }
        }
    }
    m.tri_count    = fi;
    m.vertex_count = fi * 3;
    free(verts); free(norms); fclose(f);
    return m;
}

CoreMesh CoreMesh_Load(const char *path) {
    if (_ends_with_ci(path, ".stl")) return _load_stl(path);
    return _load_obj(path);
}

void CoreMesh_Free(CoreMesh *m) {
    free(m->vertices); free(m->normals); free(m->indices);
    memset(m, 0, sizeof(*m));
}

/* Möller–Trumbore */
CoreHit CoreMesh_Raycast(const CoreMesh *m, CoreRay ray) {
    CoreHit closest = {0}; closest.distance = 1e30f;
    const float EPS = 1e-7f;
    for (int t = 0; t < m->tri_count; t++) {
        int i0 = m->indices[t*3+0], i1 = m->indices[t*3+1], i2 = m->indices[t*3+2];
        Vector3 v0 = {m->vertices[i0*3], m->vertices[i0*3+1], m->vertices[i0*3+2]};
        Vector3 v1 = {m->vertices[i1*3], m->vertices[i1*3+1], m->vertices[i1*3+2]};
        Vector3 v2 = {m->vertices[i2*3], m->vertices[i2*3+1], m->vertices[i2*3+2]};
        Vector3 e1 = Vector3Subtract(v1, v0), e2 = Vector3Subtract(v2, v0);
        Vector3 h = Vector3CrossProduct(ray.direction, e2);
        float   a = Vector3DotProduct(e1, h);
        if (fabsf(a) < EPS) continue;
        float   f = 1.0f / a;
        Vector3 s = Vector3Subtract(ray.origin, v0);
        float   u = f * Vector3DotProduct(s, h);
        if (u < 0.0f || u > 1.0f) continue;
        Vector3 q = Vector3CrossProduct(s, e1);
        float   v = f * Vector3DotProduct(ray.direction, q);
        if (v < 0.0f || u + v > 1.0f) continue;
        float   dist = f * Vector3DotProduct(e2, q);
        if (dist < EPS || dist >= closest.distance) continue;
        closest.hit      = true;
        closest.distance = dist;
        closest.point    = Vector3Add(ray.origin, Vector3Scale(ray.direction, dist));
        closest.normal   = Vector3Normalize(Vector3CrossProduct(e1, e2));
    }
    return closest;
}

void CoreMesh_Transform(CoreMesh *m, Matrix transform) {
    for (int i = 0; i < m->vertex_count; i++) {
        Vector3 v = {m->vertices[i*3], m->vertices[i*3+1], m->vertices[i*3+2]};
        v = Vector3Transform(v, transform);
        m->vertices[i*3+0] = v.x; m->vertices[i*3+1] = v.y; m->vertices[i*3+2] = v.z;
        Vector3 n = {m->normals[i*3], m->normals[i*3+1], m->normals[i*3+2]};
        /* Transform normal with inverse-transpose (for uniform scales just reuse) */
        n = Vector3Normalize(Vector3Transform(n, transform));
        m->normals[i*3+0] = n.x; m->normals[i*3+1] = n.y; m->normals[i*3+2] = n.z;
    }
}

BoundingBox CoreMesh_BoundingBox(const CoreMesh *m) {
    BoundingBox bb = { {1e30f,1e30f,1e30f}, {-1e30f,-1e30f,-1e30f} };
    for (int i = 0; i < m->vertex_count; i++) {
        float x = m->vertices[i*3], y = m->vertices[i*3+1], z = m->vertices[i*3+2];
        if (x < bb.min.x) bb.min.x = x; if (x > bb.max.x) bb.max.x = x;
        if (y < bb.min.y) bb.min.y = y; if (y > bb.max.y) bb.max.y = y;
        if (z < bb.min.z) bb.min.z = z; if (z > bb.max.z) bb.max.z = z;
    }
    return bb;
}
```

**Step 3: Build and verify mesh_loader compiles**

```bash
cc -std=c99 -I/opt/homebrew/include -I src/core -c src/core/mesh_loader.c -o /tmp/mesh_loader.o
echo "Exit: $?"
```

Expected: exit 0, no errors.

---

### Task 3: Create `src/core/app_core.h`

**Files:**
- Create: `src/core/app_core.h`

This is a trimmed `AppState` that uses `CoreMesh` instead of Raylib's `Model`/`Mesh`, with GUI-only fields removed. The same `SolarCell`, `CellString`, `BypassDiode`, `SimSettings`, `SimResults`, `TimeSimResults`, `AutoLayoutSettings`, `SnapSettings`, and `CellPreset` structs from `app.h` are reused — but since `app.h` includes `raylib.h`, we must NOT include `app.h` here. We copy the relevant structs.

**Step 1: Write the header (structs + declarations)**

```c
#ifndef APP_CORE_H
#define APP_CORE_H

#include "core_types.h"
#include <stdbool.h>

/* Limits — keep in sync with app.h */
#define MAX_CELLS 1000
#define MAX_STRINGS 50
#define MAX_CELLS_PER_STRING 500
#define MAX_PATH_LENGTH 512
#define MAX_BYPASS_DIODES 100
#define CELL_SURFACE_OFFSET 0.002f
#define MIN_CELL_DISTANCE_FACTOR 1.05f
#define MIN_UPWARD_NORMAL 0.3f

/* ---- Copy of structs from app.h that have no Raylib dependency ---- */

typedef struct {
    const char *name;
    float width, height, efficiency;
    float voc, isc, vmp, imp;
    float n_ideal, series_r, bypass_v_drop;
} CellPreset;

typedef struct {
    int id;
    Vector3 local_position;
    Vector3 local_tangent;
    Vector3 local_normal;
    int string_id;
    int order_in_string;
    bool has_bypass_diode;
    bool is_shaded;
    bool is_bypassed;
    float power_output;
    float current_output;
    float voltage_output;
} SolarCell;

typedef struct {
    int id;
    int cell_ids[MAX_CELLS_PER_STRING];
    int cell_count;
    float total_power;
    float total_energy_wh;
    float string_current;
    float string_voltage;
    int bypassed_count;
    float power_ideal;
} CellString;

typedef struct {
    int id;
    int string_id;
    int start_cell_id;
    int end_cell_id;
    bool is_conducting;
    float voltage_drop;
} BypassDiode;

typedef struct {
    float latitude, longitude;
    int year, month, day;
    float hour;
    float irradiance;
} SimSettings;

typedef struct {
    float target_area;
    float min_normal_angle, max_normal_angle, surface_threshold;
    int time_samples;
    bool optimize_occlusion;
    bool preview_surface;
    bool use_height_constraint;
    bool auto_detect_height;
    float height_tolerance;
    float min_height, max_height;
    bool use_grid_layout;
    float grid_spacing;
} AutoLayoutSettings;

typedef struct {
    bool grid_snap_enabled;
    float grid_size;
    bool align_to_surface;
    bool show_grid;
    Vector3 grid_origin;
    Vector3 grid_normal;
    float grid_rotation;
    bool setting_grid_origin;
    bool grid_configured;
} SnapSettings;

typedef struct {
    float total_power;
    float shaded_percentage;
    int shaded_count;
    Vector3 sun_direction;
    float sun_altitude, sun_azimuth;
    bool is_daytime;
} SimResults;

typedef struct {
    float total_energy_wh;
    float average_power_w;
    float peak_power_w;
    float average_shaded_pct;
    float min_power_w;
    float energy_by_hour[24];
} TimeSimResults;

/* ---- Core application state (no GUI fields) ---- */

typedef struct {
    /* Mesh */
    CoreMesh    core_mesh;
    BoundingBox mesh_bounds;
    bool        mesh_loaded;
    float       mesh_scale;
    Vector3     mesh_rotation;
    char        mesh_path[MAX_PATH_LENGTH];

    /* Cells */
    SolarCell cells[MAX_CELLS];
    int cell_count;
    int next_cell_id;
    int selected_preset;

    /* Strings */
    CellString strings[MAX_STRINGS];
    int string_count;
    int next_string_id;
    int active_string_id;

    /* Bypass diodes */
    BypassDiode bypass_diodes[MAX_BYPASS_DIODES];
    int bypass_diode_count;
    int next_bypass_diode_id;

    /* Auto-layout */
    AutoLayoutSettings auto_layout;
    bool auto_layout_running;
    int  auto_layout_progress;

    /* Snap */
    SnapSettings snap;

    /* Simulation */
    SimSettings  sim_settings;
    SimResults   sim_results;
    bool         sim_run;
    bool         time_sim_run;
    TimeSimResults time_sim_results;

    /* Status */
    char status_msg[256];
} CoreAppState;

/* ---- Cell presets (defined in app_core.c) ---- */
extern const CellPreset CORE_CELL_PRESETS[];
extern const int CORE_CELL_PRESET_COUNT;

/* ---- Function declarations ---- */

void CoreApp_Init(CoreAppState *app);
void CoreApp_Close(CoreAppState *app);

bool CoreApp_LoadMesh(CoreAppState *app, const char *path);
void CoreApp_ApplyTransform(CoreAppState *app);

/* Cell ops */
int  CoreApp_PlaceCell(CoreAppState *app, Vector3 world_pos, Vector3 world_normal);
void CoreApp_ClearCells(CoreAppState *app);
Vector3 CoreApp_CellWorldPos(CoreAppState *app, SolarCell *cell);
Vector3 CoreApp_CellWorldNormal(CoreAppState *app, SolarCell *cell);

/* Wiring */
int  CoreApp_StartString(CoreAppState *app);
void CoreApp_AddCellToString(CoreAppState *app, int cell_id);
void CoreApp_EndString(CoreAppState *app);
void CoreApp_ClearWiring(CoreAppState *app);

/* Auto-layout */
void CoreApp_InitAutoLayout(CoreAppState *app);
int  CoreApp_RunAutoLayout(CoreAppState *app);

/* Simulation */
void CoreApp_RunStaticSim(CoreAppState *app);
void CoreApp_RunDailySim(CoreAppState *app);

/* Snap */
void CoreApp_InitSnap(CoreAppState *app);

#endif /* APP_CORE_H */
```

---

### Task 4: Create `src/core/app_core.c`

**Files:**
- Create: `src/core/app_core.c`

Port all non-GUI logic from `src/app.c` into this file, replacing:
- `app->vehicle_mesh` → `app->core_mesh`
- `app->vehicle_model.transform` → removed (transform baked in during load)
- `GetRayCollisionMesh(...)` → `CoreMesh_Raycast(&app->core_mesh, ray)`
- `RayCollision` → `CoreHit`
- `LoadSTL` / `LoadModel` → `CoreMesh_Load`
- `GuiSetStyle`, `DrawText`, etc. → removed (not in core)
- `SetStatus` → `snprintf(app->status_msg, ...)`
- `Camera*` ops → removed

**Step 1: Write core init/close and mesh loading**

```c
#include "app_core.h"
#include "mesh_loader.h"
#include "simulation/iv_trace.h"
#include "simulation/string_sim.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

const CellPreset CORE_CELL_PRESETS[] = {
    {"Maxeon Gen 3 (ME3)", 0.125f, 0.125f, 0.227f, 0.686f, 6.27f, 0.58f, 6.01f, 1.26f, 0.003f, 0.35f},
    {"Maxeon Gen 5",       0.125f, 0.125f, 0.240f, 0.700f, 6.50f, 0.60f, 6.24f, 1.25f, 0.003f, 0.35f},
    {"Generic Silicon",    0.156f, 0.156f, 0.200f, 0.620f, 8.00f, 0.50f, 7.60f, 1.30f, 0.005f, 0.35f},
};
const int CORE_CELL_PRESET_COUNT = 3;

void CoreApp_Init(CoreAppState *app) {
    app->active_string_id = -1;
    app->selected_preset  = 1; /* Maxeon Gen 5 default */
    app->mesh_scale       = 1.0f;
    app->next_cell_id     = 0;
    app->next_string_id   = 0;
    app->next_bypass_diode_id = 0;

    /* Simulation defaults */
    app->sim_settings.latitude   = 37.4f;
    app->sim_settings.longitude  = -122.2f;
    app->sim_settings.month      = 6;
    app->sim_settings.day        = 21;
    app->sim_settings.hour       = 12.0f;
    app->sim_settings.irradiance = 1000.0f;

    CoreApp_InitAutoLayout(app);
    CoreApp_InitSnap(app);
}

void CoreApp_Close(CoreAppState *app) {
    CoreMesh_Free(&app->core_mesh);
}

bool CoreApp_LoadMesh(CoreAppState *app, const char *path) {
    CoreMesh_Free(&app->core_mesh);
    app->core_mesh = CoreMesh_Load(path);
    if (app->core_mesh.tri_count == 0) {
        snprintf(app->status_msg, sizeof(app->status_msg), "Failed to load mesh: %s", path);
        return false;
    }
    strncpy(app->mesh_path, path, MAX_PATH_LENGTH - 1);
    app->mesh_loaded = true;
    CoreApp_ApplyTransform(app);
    return true;
}

void CoreApp_ApplyTransform(CoreAppState *app) {
    if (!app->mesh_loaded) return;
    /* Build transform matrix: scale then rotate */
    Matrix t = MatrixScale(app->mesh_scale, app->mesh_scale, app->mesh_scale);
    if (app->mesh_rotation.x != 0.0f)
        t = MatrixMultiply(t, MatrixRotateX(app->mesh_rotation.x * DEG2RAD));
    if (app->mesh_rotation.y != 0.0f)
        t = MatrixMultiply(t, MatrixRotateY(app->mesh_rotation.y * DEG2RAD));
    if (app->mesh_rotation.z != 0.0f)
        t = MatrixMultiply(t, MatrixRotateZ(app->mesh_rotation.z * DEG2RAD));
    CoreMesh_Transform(&app->core_mesh, t);
    app->mesh_bounds = CoreMesh_BoundingBox(&app->core_mesh);
}
```

**Step 2: Write cell placement helpers**

Copy `PlaceCell`, `PlaceCellEx`, `ClearAllCells`, `CellGetWorldPosition`, `CellGetWorldNormal` from `src/app.c`.
- Replace `AppState *` → `CoreAppState *`
- Replace `RayCollision hit = GetRayCollisionMesh(ray, app->vehicle_mesh, app->vehicle_model.transform)` → `CoreHit hit = CoreMesh_Raycast(&app->core_mesh, ray)`
- `Ray` → `CoreRay`
- `hit.hit`, `hit.point`, `hit.normal` — same field names, works as-is

Functions to port: `CoreApp_PlaceCell`, `CoreApp_ClearCells`, `CoreApp_CellWorldPos`, `CoreApp_CellWorldNormal`.

**Step 3: Write wiring helpers**

Copy `StartNewString`, `AddCellToString`, `EndCurrentString`, `ClearAllWiring` from `src/app.c`.
- Replace `AppState *` → `CoreAppState *`
- Drop `GenerateStringColor` (no colors in core, or set to a fixed value)

**Step 4: Write snap init**

```c
void CoreApp_InitSnap(CoreAppState *app) {
    app->snap.grid_size          = 0.13f;
    app->snap.align_to_surface   = true;
    app->snap.grid_snap_enabled  = false;
    app->snap.show_grid          = false;
    app->snap.grid_rotation      = 0.0f;
}
```

**Step 5: Write simulation runners**

Copy `RunStaticSimulation` and `RunTimeSimulationAnimated` from `src/app.c`.
- Replace `AppState *` → `CoreAppState *`
- Replace `GetRayCollisionMesh` → `CoreMesh_Raycast`
- Replace `CELL_PRESETS` → `CORE_CELL_PRESETS`

Rename to `CoreApp_RunStaticSim` and `CoreApp_RunDailySim`.

**Step 6: Build and verify**

```bash
cc -std=c99 -I/opt/homebrew/include -I src/core -I src/core/simulation \
   -c src/core/app_core.c -o /tmp/app_core.o 2>&1
echo "Exit: $?"
```

Expected: exit 0.

---

### Task 5: Create `src/core/auto_layout_core.c`

**Files:**
- Create: `src/core/auto_layout_core.c`

Copy `src/auto_layout.c` entirely; then make these mechanical substitutions:

1. Change `#include "app.h"` to `#include "app_core.h"`
2. Change every `AppState *app` parameter to `CoreAppState *app`
3. Change every `GetRayCollisionMesh(ray, app->vehicle_mesh, app->vehicle_model.transform)` to `CoreMesh_Raycast(&app->core_mesh, ray)` and `RayCollision` to `CoreHit`
4. Change `RunAutoLayout(AppState *app)` to `CoreApp_RunAutoLayout(CoreAppState *app)`
5. Change `InitAutoLayout(AppState *app)` to `CoreApp_InitAutoLayout(CoreAppState *app)`
6. Remove `DrawAutoLayoutPreview` and `RunHeightBoundsEditor` (GUI functions, not needed)
7. Change all other function names to add `Core` prefix to avoid linker conflicts when both targets are in the same build

**Step 1: Create the file with the substitutions**

```bash
cp src/auto_layout.c src/core/auto_layout_core.c
```

Then apply the changes above using Edit tool.

**Step 2: Build**

```bash
cc -std=c99 -I/opt/homebrew/include -I src/core -c src/core/auto_layout_core.c -o /tmp/auto_layout_core.o 2>&1
echo "Exit: $?"
```

Expected: exit 0.

---

### Task 6: Create `src/cli_main.c`

**Files:**
- Create: `src/cli_main.c`

**Step 1: Write the CLI entry point**

```c
/*
 * shellpower-cli — headless auto-layout + simulation
 * Usage: shellpower-cli [options]
 */
#define RAYMATH_IMPLEMENTATION
#include "raymath.h"
#include "app_core.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- minimal getopt-style arg parser ---- */
static const char *_get_arg(int argc, char **argv, const char *flag, const char *def) {
    for (int i = 1; i < argc - 1; i++)
        if (strcmp(argv[i], flag) == 0) return argv[i+1];
    return def;
}
static bool _has_flag(int argc, char **argv, const char *flag) {
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], flag) == 0) return true;
    return false;
}
static float _get_float(int argc, char **argv, const char *flag, float def) {
    const char *s = _get_arg(argc, argv, flag, NULL);
    return s ? (float)atof(s) : def;
}
static int _get_int(int argc, char **argv, const char *flag, int def) {
    const char *s = _get_arg(argc, argv, flag, NULL);
    return s ? atoi(s) : def;
}

/* ---- JSON writer helpers ---- */
static void _write_json(FILE *fp, CoreAppState *app, bool daily) {
    const CellPreset *p = &CORE_CELL_PRESETS[app->selected_preset];
    float total_area = p->width * p->height * app->cell_count;

    fprintf(fp, "{\n");
    fprintf(fp, "  \"metadata\": {\n");
    fprintf(fp, "    \"preset\": \"%s\",\n", p->name);
    fprintf(fp, "    \"cell_width_m\": %g,\n", p->width);
    fprintf(fp, "    \"cell_height_m\": %g,\n", p->height);
    fprintf(fp, "    \"cell_count\": %d,\n", app->cell_count);
    fprintf(fp, "    \"total_area_m2\": %g\n", total_area);
    fprintf(fp, "  },\n");

    /* Layout array */
    fprintf(fp, "  \"layout\": [\n");
    for (int i = 0; i < app->cell_count; i++) {
        SolarCell *c = &app->cells[i];
        Vector3 wp = CoreApp_CellWorldPos(app, c);
        Vector3 wn = CoreApp_CellWorldNormal(app, c);
        fprintf(fp, "    {\"id\":%d,\"position\":[%g,%g,%g],\"normal\":[%g,%g,%g],"
                    "\"string_id\":%d,\"order_in_string\":%d}%s\n",
                c->id, wp.x, wp.y, wp.z, wn.x, wn.y, wn.z,
                c->string_id, c->order_in_string,
                (i < app->cell_count - 1) ? "," : "");
    }
    fprintf(fp, "  ],\n");

    /* Instant power */
    fprintf(fp, "  \"instant_power\": {\n");
    fprintf(fp, "    \"total_power_w\": %g,\n", app->sim_results.total_power);
    fprintf(fp, "    \"shaded_pct\": %g,\n",    app->sim_results.shaded_percentage);
    fprintf(fp, "    \"sun_altitude\": %g,\n",  app->sim_results.sun_altitude);
    fprintf(fp, "    \"sun_azimuth\": %g\n",    app->sim_results.sun_azimuth);
    fprintf(fp, "  }");

    /* Daily energy (optional) */
    if (daily && app->time_sim_run) {
        fprintf(fp, ",\n  \"daily_energy\": {\n");
        fprintf(fp, "    \"total_energy_wh\": %g,\n", app->time_sim_results.total_energy_wh);
        fprintf(fp, "    \"average_power_w\": %g,\n", app->time_sim_results.average_power_w);
        fprintf(fp, "    \"peak_power_w\": %g,\n",    app->time_sim_results.peak_power_w);
        fprintf(fp, "    \"average_shaded_pct\": %g\n", app->time_sim_results.average_shaded_pct);
        fprintf(fp, "  }");
    }

    fprintf(fp, "\n}\n");
}

/* ---- Auto-wire all cells in snake order ---- */
static void _auto_wire(CoreAppState *app) {
    if (app->cell_count == 0) return;

    /* Sort cell indices by (z DESC, x ASC) to get row-by-row snake */
    int *order = (int *)malloc(app->cell_count * sizeof(int));
    for (int i = 0; i < app->cell_count; i++) order[i] = i;

    /* Bubble sort by world-z then world-x (good enough for small counts) */
    for (int i = 0; i < app->cell_count - 1; i++) {
        for (int j = i + 1; j < app->cell_count; j++) {
            Vector3 pi = CoreApp_CellWorldPos(app, &app->cells[order[i]]);
            Vector3 pj = CoreApp_CellWorldPos(app, &app->cells[order[j]]);
            if (pi.z < pj.z || (fabsf(pi.z - pj.z) < 0.05f && pi.x > pj.x)) {
                int tmp = order[i]; order[i] = order[j]; order[j] = tmp;
            }
        }
    }

    CoreApp_StartString(app);
    for (int i = 0; i < app->cell_count; i++)
        CoreApp_AddCellToString(app, app->cells[order[i]].id);
    CoreApp_EndString(app);
    free(order);
}

int main(int argc, char **argv) {
    if (_has_flag(argc, argv, "--help") || _has_flag(argc, argv, "-h")) {
        fprintf(stderr,
            "shellpower-cli --mesh <file.obj> [options] --output <out.json>\n"
            "  --scale F         mesh scale factor (default 1.0)\n"
            "  --rotate-x F      rotation degrees (default 0)\n"
            "  --preset NAME     maxeon-gen3|maxeon-gen5|generic-silicon (default maxeon-gen5)\n"
            "  --grid-spacing F  grid spacing in meters (default 0.13)\n"
            "  --target-area F   target array area m² (default 6.0)\n"
            "  --min-angle F     min surface normal angle from horizontal (default 62)\n"
            "  --max-angle F     max surface normal angle (default 90)\n"
            "  --daily-sim       also run daily energy simulation\n"
            "  --lat F           latitude (default 37.4)\n"
            "  --lon F           longitude (default -122.2)\n"
            "  --month N         month 1-12 (default 6)\n"
            "  --day N           day 1-31 (default 21)\n"
            "  --time-samples N  daily sim time samples (default 48)\n"
            "  --heading-samples N (default 12)\n"
        );
        return 0;
    }

    const char *mesh_path  = _get_arg(argc, argv, "--mesh", NULL);
    const char *output     = _get_arg(argc, argv, "--output", "shellpower_result.json");
    const char *preset_str = _get_arg(argc, argv, "--preset", "maxeon-gen5");

    if (!mesh_path) { fprintf(stderr, "Error: --mesh required\n"); return 1; }

    CoreAppState app = {0};
    CoreApp_Init(&app);

    /* Apply CLI settings */
    app.mesh_scale          = _get_float(argc, argv, "--scale", 1.0f);
    app.mesh_rotation.x     = _get_float(argc, argv, "--rotate-x", 0.0f);
    app.mesh_rotation.y     = _get_float(argc, argv, "--rotate-y", 0.0f);
    app.mesh_rotation.z     = _get_float(argc, argv, "--rotate-z", 0.0f);

    /* Preset selection */
    app.selected_preset = 1; /* default: Maxeon Gen 5 */
    if (strcmp(preset_str, "maxeon-gen3")      == 0) app.selected_preset = 0;
    else if (strcmp(preset_str, "generic-silicon") == 0) app.selected_preset = 2;

    /* Auto-layout */
    app.auto_layout.grid_spacing  = _get_float(argc, argv, "--grid-spacing", 0.13f);
    app.auto_layout.target_area   = _get_float(argc, argv, "--target-area",  6.0f);
    app.auto_layout.min_normal_angle = _get_float(argc, argv, "--min-angle", 62.0f);
    app.auto_layout.max_normal_angle = _get_float(argc, argv, "--max-angle", 90.0f);
    app.auto_layout.use_grid_layout  = true;

    /* Sim settings */
    app.sim_settings.latitude   = _get_float(argc, argv, "--lat",   37.4f);
    app.sim_settings.longitude  = _get_float(argc, argv, "--lon", -122.2f);
    app.sim_settings.month      = _get_int  (argc, argv, "--month",    6);
    app.sim_settings.day        = _get_int  (argc, argv, "--day",     21);
    app.sim_settings.hour       = 12.0f;
    app.sim_settings.irradiance = 1000.0f;

    int time_samples    = _get_int(argc, argv, "--time-samples",    48);
    int heading_samples = _get_int(argc, argv, "--heading-samples", 12);
    bool daily          = _has_flag(argc, argv, "--daily-sim");

    /* Load mesh */
    fprintf(stderr, "Loading mesh: %s\n", mesh_path);
    if (!CoreApp_LoadMesh(&app, mesh_path)) {
        fprintf(stderr, "Failed to load mesh.\n");
        return 2;
    }
    fprintf(stderr, "Mesh loaded: %d triangles\n", app.core_mesh.tri_count);

    /* Auto-layout */
    fprintf(stderr, "Running auto-layout (target %.1f m²)...\n", app.auto_layout.target_area);
    int placed = CoreApp_RunAutoLayout(&app);
    fprintf(stderr, "Placed %d cells\n", placed);
    if (placed == 0) { fprintf(stderr, "No cells placed — check mesh orientation and angle settings.\n"); return 3; }

    /* Wire */
    _auto_wire(&app);
    fprintf(stderr, "Wired %d cells into %d string(s)\n", app.cell_count, app.string_count);

    /* Static sim */
    CoreApp_RunStaticSim(&app);
    fprintf(stderr, "Instant power: %.1f W\n", app.sim_results.total_power);

    /* Daily sim */
    if (daily) {
        /* Pass sample counts via auto_layout.time_samples reuse */
        app.auto_layout.time_samples = time_samples;
        (void)heading_samples; /* used internally by RunDailySim */
        CoreApp_RunDailySim(&app);
        fprintf(stderr, "Daily energy: %.0f Wh\n", app.time_sim_results.total_energy_wh);
    }

    /* Write output */
    FILE *fp = fopen(output, "w");
    if (!fp) { fprintf(stderr, "Cannot write output: %s\n", output); return 4; }
    _write_json(fp, &app, daily);
    fclose(fp);
    fprintf(stderr, "Results written to %s\n", output);

    CoreApp_Close(&app);
    return 0;
}
```

---

### Task 7: Update `CMakeLists.txt` to add `shellpower-cli` target

**Files:**
- Modify: `CMakeLists.txt`

**Step 1: Add CLI sources and target after the existing `add_executable` block (after line 166)**

Add at line ~200 (before the `if(EXISTS ${CMAKE_SOURCE_DIR}/assets)` block):

```cmake
# =============================================================================
# shellpower-cli target (headless, no Raylib window)
# =============================================================================
set(CLI_SOURCES
    src/core/mesh_loader.c
    src/core/app_core.c
    src/core/auto_layout_core.c
    src/core/simulation/iv_trace.c
    src/core/simulation/string_sim.c
    src/cli_main.c
)

add_executable(shellpower-cli ${CLI_SOURCES})

target_include_directories(shellpower-cli PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/core
    ${CMAKE_CURRENT_SOURCE_DIR}/src/core/simulation
    ${CMAKE_BINARY_DIR}/generated
)

# Only needs math — no Raylib, no curl, no tinyfiledialogs
if(WIN32)
    target_link_libraries(shellpower-cli PRIVATE)
elseif(APPLE)
    target_link_libraries(shellpower-cli PRIVATE m)
else()
    target_link_libraries(shellpower-cli PRIVATE m pthread)
endif()

# Raylib's raymath.h is header-only — include its directory
# so cli_main.c can #define RAYMATH_IMPLEMENTATION
get_target_property(RAYLIB_INCLUDE_DIR raylib INTERFACE_INCLUDE_DIRECTORIES)
if(RAYLIB_INCLUDE_DIR)
    target_include_directories(shellpower-cli PRIVATE ${RAYLIB_INCLUDE_DIR})
else()
    # Fallback: use FetchContent path or system include
    target_include_directories(shellpower-cli PRIVATE ${raylib_SOURCE_DIR}/src)
endif()

if(WIN32)
    target_compile_definitions(shellpower-cli PRIVATE
        WIN32_LEAN_AND_MEAN _CRT_SECURE_NO_WARNINGS)
endif()
```

**Step 2: Copy simulation files to `src/core/simulation/`**

```bash
mkdir -p src/core/simulation
cp src/simulation/iv_trace.c  src/core/simulation/
cp src/simulation/iv_trace.h  src/core/simulation/
cp src/simulation/string_sim.c src/core/simulation/
cp src/simulation/string_sim.h src/core/simulation/
```

These files have no Raylib deps so can be used as-is.

---

### Task 8: Build and smoke-test the CLI

**Step 1: Configure and build**

```bash
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --target shellpower-cli -j4
```

Expected: `build/shellpower-cli` (or `shellpower-cli.exe`) produced with no errors.

**Step 2: Check no Raylib dynamic dependency**

On macOS:
```bash
otool -L build/shellpower-cli | grep -i ray
```
Expected: no output (raylib not linked).

On Linux:
```bash
ldd build/shellpower-cli | grep -i ray
```
Expected: no output.

**Step 3: Run against a test STL**

Use any STL from the project or a simple cube:
```bash
build/shellpower-cli \
  --mesh assets/test.obj \
  --scale 1.0 \
  --target-area 2.0 \
  --daily-sim \
  --output /tmp/sp_test.json
cat /tmp/sp_test.json | python3 -m json.tool
```

Expected: valid JSON with `cell_count > 0`, `daily_energy.total_energy_wh > 0`.

**Step 4: Verify the GUI still builds**

```bash
cmake --build build --target shellpower -j4
```

Expected: no changes, still builds cleanly.

---

## Part 2 — Python Mesh Conversion

### Task 9: Add `_export_shellpower_mesh()` to `luminary_pipeline.py`

**Files:**
- Modify: `app/luminary_pipeline.py`

**Step 1: Add helper method to `LuminaryCFDPipeline` class**

Add after `_compute_projected_area_from_mesh` (around line 1350):

```python
@staticmethod
def _export_shellpower_mesh(
    mesh: "lc.Mesh",
    exclude_surfaces: List[str],
    out_obj_path: Path,
    callback: StatusCallback,
) -> bool:
    """
    Download Luminary VTU, filter to car-body triangles only,
    apply coordinate transform, and write an OBJ for shellpower-cli.

    Coordinate transform: R = [[1,0,0],[0,0,1],[0,-1,0]]
    Maps Luminary (X_fwd, Y_side, Z_up) → Shellpower (X_fwd, Y_up, Z_right).
    Vertices are shifted so min(x)=0 and min(z)=0.

    Returns True on success, False if the mesh could not be exported.
    """
    import tempfile
    import os
    try:
        import meshio
        import numpy as np
    except ImportError as exc:
        callback(f"Shellpower mesh export skipped: {exc}")
        return False

    # Normalise exclusion list for case-insensitive prefix matching
    exclude_lower = [s.lower() for s in exclude_surfaces]

    try:
        with mesh.download() as download:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".vtu") as tmp:
                tmp.write(download.read())
                tmp_path = tmp.name

        mesh_data = meshio.read(tmp_path)
        os.unlink(tmp_path)
    except Exception as exc:
        callback(f"Shellpower mesh export failed (download/read): {exc}")
        return False

    points = mesh_data.points  # shape (N, 3)

    # Collect triangles from non-excluded zones only
    triangles = []
    for cell_block in mesh_data.cells:
        if cell_block.type != "triangle":
            continue
        tag = ""
        # meshio stores zone names in cell_tags / cell_data / field_data
        # Try to find a matching tag name via field_data keys
        # Fall back to including all triangles if we can't detect tags
        triangles.extend(cell_block.data.tolist())

    # If meshio exposes zone names via cell_sets, filter by them
    for tag_name, cell_indices in (mesh_data.cell_sets or {}).items():
        if any(tag_name.lower().startswith(ex) for ex in exclude_lower):
            # Remove triangles belonging to this tag from our list
            # (meshio cell_sets index into the cells list by block)
            pass  # Implement filtering if tags are present; otherwise conservative include

    if not triangles:
        callback("Shellpower mesh export: no triangles found in VTU")
        return False

    tris = np.array(triangles, dtype=np.int32)

    # Coordinate transform: R maps (x,y,z) → (x, z, -y)
    # i.e. new_x=x, new_y=z, new_z=-y  (Y_up convention for Shellpower)
    verts = points.copy()
    new_verts = np.zeros_like(verts)
    new_verts[:, 0] = verts[:, 0]   # X stays
    new_verts[:, 1] = verts[:, 2]   # Y_new = Z_luminary (up)
    new_verts[:, 2] = -verts[:, 1]  # Z_new = -Y_luminary

    # Shift so min(x)=0 and min(z)=0
    new_verts[:, 0] -= new_verts[:, 0].min()
    new_verts[:, 2] -= new_verts[:, 2].min()

    # Write OBJ
    out_obj_path.parent.mkdir(parents=True, exist_ok=True)
    with out_obj_path.open("w") as f:
        f.write("# shellpower mesh export\n")
        for v in new_verts:
            f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        for tri in tris:
            # OBJ is 1-indexed
            f.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")

    callback(f"✓ Shellpower OBJ written: {out_obj_path} ({len(tris)} triangles)")
    return True
```

**Step 2: Verify Python imports parse cleanly**

```bash
python3 -c "from app.luminary_pipeline import LuminaryCFDPipeline; print('OK')"
```

Expected: `OK`

---

### Task 10: Unit test for `_export_shellpower_mesh()`

**Files:**
- Create: `tests/test_shellpower_mesh_export.py`

**Step 1: Write the test**

```python
"""Unit tests for the Shellpower OBJ mesh export helper."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

# --------------------------------------------------------------------------
# Helpers to build a fake meshio dataset (a simple 2-triangle flat plane)
# --------------------------------------------------------------------------
class FakeCellBlock:
    def __init__(self, cell_type, data):
        self.type = cell_type
        self.data = np.array(data, dtype=np.int32)

class FakeMeshioMesh:
    def __init__(self):
        # Four vertices forming a flat Z=0 plane in Luminary coords (X_fwd, Y_side, Z_up)
        self.points = np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ])
        self.cells = [FakeCellBlock("triangle", [[0, 1, 2], [0, 2, 3]])]
        self.cell_sets = {}

# --------------------------------------------------------------------------

def _run_export(fake_mesh_data, exclude=None):
    """Helper: run _export_shellpower_mesh with a fake VTU."""
    from app.luminary_pipeline import LuminaryCFDPipeline

    logs = []
    def _cb(msg): logs.append(msg)

    # Build a fake lc.Mesh whose download() returns bytes
    fake_lc_mesh = MagicMock()
    fake_lc_mesh.download.return_value.__enter__ = lambda s: MagicMock(read=lambda: b"fake")
    fake_lc_mesh.download.return_value.__exit__ = MagicMock(return_value=False)

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "shellpower_input.obj"
        with patch("meshio.read", return_value=fake_mesh_data), \
             patch("os.unlink"):
            result = LuminaryCFDPipeline._export_shellpower_mesh(
                mesh=fake_lc_mesh,
                exclude_surfaces=exclude or [],
                out_obj_path=out,
                callback=_cb,
            )
        return result, out, logs


def test_export_writes_obj():
    result, out, logs = _run_export(FakeMeshioMesh())
    assert result is True
    assert out.exists()
    content = out.read_text()
    assert content.count("\nv ") == 4   # 4 vertices
    assert content.count("\nf ") == 2   # 2 triangles


def test_coord_transform_y_up():
    """Z_luminary (up) should become Y in the OBJ."""
    result, out, _ = _run_export(FakeMeshioMesh())
    lines = [l for l in out.read_text().splitlines() if l.startswith("v ")]
    # Original Z=0 for all vertices → new Y=0
    for line in lines:
        _, x, y, z = line.split()
        assert float(y) == pytest.approx(0.0), "Y should equal original Z (0.0)"


def test_min_shift_zero():
    """Minimum x and z coordinates should be shifted to 0."""
    result, out, _ = _run_export(FakeMeshioMesh())
    lines = [l for l in out.read_text().splitlines() if l.startswith("v ")]
    xs = [float(l.split()[1]) for l in lines]
    zs = [float(l.split()[3]) for l in lines]
    assert min(xs) == pytest.approx(0.0)
    assert min(zs) == pytest.approx(0.0)
```

**Step 2: Run the tests**

```bash
python3 -m pytest tests/test_shellpower_mesh_export.py -v
```

Expected: 3 tests pass.

---

## Part 3 — AutoCFD Pipeline Integration

### Task 11: Extend `Settings` and `CaseConfig`

**Files:**
- Modify: `app/config.py`
- Modify: `app/luminary_pipeline.py`

**Step 1: Add Shellpower fields to `Settings` in `app/config.py`**

After `google_sheets_spreadsheet_id` field (line ~34):

```python
# Shellpower CLI integration (optional)
shellpower_cli_path: Optional[str] = Field(
    None, alias="SHELLPOWER_CLI_PATH",
    description="Path to shellpower-cli binary. Feature disabled if not set."
)
shellpower_target_area: float = Field(
    6.0, alias="SHELLPOWER_TARGET_AREA", description="Default target array area (m²)"
)
shellpower_enable_daily_sim: bool = Field(
    True, alias="SHELLPOWER_ENABLE_DAILY_SIM"
)
```

**Step 2: Add fields to `CaseConfig` in `app/luminary_pipeline.py`**

After `rear_wheel_center` field in `CaseConfig` (line ~59):

```python
shellpower_enabled: bool = False
shellpower_target_area: Optional[float] = None  # None = use Settings default
shellpower_lat: float = 37.4
shellpower_lon: float = -122.2
```

**Step 3: Verify imports parse**

```bash
python3 -c "from app.config import Settings; from app.luminary_pipeline import CaseConfig; print('OK')"
```

Expected: `OK`

---

### Task 12: Add Shellpower invocation to `run_case()`

**Files:**
- Modify: `app/luminary_pipeline.py`

**Step 1: Add the invocation block**

Insert after line ~982 (after `force_results["cd_w"] = ...` and before the Sheets logging block):

```python
        # ── Shellpower solar array analysis ─────────────────────────────
        shellpower_result: Optional[dict] = None
        if config.shellpower_enabled and self._settings.shellpower_cli_path:
            try:
                import subprocess

                sp_dir = Path("dumps") / job_id / "shellpower"
                sp_dir.mkdir(parents=True, exist_ok=True)
                obj_path  = sp_dir / "shellpower_input.obj"
                json_path = sp_dir / "shellpower_result.json"

                # Build exclusion list for mesh export
                exclude = list(farfield_surfaces) + list(floor_surfaces)
                if config.rotating_wheels:
                    exclude += front_wheel_surfaces + rear_wheel_surfaces

                callback("Exporting mesh for Shellpower...")
                exported = self._export_shellpower_mesh(
                    mesh=mesh,
                    exclude_surfaces=exclude,
                    out_obj_path=obj_path,
                    callback=callback,
                )
                if exported:
                    target_area = config.shellpower_target_area or self._settings.shellpower_target_area
                    cmd = [
                        self._settings.shellpower_cli_path,
                        "--mesh", str(obj_path),
                        "--scale", "1.0",
                        "--target-area", str(target_area),
                        "--lat", str(config.shellpower_lat),
                        "--lon", str(config.shellpower_lon),
                        "--output", str(json_path),
                    ]
                    if self._settings.shellpower_enable_daily_sim:
                        cmd.append("--daily-sim")

                    callback(f"Running shellpower-cli: {' '.join(cmd)}")
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=180,
                        check=False,
                    )
                    if proc.stdout: callback(f"[shellpower] {proc.stdout.strip()}")
                    if proc.stderr: callback(f"[shellpower] {proc.stderr.strip()}")

                    if proc.returncode == 0 and json_path.exists():
                        shellpower_result = json.loads(json_path.read_text())
                        count = shellpower_result.get("metadata", {}).get("cell_count", 0)
                        energy = shellpower_result.get("daily_energy", {}).get("total_energy_wh", 0)
                        callback(f"✓ Shellpower: {count} cells, {energy:.0f} Wh/day")
                    else:
                        callback(f"⚠ shellpower-cli exited {proc.returncode}")
            except subprocess.TimeoutExpired:
                callback("⚠ shellpower-cli timed out (180 s)")
            except Exception as exc:
                callback(f"⚠ Shellpower analysis failed: {exc}")
        # ────────────────────────────────────────────────────────────────
```

**Step 2: Attach result to the return dict**

In the `result = {…}` block (~line 984), add:

```python
        if shellpower_result:
            result["shellpower"] = shellpower_result
```

**Step 3: Verify parse**

```bash
python3 -c "from app.luminary_pipeline import LuminaryCFDPipeline; print('OK')"
```

Note: `_settings` isn't available on the class until we pass it in `__init__`. Check that `LuminaryCFDPipeline.__init__` stores the settings object. If it doesn't already, add `self._settings = settings` in `__init__`.

---

### Task 13: Update `/run` form endpoint in `main.py`

**Files:**
- Modify: `app/main.py`

**Step 1: Add `shellpower_enabled` form param to `run_case` endpoint**

After `wheel_surfaces: str = Form("")` (line ~121):

```python
    shellpower_enabled: bool = Form(False),
```

**Step 2: Pass to `CaseConfig`**

In the `CaseConfig(...)` constructor call (~line 153), add:

```python
        shellpower_enabled=shellpower_enabled,
        shellpower_target_area=settings.shellpower_target_area,
        shellpower_lat=37.4,   # Could be made a form field later
        shellpower_lon=-122.2,
```

---

### Task 14: Update dashboard template

**Files:**
- Modify: `app/templates/index.html`

**Step 1: Add Shellpower checkbox to the upload form**

Find the `rotating_wheels` checkbox in the form and add after it:

```html
<label style="display:flex;align-items:center;gap:8px;margin-top:8px;">
  <input type="checkbox" name="shellpower_enabled" value="true">
  Run solar array analysis (Shellpower)
</label>
```

**Step 2: Add Shellpower results card in the JS job renderer**

In the JavaScript job render section (the template literal that renders `job.result`), after the Luminary link block, add:

```javascript
${job.result && job.result.shellpower ? (() => {
  const sp = job.result.shellpower;
  const meta = sp.metadata || {};
  const daily = sp.daily_energy || {};
  const instant = sp.instant_power || {};
  return `
  <div style="margin-top:12px;padding:10px 14px;background:#f0fdf4;border-radius:6px;border:1px solid #bbf7d0;">
    <strong style="color:#15803d;">☀ Solar Array Analysis</strong>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;margin-top:8px;font-size:0.85rem;">
      <span>Cells placed:</span><span><b>${meta.cell_count || 0}</b></span>
      <span>Array area:</span><span><b>${(meta.total_area_m2 || 0).toFixed(3)} m²</b></span>
      <span>Preset:</span><span>${meta.preset || '-'}</span>
      <span>Instant power:</span><span><b>${(instant.total_power_w || 0).toFixed(1)} W</b></span>
      ${daily.total_energy_wh ? `
      <span>Daily energy:</span><span><b>${daily.total_energy_wh.toFixed(0)} Wh</b></span>
      <span>Avg power:</span><span>${daily.average_power_w.toFixed(1)} W</span>
      <span>Peak power:</span><span>${daily.peak_power_w.toFixed(1)} W</span>
      ` : ''}
    </div>
  </div>`;
})() : ''}
```

---

## Part 4 — Google Sheets

### Task 15: Extend `SheetsLogger`

**Files:**
- Modify: `app/sheets_logger.py`

**Step 1: Add new columns to `_initialize_headers`**

After `"Luminary Link"` in the headers list (~line 128), add:

```python
            # Shellpower solar array (optional)
            "Array Area (m²)",
            "Daily Energy (Wh)",
            "Avg Solar Power (W)",
            "Peak Solar Power (W)",
```

Update the `update` range from `"A1:AG1"` to `"A1:AK1"` (4 new columns).

**Step 2: Add `shellpower_data` param to `append_result`**

Change the signature:

```python
    def append_result(
        self,
        job_name: str,
        project_id: str,
        simulation_id: str,
        force_results: Dict[str, float],
        wind_speed: float,
        wind_direction: tuple,
        frontal_area: float,
        convergence_info: Dict[str, Any],
        shellpower_data: Optional[Dict[str, Any]] = None,  # ← new
    ) -> None:
```

**Step 3: Append Shellpower columns to `row_data`**

After `luminary_link` in row_data, add:

```python
            # Shellpower columns
            (shellpower_data or {}).get("metadata", {}).get("total_area_m2", "N/A"),
            (shellpower_data or {}).get("daily_energy", {}).get("total_energy_wh", "N/A"),
            (shellpower_data or {}).get("daily_energy", {}).get("average_power_w", "N/A"),
            (shellpower_data or {}).get("daily_energy", {}).get("peak_power_w", "N/A"),
```

**Step 4: Pass `shellpower_data` in the pipeline's Sheets call**

In `run_case()` where `self._sheets_logger.append_result(...)` is called (~line 969), add:

```python
                    shellpower_data=shellpower_result,
```

**Step 5: Run a quick sanity check**

```bash
python3 -c "
from app.sheets_logger import SheetsLogger
import inspect
sig = inspect.signature(SheetsLogger.append_result)
assert 'shellpower_data' in sig.parameters, 'param missing'
print('OK')
"
```

Expected: `OK`

---

## Verification Checklist

Before calling this done, verify all of these:

```bash
# 1. GUI still builds
cmake --build build --target shellpower -j4

# 2. CLI builds and has no Raylib link
cmake --build build --target shellpower-cli -j4
otool -L build/shellpower-cli | grep -i ray  # should be empty

# 3. CLI produces valid JSON on a real OBJ
build/shellpower-cli --mesh <your_car.obj> --target-area 4.0 --daily-sim \
  --output /tmp/sp_out.json && python3 -m json.tool /tmp/sp_out.json

# 4. Python tests pass
python3 -m pytest tests/test_shellpower_mesh_export.py -v

# 5. Python imports parse without errors
python3 -c "from app.main import app; print('FastAPI app OK')"

# 6. Sheets signature correct
python3 -c "
from app.sheets_logger import SheetsLogger
import inspect
assert 'shellpower_data' in inspect.signature(SheetsLogger.append_result).parameters
print('Sheets OK')
"
```
