/*
 * app_core.c — Headless CLI application state implementation.
 *
 * Implements all functions declared in app_core.h.
 * No GUI, no Raylib window calls, no raygui.
 */

#include "app_core.h"
#include "mesh_loader.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * Include raymath after app_core.h so that the RL_VECTOR3_TYPE / RL_MATRIX_TYPE
 * guards are already defined and raymath.h skips the duplicate typedefs but
 * still provides all function implementations.
 */
#define RAYMATH_IMPLEMENTATION
#include "raymath.h"

#include "simulation/iv_trace.h"
#include "simulation/string_sim.h"

/*----------------------------------------------------------------------------
 * Cell Presets
 *--------------------------------------------------------------------------*/
const CellPreset CORE_CELL_PRESETS[] = {
    {"Maxeon Gen 3 (ME3)", 0.125f, 0.125f, 0.227f, 0.686f, 6.27f, 0.58f, 6.01f, 1.26f, 0.003f, 0.35f},
    {"Maxeon Gen 5",       0.125f, 0.125f, 0.240f, 0.700f, 6.50f, 0.60f, 6.24f, 1.25f, 0.003f, 0.35f},
    {"Maxeon Gen 7",       0.125f, 0.125f, 0.260f, 0.780f, 6.50f, 0.71f, 5.72f, 1.20f, 0.003f, 0.35f},
    {"Generic Silicon",    0.156f, 0.156f, 0.200f, 0.620f, 8.00f, 0.50f, 7.60f, 1.30f, 0.005f, 0.35f},
};
const int CORE_CELL_PRESET_COUNT = 4;

/*----------------------------------------------------------------------------
 * Internal helpers
 *--------------------------------------------------------------------------*/

static float CoreClampf(float value, float min_v, float max_v) {
    if (value < min_v) return min_v;
    if (value > max_v) return max_v;
    return value;
}

/* Generate a deterministic dummy color for a new string (no GUI dependency). */
static CoreColor CoreGenerateStringColor(int string_id) {
    /* Simple HSV-to-RGB with hue derived from string_id */
    float hue = (float)((string_id * 73) % 360);
    float sat = 0.75f;
    float val = 0.85f;

    float c = val * sat;
    float x = c * (1.0f - fabsf(fmodf(hue / 60.0f, 2.0f) - 1.0f));
    float m = val - c;

    float r, g, b;
    if      (hue < 60)  { r = c; g = x; b = 0; }
    else if (hue < 120) { r = x; g = c; b = 0; }
    else if (hue < 180) { r = 0; g = c; b = x; }
    else if (hue < 240) { r = 0; g = x; b = c; }
    else if (hue < 300) { r = x; g = 0; b = c; }
    else                { r = c; g = 0; b = x; }

    CoreColor col;
    col.r = (unsigned char)((r + m) * 255);
    col.g = (unsigned char)((g + m) * 255);
    col.b = (unsigned char)((b + m) * 255);
    col.a = 230;
    return col;
}

/*----------------------------------------------------------------------------
 * Lifecycle
 *--------------------------------------------------------------------------*/

void CoreApp_Init(CoreAppState *app) {
    memset(app, 0, sizeof(CoreAppState));

    /* Mesh */
    app->mesh_loaded   = false;
    app->mesh_scale    = 1.0f;
    app->mesh_rotation = (Vector3){0.0f, 0.0f, 0.0f};

    /* Cells */
    app->cell_count      = 0;
    app->next_cell_id    = 0;
    app->selected_preset = 1;   /* Maxeon Gen 5 */

    /* Strings */
    app->string_count    = 0;
    app->next_string_id  = 0;
    app->active_string_id = -1;

    /* Bypass diodes */
    app->bypass_diode_count   = 0;
    app->next_bypass_diode_id = 0;

    /* Simulation defaults */
    app->sim_settings.latitude   = 37.4f;
    app->sim_settings.longitude  = -122.2f;
    app->sim_settings.year       = 2024;
    app->sim_settings.month      = 6;
    app->sim_settings.day        = 21;
    app->sim_settings.hour       = 12.0f;
    app->sim_settings.irradiance = 1000.0f;

    app->sim_run      = false;
    app->time_sim_run = false;

    /* Auto-layout and snap */
    CoreApp_InitAutoLayout(app);
    CoreApp_InitSnap(app);

    snprintf(app->status_msg, sizeof(app->status_msg),
             "CoreApp initialized. Load a mesh to begin.");
}

void CoreApp_Close(CoreAppState *app) {
    CoreMesh_Free(&app->core_mesh);
}

/*----------------------------------------------------------------------------
 * Mesh
 *--------------------------------------------------------------------------*/

