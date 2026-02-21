/*
 * cli_main.c — Shellpower CLI entry point.
 *
 * Build (compile-check only, no link):
 *   cc -std=c99 -Isrc/core -Isrc \
 *      -I build/_deps/raylib-src/src \
 *      -c src/cli_main.c -o /tmp/cli_main.o
 *
 * Exit codes:
 *   0  success
 *   1  missing required argument
 *   2  mesh load failed
 *   3  no cells placed after auto-layout
 *   4  output file write failed
 */

#include "core/app_core.h"
#include "core/mesh_loader.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*---------------------------------------------------------------------------
 * Helpers
 *--------------------------------------------------------------------------*/

static void print_usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s --mesh <path> [options]\n"
        "\n"
        "Required:\n"
        "  --mesh <path>           Path to OBJ or STL mesh file\n"
        "\n"
        "Options:\n"
        "  --output <path>         Output JSON path (default: shellpower_result.json)\n"
        "  --scale <float>         Mesh scale factor (default: 1.0)\n"
        "  --rotate-x <float>      X-axis rotation in degrees (default: 0)\n"
        "  --rotate-y <float>      Y-axis rotation in degrees (default: 0)\n"
        "  --rotate-z <float>      Z-axis rotation in degrees (default: 0)\n"
        "  --preset <name>         Cell preset: maxeon-gen3 | maxeon-gen5 | maxeon-gen7 | generic-silicon\n"
        "                          (default: maxeon-gen7)\n"
        "  --grid-spacing <float>  Auto-layout grid spacing in m (default: 0.13)\n"
        "  --target-area <float>   Auto-layout target area in m^2 (default: 6.0)\n"
        "  --min-angle <float>     Min surface angle from horizontal (default: 62)\n"
        "  --max-angle <float>     Max surface angle from horizontal (default: 90)\n"
        "  --no-occlusion-opt      Disable occlusion-based placement scoring (place cells\n"
        "                          in spatial scan order instead of best-sun-first)\n"
        "  --daily-sim             Run daily energy simulation\n"
        "  --lat <float>           Latitude in degrees (default: 37.4)\n"
        "  --lon <float>           Longitude in degrees (default: -122.2)\n"
        "  --month <int>           Month 1-12 (default: 6)\n"
        "  --day <int>             Day 1-31 (default: 21)\n"
        "  --time-samples <int>    Time samples for daily sim (default: 48)\n"
        "  --heading-samples <int> Heading samples for daily sim (default: 12)\n"
        "  -h, --help              Show this help\n",
        prog);
}

/* Safe string-to-float; returns def on parse failure. */
static float parse_float(const char *s, float def) {
    if (!s || s[0] == '\0') return def;
    char *end = NULL;
    float v = (float)strtod(s, &end);
    if (end == s) return def;
    return v;
}

/* Safe string-to-int; returns def on parse failure. */
static int parse_int(const char *s, int def) {
    if (!s || s[0] == '\0') return def;
    char *end = NULL;
    long v = strtol(s, &end, 10);
    if (end == s) return def;
    return (int)v;
}

/*
 * Map --preset name to CORE_CELL_PRESETS index.
 * Returns -1 if the name is unrecognised.
 */
static int preset_index_from_name(const char *name) {
    if (!name) return -1;
    if (strcmp(name, "maxeon-gen3") == 0)      return 0;
    if (strcmp(name, "maxeon-gen5") == 0)      return 1;
    if (strcmp(name, "maxeon-gen7") == 0)      return 2;
    if (strcmp(name, "generic-silicon") == 0)  return 3;
    return -1;
}

/*---------------------------------------------------------------------------
 * Comparator for snake-pattern sort.
 *
 * Primary key  : world-Z descending  (front-to-back rows)
 * Secondary key: world-X ascending   (left-to-right within row)
 *
 * The comparison data is stored in a file-scope array that the comparator
 * references via a global pointer set just before qsort().
 *--------------------------------------------------------------------------*/

typedef struct {
    int   cell_idx;   /* index into app->cells[] */
    float world_z;
    float world_x;
} SortEntry;

