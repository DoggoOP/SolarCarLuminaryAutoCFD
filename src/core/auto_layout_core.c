/*
 * auto_layout_core.c — Headless port of src/auto_layout.c for CoreAppState.
 *
 * Implements CoreApp_InitAutoLayout and CoreApp_RunAutoLayout.
 * No GUI, no Raylib window/model/color calls.
 */

#include "app_core.h"
#include "mesh_loader.h"
#include <stdlib.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "raymath.h"

/*----------------------------------------------------------------------------
 * Implementation-specific constants
 *--------------------------------------------------------------------------*/
#define MAX_CANDIDATES    10000
#define MAX_HEIGHT_SAMPLES 5000

/*----------------------------------------------------------------------------
 * Local types
 *--------------------------------------------------------------------------*/

/* Candidate position for auto-layout (mirrors LayoutCandidate in app.h) */
typedef struct {
    Vector3 position;
    Vector3 normal;
    float   occlusion_score; /* 0 = no occlusion, 1 = always occluded */
    bool    valid;
} LayoutCandidate;

/*----------------------------------------------------------------------------
 * Local helpers
 *--------------------------------------------------------------------------*/

static float CoreAL_Clampf(float value, float min_v, float max_v) {
    if (value < min_v) return min_v;
    if (value > max_v) return max_v;
    return value;
}

/* Find a cell at a given position within a distance threshold.
 * Returns cell id if found, -1 otherwise. */
static int CoreAL_FindCellAtPosition(CoreAppState *app, Vector3 pos, float threshold) {
    for (int i = 0; i < app->cell_count; i++) {
        if (Vector3Distance(pos, app->cells[i].local_position) < threshold) {
            return app->cells[i].id;
        }
    }
    return -1;
}

/*----------------------------------------------------------------------------
 * Static helpers (mirroring the same-named functions in auto_layout.c)
 *--------------------------------------------------------------------------*/

static bool IsPointOnMesh(CoreAppState *app, Vector3 position, float tolerance) {
    if (!app->mesh_loaded)
        return false;

    CoreRay ray;
    ray.origin    = (Vector3){position.x, app->mesh_bounds.max.y + 1.0f, position.z};
    ray.direction = (Vector3){0, -1, 0};

    CoreHit hit = CoreMesh_Raycast(&app->core_mesh, ray);

    if (!hit.hit)
        return false;

    float heightDiff = fabsf(hit.point.y - position.y);
    return heightDiff < tolerance;
}

static bool IsCellFootprintValid(CoreAppState *app, Vector3 position, Vector3 normal,
                                 float cellWidth, float cellHeight) {
    if (!app->mesh_loaded)
        return false;

    /* Calculate cell corner directions */
    Vector3 right;
    Vector3 ref = {0, 0, 1};
    right = Vector3CrossProduct(ref, normal);
    if (Vector3Length(right) < 0.001f) {
        ref   = (Vector3){1, 0, 0};
        right = Vector3CrossProduct(ref, normal);
    }
    right = Vector3Normalize(right);

    Vector3 forward = Vector3Normalize(Vector3CrossProduct(normal, right));

    /* Scale to half cell size */
    Vector3 halfRight   = Vector3Scale(right,   cellWidth  / 2.0f);
    Vector3 halfForward = Vector3Scale(forward,  cellHeight / 2.0f);

    /* Check corners and edge midpoints (9 points for better coverage) */
    Vector3 checkPoints[9];
    checkPoints[0] = position;
    checkPoints[1] = Vector3Add(position, Vector3Add(halfRight, halfForward));
    checkPoints[2] = Vector3Add(position, Vector3Add(Vector3Negate(halfRight), halfForward));
    checkPoints[3] = Vector3Add(position, Vector3Add(halfRight, Vector3Negate(halfForward)));
    checkPoints[4] = Vector3Add(position, Vector3Add(Vector3Negate(halfRight), Vector3Negate(halfForward)));
    checkPoints[5] = Vector3Add(position, halfRight);
    checkPoints[6] = Vector3Add(position, Vector3Negate(halfRight));
    checkPoints[7] = Vector3Add(position, halfForward);
    checkPoints[8] = Vector3Add(position, Vector3Negate(halfForward));

    float tolerance = 0.05f;
    float cos_curvature_limit = -1.0f;
    if (app->auto_layout.surface_threshold > 0.0f &&
        app->auto_layout.surface_threshold < 180.0f) {
        cos_curvature_limit =
            cosf(app->auto_layout.surface_threshold * DEG2RAD);
    }

    for (int i = 0; i < 9; i++) {
        Vector3 checkPos = checkPoints[i];

        /* Check that this point is on the mesh */
        CoreRay rayDown;
        rayDown.origin    = (Vector3){checkPos.x, app->mesh_bounds.max.y + 1.0f, checkPos.z};
        rayDown.direction = (Vector3){0, -1, 0};

        CoreHit hitDown = CoreMesh_Raycast(&app->core_mesh, rayDown);

        if (!hitDown.hit) {
            return false;
        }

        float expectedY = position.y;
        float surfaceY  = hitDown.point.y;

        if (!app->auto_layout.ignore_curvature_limit &&
            fabsf(surfaceY - expectedY) > tolerance * 2.0f) {
            return false;
        }

        float normalDot = Vector3DotProduct(normal, hitDown.normal);
        if (!app->auto_layout.ignore_curvature_limit &&
            cos_curvature_limit > -0.999f && normalDot < cos_curvature_limit) {
            return false;
        }

        /* Check for mesh geometry above this point */
        CoreRay rayUp;
        rayUp.origin    = Vector3Add(checkPos, (Vector3){0, 0.01f, 0});
        rayUp.direction = (Vector3){0, 1, 0};

        CoreHit hitUp = CoreMesh_Raycast(&app->core_mesh, rayUp);

        float clearance_required = 0.05f;
        if (hitUp.hit && hitUp.distance < clearance_required) {
            return false;
        }
    }

    return true;
}