bool CoreApp_LoadMesh(CoreAppState *app, const char *path) {
    /* Free any existing mesh */
    if (app->mesh_loaded) {
        CoreMesh_Free(&app->core_mesh);
        app->mesh_loaded = false;
    }

    CoreMesh m = CoreMesh_Load(path);
    if (m.tri_count == 0) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Error: Failed to load mesh from '%s'", path);
        return false;
    }

    app->core_mesh = m;
    strncpy(app->mesh_path, path, MAX_PATH_LENGTH - 1);
    app->mesh_path[MAX_PATH_LENGTH - 1] = '\0';
    app->mesh_loaded = true;

    /* Bake transform (scale + rotation) into vertex data */
    CoreApp_ApplyTransform(app);

    /* Reset cells */
    CoreApp_ClearCells(app);

    /* Reset snap grid */
    app->snap.grid_origin    = (Vector3){0.0f, 0.0f, 0.0f};
    app->snap.grid_normal    = (Vector3){0.0f, 1.0f, 0.0f};
    app->snap.grid_configured = false;

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Loaded mesh: %s (%d triangles)", path, app->core_mesh.tri_count);
    return true;
}

void CoreApp_ApplyTransform(CoreAppState *app) {
    if (!app->mesh_loaded)
        return;

    float  scale = app->mesh_scale;
    Vector3 rot  = app->mesh_rotation;

    /* Build transform: scale then rotate (X, Y, Z Euler order) */
    Matrix scaleM    = MatrixScale(scale, scale, scale);
    Matrix rotX      = MatrixRotateX(rot.x * DEG2RAD);
    Matrix rotY      = MatrixRotateY(rot.y * DEG2RAD);
    Matrix rotZ      = MatrixRotateZ(rot.z * DEG2RAD);
    Matrix rotation  = MatrixMultiply(MatrixMultiply(rotX, rotY), rotZ);
    Matrix transform = MatrixMultiply(scaleM, rotation);

    CoreMesh_Transform(&app->core_mesh, transform);
    app->mesh_bounds = CoreMesh_BoundingBox(&app->core_mesh);
}

/*----------------------------------------------------------------------------
 * Cells
 *--------------------------------------------------------------------------*/

int CoreApp_PlaceCell(CoreAppState *app, Vector3 world_pos, Vector3 world_normal) {
    if (!app->mesh_loaded) {
        snprintf(app->status_msg, sizeof(app->status_msg), "No mesh loaded");
        return -1;
    }

    if (app->cell_count >= MAX_CELLS) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Maximum cell count reached");
        return -1;
    }

    /* Reject cells on surfaces that face too steeply downward */
    if (world_normal.y < MIN_UPWARD_NORMAL) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Surface too steep for cell placement");
        return -1;
    }

    /* Minimum distance check */
    const CellPreset *preset = &CORE_CELL_PRESETS[app->selected_preset];
    float min_dist = fmaxf(preset->width, preset->height) * MIN_CELL_DISTANCE_FACTOR;

    for (int i = 0; i < app->cell_count; i++) {
        float dist = Vector3Distance(world_pos, app->cells[i].local_position);
        if (dist < min_dist) {
            snprintf(app->status_msg, sizeof(app->status_msg),
                     "Too close to existing cell");
            return -1;
        }
    }

    /* Compute tangent */
    Vector3 ref = {0.0f, 0.0f, 1.0f};
    Vector3 tangent = Vector3CrossProduct(ref, world_normal);
    if (Vector3Length(tangent) < 0.001f) {
        ref = (Vector3){1.0f, 0.0f, 0.0f};
        tangent = Vector3CrossProduct(ref, world_normal);
    }
    tangent = Vector3Normalize(tangent);

    /* Store in local_position / local_normal
     * Since transforms are baked in (CoreMesh_Transform applied the world
     * transform), world coords == local coords here. */
    SolarCell *cell        = &app->cells[app->cell_count];
    cell->id               = app->next_cell_id++;
    cell->local_position   = world_pos;
    cell->local_normal     = Vector3Normalize(world_normal);
    cell->local_tangent    = tangent;
    cell->string_id        = -1;
    cell->order_in_string  = -1;
    cell->has_bypass_diode = false;
    cell->is_shaded        = false;
    cell->is_bypassed      = false;
    cell->power_output     = 0.0f;
    cell->current_output   = 0.0f;
    cell->voltage_output   = 0.0f;
    cell->curvature_deg    = 0.0f;
    cell->over_curvature_limit = false;

    app->cell_count++;

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Placed cell #%d", cell->id);
    return cell->id;
}

void CoreApp_ClearCells(CoreAppState *app) {
    memset(app->cells, 0, sizeof(app->cells));
    app->cell_count   = 0;
    app->next_cell_id = 0;
    CoreApp_ClearWiring(app);
}