static int snake_compare(const void *a, const void *b) {
    const SortEntry *ea = (const SortEntry *)a;
    const SortEntry *eb = (const SortEntry *)b;

    /* Primary: Z descending */
    if (ea->world_z > eb->world_z) return -1;
    if (ea->world_z < eb->world_z) return  1;

    /* Secondary: X ascending */
    if (ea->world_x < eb->world_x) return -1;
    if (ea->world_x > eb->world_x) return  1;

    return 0;
}

/*---------------------------------------------------------------------------
 * JSON helpers
 *--------------------------------------------------------------------------*/

/* Escape a string for embedding in a JSON value (replaces \ and "). */
static void json_write_string(FILE *f, const char *s) {
    fputc('"', f);
    for (; *s; s++) {
        if (*s == '\\' || *s == '"') fputc('\\', f);
        fputc(*s, f);
    }
    fputc('"', f);
}

/*---------------------------------------------------------------------------
 * main
 *--------------------------------------------------------------------------*/

int main(int argc, char *argv[]) {

    /* ----- Default settings ----- */
    const char *mesh_path    = NULL;
    const char *output_path  = "shellpower_result.json";
    const char *preset_name  = "maxeon-gen7";
    float scale              = 1.0f;
    float rotate_x           = 0.0f;
    float rotate_y           = 0.0f;
    float rotate_z           = 0.0f;
    float grid_spacing       = 0.13f;
    float target_area        = 6.0f;
    float min_angle          = 62.0f;
    float max_angle          = 90.0f;
    int   run_daily_sim      = 0;
    int   no_occlusion_opt   = 0;
    float lat                = 37.4f;
    float lon                = -122.2f;
    int   month              = 6;
    int   day                = 21;
    int   time_samples       = 12;
    int   heading_samples    = 7;
    float sim_start_hour     = 8.0f;
    float sim_end_hour       = 18.0f;
    float min_heading        = 0.0f;
    float max_heading        = 360.0f;

    /* ----- Argument parsing (getopt-style, manual) ----- */
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];

        if (strcmp(arg, "-h") == 0 || strcmp(arg, "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        }

        /* Flags without a value */
        if (strcmp(arg, "--daily-sim") == 0) {
            run_daily_sim = 1;
            continue;
        }
        if (strcmp(arg, "--no-occlusion-opt") == 0) {
            no_occlusion_opt = 1;
            continue;
        }

        /* All remaining flags expect the next argv as their value */
        const char *val = (i + 1 < argc) ? argv[i + 1] : NULL;

#define CONSUME_VAL() do { i++; } while (0)

        if (strcmp(arg, "--mesh") == 0) {
            if (!val) { fprintf(stderr, "Error: --mesh requires a path argument\n"); return 1; }
            mesh_path = val; CONSUME_VAL();
        } else if (strcmp(arg, "--output") == 0) {
            if (!val) { fprintf(stderr, "Error: --output requires a path argument\n"); return 1; }
            output_path = val; CONSUME_VAL();
        } else if (strcmp(arg, "--scale") == 0) {
            if (!val) { fprintf(stderr, "Error: --scale requires a float argument\n"); return 1; }
            scale = parse_float(val, 1.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--rotate-x") == 0) {
            if (!val) { fprintf(stderr, "Error: --rotate-x requires a float argument\n"); return 1; }
            rotate_x = parse_float(val, 0.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--rotate-y") == 0) {
            if (!val) { fprintf(stderr, "Error: --rotate-y requires a float argument\n"); return 1; }
            rotate_y = parse_float(val, 0.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--rotate-z") == 0) {
            if (!val) { fprintf(stderr, "Error: --rotate-z requires a float argument\n"); return 1; }
            rotate_z = parse_float(val, 0.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--preset") == 0) {
            if (!val) { fprintf(stderr, "Error: --preset requires a name argument\n"); return 1; }
            preset_name = val; CONSUME_VAL();
        } else if (strcmp(arg, "--grid-spacing") == 0) {
            if (!val) { fprintf(stderr, "Error: --grid-spacing requires a float argument\n"); return 1; }
            grid_spacing = parse_float(val, 0.13f); CONSUME_VAL();
        } else if (strcmp(arg, "--target-area") == 0) {
            if (!val) { fprintf(stderr, "Error: --target-area requires a float argument\n"); return 1; }
            target_area = parse_float(val, 6.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--min-angle") == 0) {
            if (!val) { fprintf(stderr, "Error: --min-angle requires a float argument\n"); return 1; }
            min_angle = parse_float(val, 62.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--max-angle") == 0) {
            if (!val) { fprintf(stderr, "Error: --max-angle requires a float argument\n"); return 1; }
            max_angle = parse_float(val, 90.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--lat") == 0) {
            if (!val) { fprintf(stderr, "Error: --lat requires a float argument\n"); return 1; }
            lat = parse_float(val, 37.4f); CONSUME_VAL();
        } else if (strcmp(arg, "--lon") == 0) {
            if (!val) { fprintf(stderr, "Error: --lon requires a float argument\n"); return 1; }
            lon = parse_float(val, -122.2f); CONSUME_VAL();
        } else if (strcmp(arg, "--month") == 0) {
            if (!val) { fprintf(stderr, "Error: --month requires an int argument\n"); return 1; }
            month = parse_int(val, 6); CONSUME_VAL();
        } else if (strcmp(arg, "--day") == 0) {
            if (!val) { fprintf(stderr, "Error: --day requires an int argument\n"); return 1; }
            day = parse_int(val, 21); CONSUME_VAL();
        } else if (strcmp(arg, "--time-samples") == 0) {
            if (!val) { fprintf(stderr, "Error: --time-samples requires an int argument\n"); return 1; }
            time_samples = parse_int(val, 48); CONSUME_VAL();
        } else if (strcmp(arg, "--heading-samples") == 0) {
            if (!val) { fprintf(stderr, "Error: --heading-samples requires an int argument\n"); return 1; }
            heading_samples = parse_int(val, 7); CONSUME_VAL();
        } else if (strcmp(arg, "--sim-start-hour") == 0) {
            if (!val) { fprintf(stderr, "Error: --sim-start-hour requires a float argument\n"); return 1; }
            sim_start_hour = parse_float(val, 8.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--sim-end-hour") == 0) {
            if (!val) { fprintf(stderr, "Error: --sim-end-hour requires a float argument\n"); return 1; }
            sim_end_hour = parse_float(val, 18.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--min-heading") == 0) {
            if (!val) { fprintf(stderr, "Error: --min-heading requires a float argument\n"); return 1; }
            min_heading = parse_float(val, 0.0f); CONSUME_VAL();
        } else if (strcmp(arg, "--max-heading") == 0) {
            if (!val) { fprintf(stderr, "Error: --max-heading requires a float argument\n"); return 1; }
            max_heading = parse_float(val, 360.0f); CONSUME_VAL();
        } else {
            fprintf(stderr, "Warning: unknown argument '%s' (ignored)\n", arg);
        }

#undef CONSUME_VAL
    }

    /* ----- Validate required args ----- */
    if (!mesh_path) {
        fprintf(stderr, "Error: --mesh <path> is required\n");
        print_usage(argv[0]);
        return 1;
    }

    /* ----- Resolve preset index ----- */
    int preset_idx = preset_index_from_name(preset_name);
    if (preset_idx < 0) {
        fprintf(stderr,
            "Error: unknown preset '%s'. "
            "Valid options: maxeon-gen3, maxeon-gen5, maxeon-gen7, generic-silicon\n",
            preset_name);
        return 1;
    }

    /* ----- Initialise application state ----- */
    CoreAppState app;
    CoreApp_Init(&app);

    /* Apply CLI overrides */
    app.mesh_scale             = scale;
    app.mesh_rotation.x        = rotate_x;
    app.mesh_rotation.y        = rotate_y;
    app.mesh_rotation.z        = rotate_z;
    app.selected_preset        = preset_idx;

    app.sim_settings.latitude  = lat;
    app.sim_settings.longitude = lon;
    app.sim_settings.month     = month;
    app.sim_settings.day       = day;
    app.sim_settings.hour      = 12.0f;   /* noon for instant sim */

    /* Auto-layout overrides */
    app.auto_layout.grid_spacing      = grid_spacing;
    app.auto_layout.target_area       = target_area;
    app.auto_layout.min_normal_angle  = min_angle;
    app.auto_layout.max_normal_angle  = max_angle;
    app.auto_layout.time_samples      = time_samples;
    app.auto_layout.sim_start_hour    = sim_start_hour;
    app.auto_layout.sim_end_hour      = sim_end_hour;
    app.auto_layout.heading_samples   = heading_samples;
    app.auto_layout.min_heading_deg   = min_heading;
    app.auto_layout.max_heading_deg   = max_heading;
    app.auto_layout.use_grid_layout   = 1;
    if (no_occlusion_opt)
        app.auto_layout.optimize_occlusion = false;

    /* ----- Step 3: Load mesh ----- */
    fprintf(stderr, "Loading mesh: %s\n", mesh_path);
    if (!CoreApp_LoadMesh(&app, mesh_path)) {
        fprintf(stderr, "Error: %s\n", app.status_msg);
        CoreApp_Close(&app);
        return 2;
    }
    fprintf(stderr, "Mesh loaded: %d triangles\n", app.core_mesh.tri_count);

    /* ----- Configure snap for headless auto-layout ----- *
     * CoreApp_RunAutoLayout requires grid_snap_enabled and grid_configured.
     * In the GUI these are set interactively; for the CLI we configure them
     * automatically using the mesh bounds and the --grid-spacing argument.
     * CoreApp_LoadMesh resets snap, so we must do this AFTER loading.       */
    app.snap.grid_snap_enabled = true;
    app.snap.grid_configured   = true;
    app.snap.grid_size         = grid_spacing;   /* RunAutoLayout reads grid_size */
    /* Place the grid origin at the mesh min-XZ, on the upward-facing surface. */
    app.snap.grid_origin = (Vector3){
        app.mesh_bounds.min.x,
        app.mesh_bounds.min.y,
        app.mesh_bounds.min.z
    };
    app.snap.grid_normal   = (Vector3){0.0f, 1.0f, 0.0f};
    app.snap.grid_rotation = 0.0f;

    /* ----- Step 4: Run auto-layout ----- */
    fprintf(stderr, "Running auto-layout (target %.1f m^2, spacing %.3f m) ...\n",
            target_area, grid_spacing);
    int placed = CoreApp_RunAutoLayout(&app);
    fprintf(stderr, "Auto-layout placed %d cells\n", placed);

    if (app.cell_count == 0) {
        fprintf(stderr, "Error: auto-layout placed no cells on mesh '%s'\n", mesh_path);
        CoreApp_Close(&app);
        return 3;
    }

    /* ----- Step 5: Auto-wire cells (snake pattern) ----- */
    fprintf(stderr, "Wiring %d cells into one string (snake pattern) ...\n", app.cell_count);

    /* Build sort table */
    SortEntry *sort_table = (SortEntry *)malloc((size_t)app.cell_count * sizeof(SortEntry));
    if (!sort_table) {
        fprintf(stderr, "Error: out of memory allocating sort table\n");
        CoreApp_Close(&app);
        return 1;
    }

    for (int i = 0; i < app.cell_count; i++) {
        Vector3 wp = CoreApp_CellWorldPos(&app, &app.cells[i]);
        sort_table[i].cell_idx = i;
        sort_table[i].world_z  = wp.z;
        sort_table[i].world_x  = wp.x;
    }

    qsort(sort_table, (size_t)app.cell_count, sizeof(SortEntry), snake_compare);

    /* Wire all sorted cells into a single string */
    int string_id = CoreApp_StartString(&app);
    if (string_id < 0) {
        fprintf(stderr, "Error: could not start wiring string\n");
        free(sort_table);
        CoreApp_Close(&app);
        return 3;
    }

    for (int i = 0; i < app.cell_count; i++) {
        int cidx = sort_table[i].cell_idx;
        CoreApp_AddCellToString(&app, app.cells[cidx].id);
    }

    CoreApp_EndString(&app);
    free(sort_table);

    /* Give every cell an individual bypass diode so tilted/shaded cells
     * can be bypassed independently, matching fine-grained MPPT behaviour
     * and keeping results comparable to per-cell manual calculations. */
    for (int i = 0; i < app.cell_count; i++)
        app.cells[i].has_bypass_diode = true;

    fprintf(stderr, "Wired %d cells into string #%d\n",
            (app.string_count > 0) ? app.strings[0].cell_count : 0,
            string_id);

    /* ----- Step 6: Run instant simulation ----- */
    fprintf(stderr, "Running instant simulation (noon, lat=%.2f, lon=%.2f) ...\n", lat, lon);
    CoreApp_RunStaticSim(&app);
    fprintf(stderr, "Instant sim: %s\n", app.status_msg);

    /* ----- Step 7: Optionally run daily simulation ----- */
    if (run_daily_sim) {
        fprintf(stderr, "Running daily simulation (month=%d, day=%d) ...\n", month, day);
        CoreApp_RunDailySim(&app);
        fprintf(stderr, "Daily sim: %s\n", app.status_msg);
    }

    /* ----- Step 8: Write JSON output ----- */
    fprintf(stderr, "Writing output to: %s\n", output_path);

    FILE *out = fopen(output_path, "w");
    if (!out) {
        fprintf(stderr, "Error: cannot open output file '%s' for writing\n", output_path);
        CoreApp_Close(&app);
        return 4;
    }

    const CellPreset *preset = &CORE_CELL_PRESETS[app.selected_preset];

    /* Total cell area */
    float total_area = (float)app.cell_count * preset->width * preset->height;

    fprintf(out, "{\n");

    /* --- metadata --- */
    fprintf(out, "  \"metadata\": {\n");
    fprintf(out, "    \"preset\": ");
    json_write_string(out, preset->name);
    fprintf(out, ",\n");
    fprintf(out, "    \"cell_width_m\": %.6g,\n",  (double)preset->width);
    fprintf(out, "    \"cell_height_m\": %.6g,\n", (double)preset->height);
    fprintf(out, "    \"cell_count\": %d,\n",       app.cell_count);
    fprintf(out, "    \"total_area_m2\": %.6g\n",   (double)total_area);
    fprintf(out, "  },\n");

    /* --- layout --- */
    fprintf(out, "  \"layout\": [\n");
    for (int i = 0; i < app.cell_count; i++) {
        SolarCell *cell = &app.cells[i];
        Vector3 pos    = CoreApp_CellWorldPos(&app, cell);
        Vector3 nrm    = CoreApp_CellWorldNormal(&app, cell);

        fprintf(out,
            "    {\"id\":%d, \"position\":[%.6g,%.6g,%.6g], "
            "\"normal\":[%.6g,%.6g,%.6g], "
            "\"string_id\":%d, \"order_in_string\":%d}",
            cell->id,
            (double)pos.x, (double)pos.y, (double)pos.z,
            (double)nrm.x, (double)nrm.y, (double)nrm.z,
            cell->string_id,
            cell->order_in_string);

        if (i < app.cell_count - 1)
            fprintf(out, ",");
        fprintf(out, "\n");
    }
    fprintf(out, "  ],\n");

    /* --- instant_power --- */
    fprintf(out, "  \"instant_power\": {\n");
    fprintf(out, "    \"total_power_w\": %.6g,\n",
            (double)app.sim_results.total_power);
    fprintf(out, "    \"shaded_pct\": %.6g,\n",
            (double)app.sim_results.shaded_percentage);
    fprintf(out, "    \"sun_altitude\": %.6g,\n",
            (double)app.sim_results.sun_altitude);
    fprintf(out, "    \"sun_azimuth\": %.6g\n",
            (double)app.sim_results.sun_azimuth);

    if (run_daily_sim) {
        /* Close instant_power and open daily_energy */
        fprintf(out, "  },\n");
        fprintf(out, "  \"daily_energy\": {\n");
        fprintf(out, "    \"total_energy_wh\": %.6g,\n",
                (double)app.time_sim_results.total_energy_wh);
        fprintf(out, "    \"average_power_w\": %.6g,\n",
                (double)app.time_sim_results.average_power_w);
        fprintf(out, "    \"peak_power_w\": %.6g,\n",
                (double)app.time_sim_results.peak_power_w);
        fprintf(out, "    \"sun_altitude_at_peak\": %.6g,\n",
                (double)app.time_sim_results.sun_altitude_at_peak);
        fprintf(out, "    \"average_shaded_pct\": %.6g\n",
                (double)app.time_sim_results.average_shaded_pct);
        fprintf(out, "  }\n");
    } else {
        fprintf(out, "  }\n");
    }

    fprintf(out, "}\n");

    if (fclose(out) != 0) {
        fprintf(stderr, "Error: failed to flush/close output file '%s'\n", output_path);
        CoreApp_Close(&app);
        return 4;
    }

    /* ----- Cleanup ----- */
    CoreApp_Close(&app);

    fprintf(stderr, "Done. Results written to %s\n", output_path);
    return 0;
}