static float CoreAL_ComputeCurvature(CoreAppState *app, Vector3 position, Vector3 normal,
                                     float cellWidth, float cellHeight) {
    if (!app->mesh_loaded)
        return 0.0f;

    Vector3 right;
    Vector3 ref = {0.0f, 0.0f, 1.0f};
    right = Vector3CrossProduct(ref, normal);
    if (Vector3Length(right) < 0.001f) {
        ref = (Vector3){1.0f, 0.0f, 0.0f};
        right = Vector3CrossProduct(ref, normal);
    }
    right = Vector3Normalize(right);
    Vector3 forward = Vector3Normalize(Vector3CrossProduct(normal, right));

    Vector3 halfRight   = Vector3Scale(right, cellWidth / 2.0f);
    Vector3 halfForward = Vector3Scale(forward, cellHeight / 2.0f);

    Vector3 checkPoints[9];
    checkPoints[0] = position;
    checkPoints[1] = Vector3Add(position, Vector3Add(halfRight, halfForward));
    checkPoints[2] = Vector3Add(position, Vector3Add(Vector3Negate(halfRight), halfForward));
    checkPoints[3] = Vector3Add(position, Vector3Add(halfRight, Vector3Negate(halfForward)));
    checkPoints[4] = Vector3Add(position, Vector3Add(Vector3Negate(halfRight), Vector3Negate(halfForward)));
    checkPoints[5] = Vector3Add(position, halfRight);
    checkPoints[6] = Vector3Add(position, Vector3Negate(halfRight));
    checkPoints[7] = Vector3Add(position, halfForward);
    checkPoints[8] = Vector3Add(position, Vector3Negate(halfForward));

    float max_angle = 0.0f;
    for (int i = 0; i < 9; i++) {
        Vector3 checkPos = checkPoints[i];
        CoreRay rayDown;
        rayDown.origin    = (Vector3){checkPos.x, app->mesh_bounds.max.y + 1.0f, checkPos.z};
        rayDown.direction = (Vector3){0.0f, -1.0f, 0.0f};
        CoreHit hitDown   = CoreMesh_Raycast(&app->core_mesh, rayDown);
        if (!hitDown.hit)
            continue;

        float dot = Vector3DotProduct(normal, hitDown.normal);
        dot = CoreAL_Clampf(dot, -1.0f, 1.0f);
        float angle = acosf(dot) * RAD2DEG;
        if (angle > max_angle)
            max_angle = angle;
    }
    return max_angle;
}