/* Transforms are baked in, so world == local. */
Vector3 CoreApp_CellWorldPos(CoreAppState *app, SolarCell *cell) {
    (void)app;
    return cell->local_position;
}

Vector3 CoreApp_CellWorldNormal(CoreAppState *app, SolarCell *cell) {
    (void)app;
    return cell->local_normal;
}

/*----------------------------------------------------------------------------
 * Wiring
 *--------------------------------------------------------------------------*/

int CoreApp_StartString(CoreAppState *app) {
    if (app->string_count >= MAX_STRINGS) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Maximum string count reached");
        return -1;
    }

    CellString *str  = &app->strings[app->string_count];
    str->id          = app->next_string_id++;
    str->color       = CoreGenerateStringColor(str->id);
    str->cell_count  = 0;
    str->total_power = 0.0f;

    app->active_string_id = str->id;
    app->string_count++;

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Started string #%d", str->id);
    return str->id;
}

void CoreApp_AddCellToString(CoreAppState *app, int cell_id) {
    /* Find the cell */
    SolarCell *cell = NULL;
    for (int i = 0; i < app->cell_count; i++) {
        if (app->cells[i].id == cell_id) {
            cell = &app->cells[i];
            break;
        }
    }
    if (!cell) return;

    /* Reject already-wired cells */
    if (cell->string_id >= 0) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Cell already wired to string #%d", cell->string_id);
        return;
    }

    /* Start a new string if none is active */
    if (app->active_string_id < 0) {
        if (CoreApp_StartString(app) < 0)
            return;
    }

    /* Find the active string */
    CellString *str = NULL;
    for (int i = 0; i < app->string_count; i++) {
        if (app->strings[i].id == app->active_string_id) {
            str = &app->strings[i];
            break;
        }
    }
    if (!str) return;

    if (str->cell_count >= MAX_CELLS_PER_STRING) {
        snprintf(app->status_msg, sizeof(app->status_msg), "String is full");
        return;
    }

    cell->string_id       = str->id;
    cell->order_in_string = str->cell_count;
    str->cell_ids[str->cell_count++] = cell_id;

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Added cell #%d to string #%d (%d cells)",
             cell_id, str->id, str->cell_count);
}

void CoreApp_EndString(CoreAppState *app) {
    if (app->active_string_id < 0) {
        snprintf(app->status_msg, sizeof(app->status_msg), "No active string");
        return;
    }

    /* Remove the string if it is empty */
    for (int i = 0; i < app->string_count; i++) {
        if (app->strings[i].id == app->active_string_id) {
            if (app->strings[i].cell_count == 0) {
                for (int j = i; j < app->string_count - 1; j++) {
                    app->strings[j] = app->strings[j + 1];
                }
                app->string_count--;
            }
            break;
        }
    }

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Ended string #%d", app->active_string_id);
    app->active_string_id = -1;
}

void CoreApp_ClearWiring(CoreAppState *app) {
    /* Unwire all cells */
    for (int i = 0; i < app->cell_count; i++) {
        app->cells[i].string_id       = -1;
        app->cells[i].order_in_string = -1;
    }

    app->string_count     = 0;
    app->active_string_id = -1;
    app->sim_run          = false;

    snprintf(app->status_msg, sizeof(app->status_msg), "Cleared all wiring");
}

/*----------------------------------------------------------------------------
 * Snap
 *--------------------------------------------------------------------------*/

void CoreApp_InitSnap(CoreAppState *app) {
    app->snap.grid_snap_enabled  = false;
    app->snap.grid_size          = 0.126f;
    app->snap.align_to_surface   = true;
    app->snap.show_grid          = false;
    app->snap.grid_origin        = (Vector3){0.0f, 0.0f, 0.0f};
    app->snap.grid_normal        = (Vector3){0.0f, 1.0f, 0.0f};
    app->snap.grid_rotation      = 0.0f;
    app->snap.setting_grid_origin = false;
    app->snap.grid_configured    = false;
}

/*----------------------------------------------------------------------------
 * Sun position
 *--------------------------------------------------------------------------*/

