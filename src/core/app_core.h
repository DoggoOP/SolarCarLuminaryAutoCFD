#ifndef APP_CORE_H
#define APP_CORE_H

/*
 * app_core.h — Headless CLI application state.
 *
 * Mirrors the data structures in src/app.h but:
 *   - Replaces Raylib Model/Mesh with CoreMesh from core_types.h
 *   - Drops all GUI, camera, window, and Raylib-specific fields
 *   - Safe to include without raylib.h / raymath.h
 */

#include "core_types.h"
#include <stdbool.h>

/*----------------------------------------------------------------------------
 * Constants (mirrored from app.h)
 *--------------------------------------------------------------------------*/
#define MAX_CELLS              1000
#define MAX_STRINGS            50
#define MAX_CELLS_PER_STRING   500
#define MAX_PATH_LENGTH        512
#define MAX_BYPASS_DIODES      100

#define CELL_SURFACE_OFFSET       0.002f
#define MIN_CELL_DISTANCE_FACTOR  1.0f
#define MIN_UPWARD_NORMAL         0.3f

/*----------------------------------------------------------------------------
 * Minimal color type (replaces Raylib's Color in CellString)
 *--------------------------------------------------------------------------*/
#if !defined(RL_COLOR_TYPE)
typedef struct CoreColor { unsigned char r, g, b, a; } CoreColor;
#endif

/*----------------------------------------------------------------------------
 * Data Structures (layout-compatible with app.h equivalents)
 *--------------------------------------------------------------------------*/

/* Solar cell preset specifications */
typedef struct {
    const char *name;
    float width;          /* meters */
    float height;         /* meters */
    float efficiency;     /* 0-1 */
    float voc;            /* Open circuit voltage at STC */
    float isc;            /* Short circuit current at STC */
    float vmp;            /* Voltage at max power */
    float imp;            /* Current at max power */
    float n_ideal;        /* Diode ideality factor (typically 1.0-1.5) */
    float series_r;       /* Series resistance (ohms) */
    float bypass_v_drop;  /* Bypass diode forward voltage drop */
} CellPreset;

/* Individual solar cell instance */
typedef struct {
    int     id;
    Vector3 local_position;   /* Position in mesh-local coordinates (before transform) */
    Vector3 local_tangent;
    Vector3 local_normal;     /* Normal in mesh-local coordinates */
    int     string_id;        /* -1 = unwired */
    int     order_in_string;  /* Order within string for series connection */
    bool    has_bypass_diode;
    bool    is_shaded;
    bool    is_bypassed;      /* True if bypass diode is conducting */
    float   power_output;     /* Calculated during simulation */
    float   current_output;   /* Photo-generated current at current conditions */
    float   voltage_output;   /* Cell voltage at operating point */
    float   curvature_deg;    /* Max local curvature angle */
    bool    over_curvature_limit; /* True if curvature exceeds threshold */
} SolarCell;

/* A string of series-connected cells */
typedef struct {
    int        id;
    CoreColor  color;
    int        cell_ids[MAX_CELLS_PER_STRING];
    int        cell_count;
    float      total_power;     /* String power output (W) - with series constraints */
    float      total_energy_wh;
    float      string_current;  /* Operating current of entire string (A) */
    float      string_voltage;  /* Total string voltage (V) */
    int        bypassed_count;  /* Number of cells being bypassed */
    float      power_ideal;     /* Power if all cells were in full sun */
} CellString;

/* Bypass diode segment - bypasses cells between start and end (inclusive) */
typedef struct {
    int   id;
    int   string_id;       /* Which string this diode is on */
    int   start_cell_id;   /* First cell in bypassed segment */
    int   end_cell_id;     /* Last cell in bypassed segment */
    bool  is_conducting;   /* True if diode is active (bypassing cells) */
    float voltage_drop;    /* Forward voltage drop when conducting */
} BypassDiode;

/* Simulation settings */
typedef struct {
    float latitude;    /* degrees */
    float longitude;   /* degrees */
    int   year;
    int   month;
    int   day;
    float hour;        /* 0-24 decimal hours */
    float irradiance;  /* W/m^2 */
} SimSettings;

/* Auto-layout settings */
typedef struct {
    float target_area;          /* Target coverage area in m^2 */
    float min_normal_angle;     /* Minimum angle from horizontal (degrees) to consider */
    float max_normal_angle;     /* Maximum angle from horizontal (degrees) */
    float surface_threshold;    /* Maximum angle between adjacent triangles (degrees) */
    int   time_samples;         /* Number of time samples for occlusion scoring */
    float sim_start_hour;       /* Start hour for time sampling (default 8.0) */
    float sim_end_hour;         /* End hour for time sampling (default 18.0) */
    int   heading_samples;      /* Number of heading samples for occlusion scoring */
    float min_heading_deg;      /* Min car heading angle in degrees (default 0) */
    float max_heading_deg;      /* Max car heading angle in degrees (default 360) */
    bool  optimize_occlusion;   /* Whether to optimize for minimal occlusion */
    bool  preview_surface;      /* Show surface selection preview */
    bool  use_height_constraint;/* Enable height constraint (to exclude canopy) */
    bool  auto_detect_height;   /* Automatically find optimal height range */
    float height_tolerance;     /* Vertical tolerance for auto-detect (default 0.1m) */
    float min_height;           /* Minimum height for cell placement */
    float max_height;           /* Maximum height for cell placement */
    bool  use_grid_layout;      /* Use grid-based layout instead of mesh triangles */
    float grid_spacing;         /* Grid spacing for layout (0 = auto based on cell size) */
    float edge_margin;          /* Minimum inset from shell boundary (meters) */
    bool  ignore_curvature_limit; /* Allow placement past curvature threshold */
} AutoLayoutSettings;