static bool IsValidSurface(CoreAppState *app, Vector3 position, Vector3 normal) {
    float angle_from_vertical   = acosf(CoreAL_Clampf(normal.y, -1.0f, 1.0f)) * RAD2DEG;
    float angle_from_horizontal = 90.0f - angle_from_vertical;

    if (angle_from_horizontal < app->auto_layout.min_normal_angle ||
        angle_from_horizontal > app->auto_layout.max_normal_angle) {
        return false;
    }

    if (position.y < 0.01f)
        return false;

    if (app->auto_layout.use_height_constraint) {
        if (position.y < app->auto_layout.min_height ||
            position.y > app->auto_layout.max_height) {
            return false;
        }
    }

    const CellPreset *preset = &CORE_CELL_PRESETS[app->selected_preset];
    if (!IsCellFootprintValid(app, position, normal, preset->width, preset->height)) {
        return false;
    }

    return true;
}

static float CalculateOcclusionScore(CoreAppState *app, Vector3 position, Vector3 normal) {
    if (!app->mesh_loaded)
        return 0.0f;

    int occluded_count = 0;
    int total_samples  = 0;

    SimSettings original        = app->sim_settings;
    int         heading_samples = app->auto_layout.heading_samples;
    float       min_h           = app->auto_layout.min_heading_deg;
    float       max_h           = app->auto_layout.max_heading_deg;
    float       start_hour      = app->auto_layout.sim_start_hour;
    float       end_hour        = app->auto_layout.sim_end_hour;
    int         n_time          = app->auto_layout.time_samples;

    for (int heading_idx = 0; heading_idx < heading_samples; heading_idx++) {
        float t_h = (heading_samples > 1) ? (float)heading_idx / (heading_samples - 1) : 0.0f;
        float heading_angle = min_h + t_h * (max_h - min_h);
        float heading_rad   = heading_angle * DEG2RAD;

        for (int hour_idx = 0; hour_idx < n_time; hour_idx++) {
            float t_t = (n_time > 1) ? (float)hour_idx / (n_time - 1) : 0.0f;
            float hour = start_hour + t_t * (end_hour - start_hour);
            app->sim_settings.hour = hour;

            float   altitude, azimuth;
            Vector3 sun_dir = CoreApp_CalcSunDirection(&app->sim_settings, &altitude, &azimuth);

            if (altitude <= 0)
                continue;

            total_samples++;

            Vector3 rotated_sun_dir = {
                sun_dir.x * cosf(-heading_rad) - sun_dir.z * sinf(-heading_rad),
                sun_dir.y,
                sun_dir.x * sinf(-heading_rad) + sun_dir.z * cosf(-heading_rad)
            };

            float facing = Vector3DotProduct(normal, rotated_sun_dir);
            if (facing <= 0) {
                occluded_count++;
                continue;
            }

            CoreRay ray;
            ray.origin    = Vector3Add(position, Vector3Scale(normal, 0.01f));
            ray.direction = rotated_sun_dir;

            CoreHit hit = CoreMesh_Raycast(&app->core_mesh, ray);
            if (hit.hit && hit.distance > 0.02f) {
                occluded_count++;
            }
        }
    }

    app->sim_settings = original;
    return (total_samples > 0) ? (float)occluded_count / total_samples : 1.0f;
}

/*----------------------------------------------------------------------------
 * AutoDetectHeightRange — ported from auto_layout.c using CoreMesh
 *--------------------------------------------------------------------------*/