Vector3 CoreApp_CalcSunDirection(SimSettings *s, float *out_alt, float *out_az) {
    /* Simplified NOAA solar position algorithm (ported from CalculateSunDirection) */
    float lat = s->latitude * DEG2RAD;
    lat = CoreClampf(lat, -89.0f * DEG2RAD, 89.0f * DEG2RAD);

    /* Day of year (approximate) */
    int doy = (s->month - 1) * 30 + s->day;
    if (doy < 1)   doy = 1;
    if (doy > 365) doy = 365;

    float gamma = 2.0f * PI / 365.0f * (float)(doy - 1);

    /* Equation of time (minutes) */
    float eqtime = 229.18f * (0.000075f
        + 0.001868f * cosf(gamma) - 0.032077f * sinf(gamma)
        - 0.014615f * cosf(2.0f * gamma) - 0.040849f * sinf(2.0f * gamma));

    /* Solar declination (radians) */
    float decl = 0.006918f
        - 0.399912f * cosf(gamma)    + 0.070257f * sinf(gamma)
        - 0.006758f * cosf(2.0f * gamma) + 0.000907f * sinf(2.0f * gamma)
        - 0.002697f * cosf(3.0f * gamma) + 0.001480f * sinf(3.0f * gamma);

    /* Timezone offset from longitude */
    float timezone_offset = roundf(s->longitude / 15.0f);

    /* Solar time */
    float longitude_correction  = 4.0f * (s->longitude - timezone_offset * 15.0f);
    float solar_time_minutes    = s->hour * 60.0f + longitude_correction + eqtime;

    /* Hour angle */
    float ha     = (solar_time_minutes / 4.0f) - 180.0f;
    float ha_rad = ha * DEG2RAD;

    /* Zenith */
    float cos_zen = sinf(lat) * sinf(decl) + cosf(lat) * cosf(decl) * cosf(ha_rad);
    cos_zen = CoreClampf(cos_zen, -1.0f, 1.0f);

    float zenith   = acosf(cos_zen);
    float altitude = 90.0f - zenith * RAD2DEG;

    /* Azimuth */
    float sin_zen = sinf(zenith);
    float azimuth = 0.0f;

    if (fabsf(sin_zen) > 0.001f) {
        float cos_az = (sinf(decl) - sinf(lat) * cos_zen) / (cosf(lat) * sin_zen);
        cos_az = CoreClampf(cos_az, -1.0f, 1.0f);
        azimuth = acosf(cos_az) * RAD2DEG;
        if (ha > 0.0f)
            azimuth = 360.0f - azimuth;
    } else {
        azimuth = 180.0f;
    }

    if (out_alt) *out_alt = altitude;
    if (out_az)  *out_az  = azimuth;

    if (altitude <= 0.0f) {
        return (Vector3){0.0f, -1.0f, 0.0f};
    }

    float alt_rad = altitude * DEG2RAD;
    float az_rad  = azimuth  * DEG2RAD;

    Vector3 dir = {
        cosf(alt_rad) * sinf(az_rad),
        sinf(alt_rad),
        -cosf(alt_rad) * cosf(az_rad)
    };
    return Vector3Normalize(dir);
}

/*----------------------------------------------------------------------------
 * Internal shading check
 *--------------------------------------------------------------------------*/

static bool CoreCheckCellShading(CoreAppState *app, SolarCell *cell, Vector3 sun_dir) {
    if (!app->mesh_loaded) return false;

    Vector3 pos    = CoreApp_CellWorldPos(app, cell);
    Vector3 normal = CoreApp_CellWorldNormal(app, cell);

    CoreRay ray;
    ray.origin    = Vector3Add(pos, Vector3Scale(normal, 0.01f));
    ray.direction = sun_dir;

    CoreHit hit = CoreMesh_Raycast(&app->core_mesh, ray);
    return hit.hit;
}

/*----------------------------------------------------------------------------
 * Internal power calculation (unwired cells)
 *--------------------------------------------------------------------------*/

static float CoreCalcCellPower(CoreAppState *app, SolarCell *cell,
                                Vector3 sun_dir, const CellPreset *preset,
                                float irradiance) {
    if (cell->is_shaded) return 0.0f;

    Vector3 world_normal = CoreApp_CellWorldNormal(app, cell);
    float cos_angle = Vector3DotProduct(world_normal, sun_dir);
    if (cos_angle < 0.0f) cos_angle = 0.0f;

    float area  = preset->width * preset->height;
    return irradiance * area * cos_angle * preset->efficiency;
}

/*----------------------------------------------------------------------------
 * Static simulation
 *--------------------------------------------------------------------------*/