/* Snap settings for cell placement */
typedef struct {
    bool    grid_snap_enabled;  /* Snap to grid */
    float   grid_size;          /* Grid cell size in meters */
    bool    align_to_surface;   /* Align cell orientation to surface */
    bool    show_grid;          /* Show grid overlay */

    /* Grid positioning */
    Vector3 grid_origin;        /* Grid anchor point (world coordinates) */
    Vector3 grid_normal;        /* Normal at grid origin */
    float   grid_rotation;      /* Grid rotation degrees (0-90) */

    /* Interaction state */
    bool    setting_grid_origin; /* True when in "set origin" mode */
    bool    grid_configured;     /* True after user explicitly sets grid origin on mesh */
} SnapSettings;

/* Simulation results */
typedef struct {
    float   total_power;
    float   shaded_percentage;
    int     shaded_count;
    Vector3 sun_direction;
    float   sun_altitude;
    float   sun_azimuth;
    bool    is_daytime;
} SimResults;

/* Daily simulation results */
typedef struct TimeSimResults {
    float total_energy_wh;       /* Total energy over the day (Watt-hours) */
    float average_power_w;       /* Average power over daylight hours */
    float peak_power_w;          /* Maximum instantaneous power */
    float sun_altitude_at_peak;  /* Solar altitude (degrees) when peak power occurred */
    float average_shaded_pct;    /* Average shading percentage */
    float min_power_w;           /* Minimum power (when not zero) */
    float energy_by_hour[24];    /* Energy breakdown by hour (optional) */
} TimeSimResults;

/*----------------------------------------------------------------------------
 * CoreAppState — headless equivalent of AppState
 *--------------------------------------------------------------------------*/
typedef struct {
    /* Mesh */
    CoreMesh    core_mesh;
    BoundingBox mesh_bounds;
    bool        mesh_loaded;
    float       mesh_scale;
    Vector3     mesh_rotation;            /* Euler angles in degrees (X, Y, Z) */
    char        mesh_path[MAX_PATH_LENGTH];

    /* Cells */
    SolarCell cells[MAX_CELLS];
    int       cell_count;
    int       next_cell_id;
    int       selected_preset;            /* Index into CORE_CELL_PRESETS */

    /* Strings */
    CellString strings[MAX_STRINGS];
    int        string_count;
    int        next_string_id;
    int        active_string_id;          /* Currently building string, -1 = none */

    /* Bypass diodes */
    BypassDiode bypass_diodes[MAX_BYPASS_DIODES];
    int         bypass_diode_count;
    int         next_bypass_diode_id;

    /* Auto-layout */
    AutoLayoutSettings auto_layout;
    bool               auto_layout_running;
    int                auto_layout_progress; /* 0-100 progress percentage */

    /* Snap settings */
    SnapSettings snap;

    /* Simulation */
    SimSettings    sim_settings;
    SimResults     sim_results;
    bool           sim_run;          /* Has static simulation been run? */
    bool           time_sim_run;
    TimeSimResults time_sim_results;
    bool           ignore_shading;

    /* Status */
    char status_msg[256];
} CoreAppState;

/*----------------------------------------------------------------------------
 * Cell Presets (defined in app_core.c)
 *--------------------------------------------------------------------------*/
extern const CellPreset CORE_CELL_PRESETS[];
extern const int        CORE_CELL_PRESET_COUNT;

/*----------------------------------------------------------------------------
 * Function Declarations
 *--------------------------------------------------------------------------*/

/* Lifecycle */
void CoreApp_Init(CoreAppState *app);
void CoreApp_Close(CoreAppState *app);

/* Mesh */
bool    CoreApp_LoadMesh(CoreAppState *app, const char *path);
void    CoreApp_ApplyTransform(CoreAppState *app);

/* Cells */
int     CoreApp_PlaceCell(CoreAppState *app, Vector3 world_pos, Vector3 world_normal);
void    CoreApp_ClearCells(CoreAppState *app);
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

/* Sun position (used internally and by cli_main.c) */
Vector3 CoreApp_CalcSunDirection(SimSettings *settings, float *altitude, float *azimuth);

#endif /* APP_CORE_H */