static void AutoDetectHeightRange(CoreAppState *app) {
    if (!app->mesh_loaded)
        return;

    float *vertices    = app->core_mesh.vertices;
    int   *indices     = app->core_mesh.indices;
    int    triCount    = app->core_mesh.tri_count;
    float  tolerance   = app->auto_layout.height_tolerance;

    float *heights    = (float *)malloc(MAX_HEIGHT_SAMPLES * sizeof(float));
    int    heightCount = 0;

    int step = (triCount > MAX_HEIGHT_SAMPLES) ? triCount / MAX_HEIGHT_SAMPLES : 1;

    for (int i = 0; i < triCount && heightCount < MAX_HEIGHT_SAMPLES; i += step) {
        int idx0, idx1, idx2;
        if (indices) {
            idx0 = indices[i * 3 + 0];
            idx1 = indices[i * 3 + 1];
            idx2 = indices[i * 3 + 2];
        } else {
            idx0 = i * 3 + 0;
            idx1 = i * 3 + 1;
            idx2 = i * 3 + 2;
        }

        /* Vertices are already in world space (transform baked in by CoreMesh_Transform) */
        Vector3 v0 = {vertices[idx0 * 3], vertices[idx0 * 3 + 1], vertices[idx0 * 3 + 2]};
        Vector3 v1 = {vertices[idx1 * 3], vertices[idx1 * 3 + 1], vertices[idx1 * 3 + 2]};
        Vector3 v2 = {vertices[idx2 * 3], vertices[idx2 * 3 + 1], vertices[idx2 * 3 + 2]};

        Vector3 edge1  = Vector3Subtract(v1, v0);
        Vector3 edge2  = Vector3Subtract(v2, v0);
        Vector3 normal = Vector3Normalize(Vector3CrossProduct(edge1, edge2));

        if (normal.y < MIN_UPWARD_NORMAL)
            continue;

        float center_y = (v0.y + v1.y + v2.y) / 3.0f;
        heights[heightCount++] = center_y;
    }

    if (heightCount == 0) {
        free(heights);
        return;
    }

    /* Sort heights (insertion sort for clarity; dataset is bounded) */
    for (int i = 0; i < heightCount - 1; i++) {
        for (int j = i + 1; j < heightCount; j++) {
            if (heights[j] < heights[i]) {
                float temp = heights[i];
                heights[i] = heights[j];
                heights[j] = temp;
            }
        }
    }

    /* Sliding window to find best height range */
    int   bestCount = 0;
    float bestMinY  = heights[0];
    float bestMaxY  = heights[0] + tolerance;

    for (int i = 0; i < heightCount; i++) {
        float windowMin = heights[i];
        float windowMax = windowMin + tolerance;

        int count = 0;
        for (int j = i; j < heightCount && heights[j] <= windowMax; j++) {
            count++;
        }

        if (count > bestCount) {
            bestCount = count;
            bestMinY  = windowMin;
            bestMaxY  = windowMax;
        }
    }

    app->auto_layout.min_height = bestMinY;
    app->auto_layout.max_height = bestMaxY;

    free(heights);

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Auto-detected height: %.2f - %.2f m (%d surfaces)",
             bestMinY, bestMaxY, bestCount);
}

/*----------------------------------------------------------------------------
 * CoreApp_InitAutoLayout
 *--------------------------------------------------------------------------*/

void CoreApp_InitAutoLayout(CoreAppState *app) {
    app->auto_layout.target_area           = 6.0f;
    app->auto_layout.min_normal_angle      = 62.0f;
    app->auto_layout.max_normal_angle      = 90.0f;
    app->auto_layout.surface_threshold     = 30.0f;
    app->auto_layout.time_samples          = 12;
    app->auto_layout.sim_start_hour        = 8.0f;
    app->auto_layout.sim_end_hour          = 18.0f;
    app->auto_layout.heading_samples       = 10;
    app->auto_layout.min_heading_deg       = 0.0f;
    app->auto_layout.max_heading_deg       = 360.0f;
    app->auto_layout.optimize_occlusion    = true;
    app->auto_layout.preview_surface       = false;
    app->auto_layout.use_height_constraint = true;
    app->auto_layout.auto_detect_height    = true;
    app->auto_layout.height_tolerance      = 0.3f;
    app->auto_layout.min_height            = 0.0f;
    app->auto_layout.max_height            = 10.0f;
    app->auto_layout.use_grid_layout       = true;
    app->auto_layout.grid_spacing          = 0.0f;
    app->auto_layout.edge_margin           = 0.035f;
    app->auto_layout.ignore_curvature_limit = false;
    app->auto_layout_running               = false;
    app->auto_layout_progress              = 0;
}

/*----------------------------------------------------------------------------
 * CoreApp_RunAutoLayout — the real implementation (replaces stub)
 *--------------------------------------------------------------------------*/