void CoreApp_RunStaticSim(CoreAppState *app) {
    if (app->cell_count == 0) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "No cells to simulate");
        return;
    }

    const CellPreset *preset = &CORE_CELL_PRESETS[app->selected_preset];

    /* Sun position */
    app->sim_results.sun_direction =
        CoreApp_CalcSunDirection(&app->sim_settings,
                                 &app->sim_results.sun_altitude,
                                 &app->sim_results.sun_azimuth);
    app->sim_results.is_daytime = (app->sim_results.sun_altitude > 0.0f);

    /* Reset aggregate results */
    app->sim_results.total_power  = 0.0f;
    app->sim_results.shaded_count = 0;

    for (int s = 0; s < app->string_count; s++) {
        app->strings[s].total_power    = 0.0f;
        app->strings[s].string_current = 0.0f;
        app->strings[s].string_voltage = 0.0f;
        app->strings[s].bypassed_count = 0;
        app->strings[s].power_ideal    = 0.0f;
    }

    /* First pass: per-cell shading and irradiance */
    for (int i = 0; i < app->cell_count; i++) {
        SolarCell *cell   = &app->cells[i];
        cell->is_bypassed = false;

        if (!app->sim_results.is_daytime) {
            cell->is_shaded      = true;
            cell->power_output   = 0.0f;
            cell->current_output = 0.0f;
            cell->voltage_output = 0.0f;
        } else {
            cell->is_shaded = CoreCheckCellShading(app, cell,
                                                   app->sim_results.sun_direction);
            Vector3 wn        = CoreApp_CellWorldNormal(app, cell);
            float cos_angle   = Vector3DotProduct(wn, app->sim_results.sun_direction);
            if (cos_angle < 0.0f) cos_angle = 0.0f;

            if (cell->is_shaded || cos_angle <= 0.0f) {
                cell->current_output = 0.0f;
                cell->voltage_output = 0.0f;
                cell->power_output   = 0.0f;
            } else {
                float irr_ratio      = (app->sim_settings.irradiance / 1000.0f) * cos_angle;
                cell->current_output = preset->isc * irr_ratio;
                cell->voltage_output = preset->vmp;
                cell->power_output   = cell->current_output * cell->voltage_output;
            }
        }

        if (cell->is_shaded)
            app->sim_results.shaded_count++;
    }

    /* String power with IV-trace / series constraints */
    float total_string_power  = 0.0f;
    float total_unwired_power = 0.0f;

    for (int s = 0; s < app->string_count; s++) {
        CellString *str = &app->strings[s];
        if (str->cell_count == 0) continue;

        IVTrace cell_traces[MAX_CELLS_PER_STRING];
        int     cell_indices[MAX_CELLS_PER_STRING];
        int     order_to_idx[MAX_CELLS_PER_STRING];
        int     string_cell_count = 0;

        /* Build IV traces ordered by wiring order */
        for (int order = 0; order < str->cell_count; order++) {
            for (int c = 0; c < app->cell_count; c++) {
                if (app->cells[c].string_id       == str->id &&
                    app->cells[c].order_in_string == order) {
                    SolarCell *cell = &app->cells[c];

                    float irr_ratio = 0.0f;
                    if (!cell->is_shaded) {
                        Vector3 wn      = CoreApp_CellWorldNormal(app, cell);
                        float cos_angle = Vector3DotProduct(wn, app->sim_results.sun_direction);
                        if (cos_angle < 0.0f) cos_angle = 0.0f;
                        irr_ratio = (app->sim_settings.irradiance / 1000.0f) * cos_angle;
                    }

                    IVTrace_CreateCellTrace(&cell_traces[string_cell_count],
                                            preset->voc, preset->isc, preset->n_ideal,
                                            preset->series_r, irr_ratio);

                    order_to_idx[order]             = string_cell_count;
                    cell_indices[string_cell_count] = c;
                    string_cell_count++;
                    break;
                }
            }
        }

        /* Build segment bypass array */
        SegmentBypass segments[STRING_SIM_MAX_SEGMENTS];
        int n_segments = 0;

        for (int d = 0; d < app->bypass_diode_count && n_segments < STRING_SIM_MAX_SEGMENTS; d++) {
            BypassDiode *diode = &app->bypass_diodes[d];
            if (diode->string_id != str->id) continue;

            int start_order = -1, end_order = -1;
            for (int c = 0; c < app->cell_count; c++) {
                if (app->cells[c].id == diode->start_cell_id)
                    start_order = app->cells[c].order_in_string;
                if (app->cells[c].id == diode->end_cell_id)
                    end_order = app->cells[c].order_in_string;
            }

            if (start_order >= 0 && end_order >= 0) {
                int min_order = (start_order < end_order) ? start_order : end_order;
                int max_order = (start_order > end_order) ? start_order : end_order;

                if (min_order < str->cell_count && max_order < str->cell_count) {
                    segments[n_segments].start_idx = order_to_idx[min_order];
                    segments[n_segments].end_idx   = order_to_idx[max_order];
                    segments[n_segments].v_drop    = preset->bypass_v_drop;
                    n_segments++;
                }
            }
        }

        StringSimResult sim_result;
        bool segment_bypassed[STRING_SIM_MAX_SEGMENTS];
        memset(segment_bypassed, 0, sizeof(segment_bypassed));

        if (n_segments > 0) {
            StringSim_CalcStringIVSegments(cell_traces, string_cell_count,
                                           segments, n_segments,
                                           &sim_result, segment_bypassed);
        } else {
            bool has_bypass[MAX_CELLS_PER_STRING];
            for (int i = 0; i < string_cell_count; i++) {
                has_bypass[i] = app->cells[cell_indices[i]].has_bypass_diode;
            }
            StringSim_CalcStringIV(cell_traces, string_cell_count,
                                   preset->bypass_v_drop, has_bypass, &sim_result);
        }

        str->total_power    = sim_result.power_out;
        str->string_current = sim_result.current;
        str->string_voltage = sim_result.voltage;
        str->bypassed_count = sim_result.cells_bypassed;

        /* Per-cell bypass state */
        int seg_sizes[STRING_SIM_MAX_SEGMENTS];
        for (int seg = 0; seg < n_segments; seg++) {
            seg_sizes[seg] = segments[seg].end_idx - segments[seg].start_idx + 1;
        }

        for (int i = 0; i < string_cell_count; i++) {
            int c = cell_indices[i];
            SolarCell *cell = &app->cells[c];
            bool cell_bypassed = false;

            if (n_segments > 0) {
                int smallest_bypassed_size = string_cell_count + 1;
                int smallest_active_size   = string_cell_count + 1;

                for (int seg = 0; seg < n_segments; seg++) {
                    if (i >= segments[seg].start_idx && i <= segments[seg].end_idx) {
                        if (segment_bypassed[seg]) {
                            if (seg_sizes[seg] < smallest_bypassed_size)
                                smallest_bypassed_size = seg_sizes[seg];
                        } else {
                            if (seg_sizes[seg] < smallest_active_size)
                                smallest_active_size = seg_sizes[seg];
                        }
                    }
                }

                if (smallest_bypassed_size < string_cell_count + 1 &&
                    smallest_active_size >= smallest_bypassed_size) {
                    cell_bypassed = true;
                }
            }

            if (!cell_bypassed && n_segments == 0 && cell->has_bypass_diode) {
                if (sim_result.current >= cell_traces[i].Isc)
                    cell_bypassed = true;
            }

            if (cell_bypassed) {
                cell->is_bypassed    = true;
                cell->voltage_output = -preset->bypass_v_drop;
                cell->power_output   = sim_result.current * cell->voltage_output;
            } else {
                cell->is_bypassed    = false;
                cell->voltage_output = IVTrace_InterpV(&cell_traces[i], sim_result.current);
                cell->power_output   = sim_result.current * cell->voltage_output;
            }
        }

        str->power_ideal     = (float)string_cell_count * preset->vmp * preset->imp;
        total_string_power  += sim_result.power_out;
    }

    /* Unwired cells */
    for (int i = 0; i < app->cell_count; i++) {
        if (app->cells[i].string_id < 0) {
            app->cells[i].power_output =
                CoreCalcCellPower(app, &app->cells[i],
                                  app->sim_results.sun_direction,
                                  preset, app->sim_settings.irradiance);
            total_unwired_power += app->cells[i].power_output;
        }
    }

    app->sim_results.total_power = total_string_power + total_unwired_power;
    app->sim_results.shaded_percentage =
        (app->cell_count > 0)
            ? (100.0f * (float)app->sim_results.shaded_count / (float)app->cell_count)
            : 0.0f;

    app->sim_run = true;

    if (app->string_count > 0) {
        int total_bypassed = 0;
        for (int s = 0; s < app->string_count; s++)
            total_bypassed += app->strings[s].bypassed_count;
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Simulation: %.1fW (%.1f%% shaded, %d bypassed)",
                 app->sim_results.total_power,
                 app->sim_results.shaded_percentage,
                 total_bypassed);
    } else {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Simulation: %.1fW total, %.1f%% shaded",
                 app->sim_results.total_power,
                 app->sim_results.shaded_percentage);
    }
}

/*----------------------------------------------------------------------------
 * Daily (time) simulation
 *--------------------------------------------------------------------------*/

void CoreApp_RunDailySim(CoreAppState *app) {
    if (app->cell_count == 0 || !app->mesh_loaded) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "No cells or mesh to simulate");
        return;
    }

    const CellPreset *preset = &CORE_CELL_PRESETS[app->selected_preset];

    const int   TIME_SAMPLES    = 48;
    const int   HEADING_SAMPLES = (app->auto_layout.heading_samples > 0)
                                      ? app->auto_layout.heading_samples : 36;
    const float START_HOUR      = app->auto_layout.sim_start_hour;
    const float END_HOUR        = app->auto_layout.sim_end_hour;
    const float DURATION        = END_HOUR - START_HOUR;
    const float dt_hours        = DURATION / (float)(TIME_SAMPLES - 1);
    const float MIN_HEADING     = app->auto_layout.min_heading_deg;
    const float MAX_HEADING     = app->auto_layout.max_heading_deg;

    float *cell_energy   = (float *)calloc((size_t)app->cell_count,   sizeof(float));
    float *string_energy = (float *)calloc((size_t)(app->string_count > 0 ? app->string_count : 1), sizeof(float));
    if (!cell_energy) return;

    float total_energy   = 0.0f;
    float peak_power     = 0.0f;
    float peak_altitude  = 0.0f;
    int   total_samples  = 0;
    int   shaded_samples = 0;

    for (int h = 0; h < 24; h++)
        app->time_sim_results.energy_by_hour[h] = 0.0f;

    for (int ti = 0; ti < TIME_SAMPLES; ti++) {
        float hour = START_HOUR + (DURATION * (float)ti / (float)(TIME_SAMPLES - 1));

        app->sim_settings.hour = hour;
        float altitude, azimuth;
        Vector3 sun_dir =
            CoreApp_CalcSunDirection(&app->sim_settings, &altitude, &azimuth);

        float effective_irradiance = 0.0f;
        if (altitude > 0.0f) {
            float sin_alt          = sinf(altitude * DEG2RAD);
            float air_mass         = 1.0f / fmaxf(sin_alt, 0.01f);
            float atm_factor       = powf(0.85f, powf(air_mass, 0.678f));
            effective_irradiance   = app->sim_settings.irradiance * atm_factor;
        }

        app->sim_results.sun_altitude = altitude;
        app->sim_results.sun_azimuth  = azimuth;
        app->sim_results.is_daytime   = (altitude > 0.0f);

        if (altitude <= 0.0f)
            continue;

        float  time_step_power_sum = 0.0f;
        float *cell_power_ts = (float *)calloc((size_t)app->cell_count, sizeof(float));

        for (int hi = 0; hi < HEADING_SAMPLES; hi++) {
            float t_h = (HEADING_SAMPLES > 1) ? (float)hi / (float)(HEADING_SAMPLES - 1) : 0.0f;
            float heading_deg = MIN_HEADING + t_h * (MAX_HEADING - MIN_HEADING);
            float heading_rad = heading_deg * DEG2RAD;

            /* Rotate sun direction relative to vehicle heading */
            Vector3 rotated_sun = {
                sun_dir.x * cosf(-heading_rad) - sun_dir.z * sinf(-heading_rad),
                sun_dir.y,
                sun_dir.x * sinf(-heading_rad) + sun_dir.z * cosf(-heading_rad)
            };

            app->sim_results.sun_direction = rotated_sun;

            float instant_power = 0.0f;

            /* First pass: shading and irradiance per cell */
            float *cell_irr = (float *)calloc((size_t)app->cell_count, sizeof(float));
            for (int c = 0; c < app->cell_count; c++) {
                SolarCell *cell = &app->cells[c];
                Vector3 pos     = CoreApp_CellWorldPos(app, cell);
                Vector3 norm    = CoreApp_CellWorldNormal(app, cell);

                total_samples++;
                float facing = Vector3DotProduct(norm, rotated_sun);

                if (facing <= 0.0f) {
                    shaded_samples++;
                    cell->is_shaded      = true;
                    cell->current_output = 0.0f;
                    cell_irr[c]          = 0.0f;
                    continue;
                }

                /* Occlusion check */
                CoreRay ray;
                ray.origin    = Vector3Add(pos, Vector3Scale(norm, 0.01f));
                ray.direction = rotated_sun;
                CoreHit hit   = CoreMesh_Raycast(&app->core_mesh, ray);

                if (hit.hit && hit.distance > 0.02f) {
                    shaded_samples++;
                    cell->is_shaded      = true;
                    cell->current_output = 0.0f;
                    cell_irr[c]          = 0.0f;
                    continue;
                }

                cell->is_shaded      = false;
                cell_irr[c]          = (effective_irradiance / 1000.0f) * facing;
                cell->current_output = preset->isc * cell_irr[c];
            }

            /* Second pass: string IV traces */
            for (int s = 0; s < app->string_count; s++) {
                CellString *str = &app->strings[s];
                if (str->cell_count == 0) continue;

                IVTrace cell_traces[MAX_CELLS_PER_STRING];
                bool    has_bypass[MAX_CELLS_PER_STRING];
                int     cell_indices[MAX_CELLS_PER_STRING];
                int     string_cell_count = 0;

                for (int c = 0; c < app->cell_count && string_cell_count < str->cell_count; c++) {
                    if (app->cells[c].string_id == str->id) {
                        IVTrace_CreateCellTrace(&cell_traces[string_cell_count],
                                                preset->voc, preset->isc, preset->n_ideal,
                                                preset->series_r, cell_irr[c]);
                        has_bypass[string_cell_count]    = app->cells[c].has_bypass_diode;
                        cell_indices[string_cell_count]  = c;
                        string_cell_count++;
                    }
                }

                StringSimResult sim_result;
                StringSim_CalcStringIV(cell_traces, string_cell_count,
                                       preset->bypass_v_drop, has_bypass, &sim_result);

                instant_power += sim_result.power_out;

                for (int i = 0; i < string_cell_count; i++) {
                    int c = cell_indices[i];
                    if (sim_result.current >= cell_traces[i].Isc && has_bypass[i]) {
                        app->cells[c].power_output =
                            sim_result.current * (-preset->bypass_v_drop);
                    } else {
                        float v = IVTrace_InterpV(&cell_traces[i], sim_result.current);
                        app->cells[c].power_output = sim_result.current * v;
                    }
                    if (cell_power_ts) cell_power_ts[c] += app->cells[c].power_output;
                }
            }

            /* Third pass: unwired cells */
            for (int c = 0; c < app->cell_count; c++) {
                if (app->cells[c].string_id < 0 && !app->cells[c].is_shaded) {
                    float area    = preset->width * preset->height;
                    float power_w = cell_irr[c] * 1000.0f * area * preset->efficiency;
                    instant_power += power_w;
                    app->cells[c].power_output = power_w;
                    if (cell_power_ts) cell_power_ts[c] += power_w;
                }
            }

            free(cell_irr);

            if (instant_power > peak_power) {
                peak_power    = instant_power;
                peak_altitude = altitude;
            }

            time_step_power_sum += instant_power;
        }

        /* Average over all headings */
        float avg_power_ts = time_step_power_sum / (float)HEADING_SAMPLES;
        float energy_ts    = avg_power_ts * dt_hours;
        total_energy      += energy_ts;

        int hour_bucket = (int)hour;
        if (hour_bucket >= 0 && hour_bucket < 24)
            app->time_sim_results.energy_by_hour[hour_bucket] += energy_ts;

        if (cell_power_ts) {
            for (int c = 0; c < app->cell_count; c++) {
                float avg_cell_power  = cell_power_ts[c] / (float)HEADING_SAMPLES;
                float cell_energy_step = avg_cell_power * dt_hours;
                cell_energy[c]        += cell_energy_step;

                SolarCell *cell = &app->cells[c];
                if (cell->string_id >= 0 && string_energy) {
                    for (int s = 0; s < app->string_count; s++) {
                        if (app->strings[s].id == cell->string_id) {
                            string_energy[s] += cell_energy_step;
                            break;
                        }
                    }
                }
            }
            free(cell_power_ts);
        }
    }

    /* Finalize */
    float daylight_hours = DURATION;

    for (int i = 0; i < app->cell_count; i++) {
        app->cells[i].power_output = cell_energy[i] / daylight_hours;

        float theoretical_max =
            preset->width * preset->height * preset->efficiency
            * app->sim_settings.irradiance * daylight_hours * 0.5f;
        app->cells[i].is_shaded = (cell_energy[i] < theoretical_max * 0.3f);
    }

    for (int s = 0; s < app->string_count; s++) {
        app->strings[s].total_energy_wh = string_energy[s];
        app->strings[s].total_power     = string_energy[s] / daylight_hours;
    }

    app->time_sim_results.total_energy_wh      = total_energy;
    app->time_sim_results.average_power_w      = total_energy / daylight_hours;
    app->time_sim_results.peak_power_w         = peak_power;
    app->time_sim_results.sun_altitude_at_peak = peak_altitude;
    app->time_sim_results.average_shaded_pct =
        (total_samples > 0)
            ? (100.0f * (float)shaded_samples / (float)total_samples)
            : 0.0f;

    app->sim_results.total_power       = app->time_sim_results.average_power_w;
    app->sim_results.shaded_percentage = app->time_sim_results.average_shaded_pct;

    app->sim_results.shaded_count = 0;
    for (int i = 0; i < app->cell_count; i++) {
        if (app->cells[i].is_shaded)
            app->sim_results.shaded_count++;
    }

    /* Reset sun to noon */
    app->sim_settings.hour = 12.0f;
    app->sim_results.sun_direction =
        CoreApp_CalcSunDirection(&app->sim_settings,
                                 &app->sim_results.sun_altitude,
                                 &app->sim_results.sun_azimuth);
    app->sim_results.is_daytime = true;

    app->sim_run      = true;
    app->time_sim_run = true;

    free(cell_energy);
    free(string_energy);

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Daily: %.1f Wh total, %.1f W avg, %.1f W peak",
             total_energy, total_energy / daylight_hours, peak_power);
}