int CoreApp_RunAutoLayout(CoreAppState *app) {
    if (!app->mesh_loaded) {
        snprintf(app->status_msg, sizeof(app->status_msg), "No mesh loaded");
        return 0;
    }

    if (!app->snap.grid_snap_enabled) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Auto-layout requires grid snap: enable Grid Snap first");
        return 0;
    }
    if (!app->snap.grid_configured) {
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Auto-layout requires grid setup: click Set Grid Origin first");
        return 0;
    }

    if (app->auto_layout.use_height_constraint && app->auto_layout.auto_detect_height) {
        AutoDetectHeightRange(app);
    }

    app->auto_layout_running  = true;
    app->auto_layout_progress = 0;

    const CellPreset *preset    = &CORE_CELL_PRESETS[app->selected_preset];
    float             cell_area = preset->width * preset->height;
    int               target_cells = (int)(app->auto_layout.target_area / cell_area);

    if (target_cells > MAX_CELLS - app->cell_count) {
        target_cells = MAX_CELLS - app->cell_count;
    }

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Auto-layout: finding %d cell positions...", target_cells);

    LayoutCandidate *candidates    = (LayoutCandidate *)malloc(MAX_CANDIDATES * sizeof(LayoutCandidate));
    int              candidate_count = 0;

    float grid_spacing = app->snap.grid_size;
    if (grid_spacing <= 0) {
        free(candidates);
        app->auto_layout_running  = false;
        app->auto_layout_progress = 0;
        snprintf(app->status_msg, sizeof(app->status_msg),
                 "Auto-layout requires a positive snap grid size");
        return 0;
    }

    /* Auto-layout follows the user-defined snap grid basis. */
    Vector3 grid_origin = app->snap.grid_origin;
    Vector3 grid_normal = Vector3Normalize(app->snap.grid_normal);
    if (Vector3Length(grid_normal) < 0.001f) {
        grid_normal = (Vector3){0, 1, 0};
    }

    Vector3 tangent1, tangent2;
    Vector3 ref = (Vector3){0, 0, 1};
    if (fabsf(Vector3DotProduct(grid_normal, ref)) > 0.9f) {
        ref = (Vector3){1, 0, 0};
    }
    tangent1 = Vector3Normalize(Vector3CrossProduct(ref, grid_normal));
    tangent2 = Vector3Normalize(Vector3CrossProduct(grid_normal, tangent1));

    float   rotRad = app->snap.grid_rotation * DEG2RAD;
    float   cosR   = cosf(rotRad);
    float   sinR   = sinf(rotRad);
    Vector3 rotT1  = Vector3Add(Vector3Scale(tangent1,  cosR), Vector3Scale(tangent2, sinR));
    Vector3 rotT2  = Vector3Add(Vector3Scale(tangent1, -sinR), Vector3Scale(tangent2, cosR));
    tangent1 = rotT1;
    tangent2 = rotT2;

    Vector3 boundsCorners[8] = {
        {app->mesh_bounds.min.x, app->mesh_bounds.min.y, app->mesh_bounds.min.z},
        {app->mesh_bounds.max.x, app->mesh_bounds.min.y, app->mesh_bounds.min.z},
        {app->mesh_bounds.min.x, app->mesh_bounds.max.y, app->mesh_bounds.min.z},
        {app->mesh_bounds.max.x, app->mesh_bounds.max.y, app->mesh_bounds.min.z},
        {app->mesh_bounds.min.x, app->mesh_bounds.min.y, app->mesh_bounds.max.z},
        {app->mesh_bounds.max.x, app->mesh_bounds.min.y, app->mesh_bounds.max.z},
        {app->mesh_bounds.min.x, app->mesh_bounds.max.y, app->mesh_bounds.max.z},
        {app->mesh_bounds.max.x, app->mesh_bounds.max.y, app->mesh_bounds.max.z}
    };

    float minU = 1e9f, maxU = -1e9f;
    float minV = 1e9f, maxV = -1e9f;
    for (int i = 0; i < 8; i++) {
        Vector3 rel = Vector3Subtract(boundsCorners[i], grid_origin);
        float   u   = Vector3DotProduct(rel, tangent1);
        float   v   = Vector3DotProduct(rel, tangent2);
        if (u < minU) minU = u;
        if (u > maxU) maxU = u;
        if (v < minV) minV = v;
        if (v > maxV) maxV = v;
    }

    float edge_margin = fmaxf(app->auto_layout.edge_margin, 0.0f);
    if (edge_margin > 0.0f) {
        float spanU = maxU - minU;
        float spanV = maxV - minV;
        if (spanU > edge_margin * 2.0f) {
            minU += edge_margin;
            maxU -= edge_margin;
        }
        if (spanV > edge_margin * 2.0f) {
            minV += edge_margin;
            maxV -= edge_margin;
        }
    }

    int uStart = (int)floorf(minU / grid_spacing) - 1;
    int uEnd   = (int)ceilf(maxU  / grid_spacing) + 1;
    int vStart = (int)floorf(minV / grid_spacing) - 1;
    int vEnd   = (int)ceilf(maxV  / grid_spacing) + 1;

    int gridU = (uEnd - uStart) + 1;
    int gridV = (vEnd - vStart) + 1;
    snprintf(app->status_msg, sizeof(app->status_msg),
             "Auto-layout: scanning %dx%d grid...", gridU, gridV);

    for (int gu = uStart; gu <= uEnd && candidate_count < MAX_CANDIDATES; gu++) {
        for (int gv = vStart; gv <= vEnd && candidate_count < MAX_CANDIDATES; gv++) {
            float   u         = ((float)gu + 0.5f) * grid_spacing;
            float   v         = ((float)gv + 0.5f) * grid_spacing;
            Vector3 gridPoint = grid_origin;
            gridPoint = Vector3Add(gridPoint, Vector3Scale(tangent1, u));
            gridPoint = Vector3Add(gridPoint, Vector3Scale(tangent2, v));

            CoreRay ray;
            ray.origin    = (Vector3){gridPoint.x, app->mesh_bounds.max.y + 1.0f, gridPoint.z};
            ray.direction = (Vector3){0, -1, 0};

            CoreHit hit = CoreMesh_Raycast(&app->core_mesh, ray);
            if (!hit.hit)
                continue;

            Vector3 position = hit.point;
            Vector3 normal   = hit.normal;

            if (!IsValidSurface(app, position, normal))
                continue;

            /* Avoid stacking on an existing cell while preserving user-defined spacing. */
            if (CoreAL_FindCellAtPosition(app, position, 0.005f) >= 0) {
                continue;
            }

            candidates[candidate_count].position       = position;
            candidates[candidate_count].normal         = normal;
            candidates[candidate_count].occlusion_score = 0.0f;
            candidates[candidate_count].valid          = true;
            candidate_count++;
        }
        app->auto_layout_progress = 30;
    }

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Auto-layout: scoring %d candidates...", candidate_count);

    if (app->auto_layout.optimize_occlusion && candidate_count > 0) {
        for (int i = 0; i < candidate_count; i++) {
            candidates[i].occlusion_score =
                CalculateOcclusionScore(app, candidates[i].position, candidates[i].normal);
            app->auto_layout_progress = 30 + (i * 50) / candidate_count;
        }

        /* Sort by occlusion score (lowest first) */
        for (int i = 0; i < candidate_count - 1; i++) {
            for (int j = i + 1; j < candidate_count; j++) {
                if (candidates[j].occlusion_score < candidates[i].occlusion_score) {
                    LayoutCandidate temp = candidates[i];
                    candidates[i]        = candidates[j];
                    candidates[j]        = temp;
                }
            }
        }
    }

    /* Place cells directly on valid snap-grid positions. */
    int placed = 0;
    for (int i = 0; i < candidate_count && placed < target_cells; i++) {
        int id = CoreApp_PlaceCell(app, candidates[i].position, candidates[i].normal);
        if (id >= 0) {
            placed++;
            if (app->cell_count > 0) {
                SolarCell *cell = &app->cells[app->cell_count - 1];
                float curvature = CoreAL_ComputeCurvature(app, candidates[i].position,
                                                          candidates[i].normal,
                                                          preset->width, preset->height);
                cell->curvature_deg = curvature;
                float limit = app->auto_layout.surface_threshold;
                cell->over_curvature_limit =
                    (limit > 0.0f && curvature > limit + 1e-3f);
            }
        }
        if (target_cells > 0) {
            app->auto_layout_progress = 80 + (placed * 20) / target_cells;
        }
    }

    free(candidates);

    app->auto_layout_running  = false;
    app->auto_layout_progress = 100;

    snprintf(app->status_msg, sizeof(app->status_msg),
             "Auto-layout: placed %d cells (%.2f m²)", placed, placed * cell_area);

    return placed;
}
