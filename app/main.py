from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Collection, List, Optional, Tuple
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .config import Settings, get_settings
from .job_store import JobStore
from .luminary_pipeline import (
    AutoArrayConfig,
    CaseConfig,
    DEFAULT_TRANSITION_MODEL,
    DEFAULT_TURBULENCE_MODEL,
    TRANSITION_MODEL_CHOICES,
    TURBULENCE_MODEL_CHOICES,
    LuminaryCFDPipeline,
)

app = FastAPI(title="Luminary AutoCFD Pipeline")
templates = Jinja2Templates(directory="app/templates")
settings: Settings = get_settings()
job_store = JobStore()
executor = ThreadPoolExecutor(max_workers=5)
_submission_lock = Lock()
_last_submission_time = 0.0

TRANSITION_MODEL_OPTIONS = [
    {"value": "GAMMA_RE_THETA_2009", "label": "Gamma-ReTheta 2009"},
    {"value": "NO_TRANSITION", "label": "No transition"},
    {"value": "GAMMA_2015", "label": "Gamma 2015"},
    {"value": "AFT_2019", "label": "AFT 2019"},
]
TURBULENCE_MODEL_OPTIONS = [
    {"value": "KOMEGA_SST", "label": "k-omega SST"},
    {"value": "SPALART_ALLMARAS", "label": "Spalart-Allmaras"},
]


def _parse_vector(raw: str, field_name: str) -> Tuple[float, float, float]:
    try:
        parts = [float(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} vector.") from exc
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail=f"{field_name} must have three components.")
    return parts[0], parts[1], parts[2]


def _parse_surfaces(raw: str) -> List[str]:
    return [surface.strip() for surface in raw.split(",") if surface.strip()]


def _parse_optional_vector(raw: str, field_name: str) -> Optional[Tuple[float, float, float]]:
    raw = raw.strip()
    if not raw:
        return None
    return _parse_vector(raw, field_name)


def _parse_optional_float(raw: str) -> Optional[float]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid numeric value.") from exc


def _parse_choice(raw: str, allowed: Collection[str], field_name: str) -> str:
    value = raw.strip().upper()
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}. Expected one of: {allowed_text}.",
        )
    return value


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    jobs = job_store.list_jobs()

    # Construct Google Sheets URL if spreadsheet ID is configured
    sheets_url = None
    if settings.google_sheets_spreadsheet_id:
        sheets_url = f"https://docs.google.com/spreadsheets/d/{settings.google_sheets_spreadsheet_id}/edit"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "jobs": jobs,
            "default_speed": settings.default_farfield_speed,
            "default_project": settings.luminary_project_name,
            "default_transition_model": DEFAULT_TRANSITION_MODEL,
            "default_turbulence_model": DEFAULT_TURBULENCE_MODEL,
            "transition_model_options": TRANSITION_MODEL_OPTIONS,
            "turbulence_model_options": TURBULENCE_MODEL_OPTIONS,
            "sheets_url": sheets_url,
        },
    )


@app.get("/autoarray", response_class=HTMLResponse)
async def autoarray_home(request: Request) -> HTMLResponse:
    jobs = [job for job in job_store.list_jobs() if str(job.get("title", "")).startswith("AutoArray run for ")]

    sheets_url = None
    if settings.google_sheets_spreadsheet_id:
        sheets_url = f"https://docs.google.com/spreadsheets/d/{settings.google_sheets_spreadsheet_id}/edit"

    return templates.TemplateResponse(
        "autoarray.html",
        {
            "request": request,
            "jobs": jobs,
            "default_project": settings.luminary_project_name,
            "sheets_url": sheets_url,
        },
    )


@app.get("/jobs/{job_id}", response_class=JSONResponse)
async def job_status(job_id: str) -> JSONResponse:
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse(job)


@app.get("/jobs", response_class=JSONResponse)
async def list_jobs() -> JSONResponse:
    return JSONResponse(job_store.list_jobs())


@app.post("/jobs/{job_id}/cancel", response_class=JSONResponse)
async def cancel_job(job_id: str) -> JSONResponse:
    if job_store.cancel(job_id):
        return JSONResponse({"status": "cancelled", "job_id": job_id})
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    raise HTTPException(status_code=400, detail=f"Cannot cancel job with status: {job['status']}")


@app.post("/run")
async def run_case(
    request: Request,
    cad_file: UploadFile = File(...),
    cad_label: str = Form(...),
    project_name: str = Form(settings.luminary_project_name),
    farfield_speed: float = Form(settings.default_farfield_speed),
    ground_speed: float = Form(settings.default_farfield_speed),
    farfield_multiplier: float = Form(25.0),
    farfield_padding: str = Form(""),
    farfield_center: str = Form(""),
    wind_direction: str = Form("1,0,0"),
    transition_model: str = Form(DEFAULT_TRANSITION_MODEL),
    turbulence_model: str = Form(DEFAULT_TURBULENCE_MODEL),
    mesh_min_size: float = Form(0.002),
    mesh_max_size: float = Form(0.05),
    frontal_area: str = Form(""),
    body_surfaces: str = Form(""),
    floor_surfaces: str = Form(""),
    farfield_surfaces: str = Form(""),
    rotating_wheels: bool = Form(False),
    wheel_surfaces: str = Form(""),
    shellpower_enabled: bool = Form(False),
    shellpower_target_area: str = Form(""),
    shellpower_lat: float = Form(-23.7),
    shellpower_lon: float = Form(133.9),
    shellpower_dual_shadow: bool = Form(False),
    shellpower_ignore_curvature_limit: bool = Form(False),
) -> JSONResponse:
    global _last_submission_time  # noqa: PLW0603
    now = time.monotonic()
    with _submission_lock:
        if now - _last_submission_time < 10.0:
            raise HTTPException(
                status_code=429,
                detail="Please wait a few seconds before starting another simulation.",
            )
        _last_submission_time = now
    if not cad_file.filename:
        raise HTTPException(status_code=400, detail="CAD filename missing.")

    farfield_center_vec = _parse_optional_vector(farfield_center, "farfield center")
    wind_direction_vec = _parse_vector(wind_direction, "wind direction")
    transition_model_value = _parse_choice(
        transition_model,
        TRANSITION_MODEL_CHOICES,
        "transition model",
    )
    turbulence_model_value = _parse_choice(
        turbulence_model,
        TURBULENCE_MODEL_CHOICES,
        "turbulence model",
    )
    body_surface_list = _parse_surfaces(body_surfaces)
    floor_surface_list = _parse_surfaces(floor_surfaces)
    farfield_surface_list = _parse_surfaces(farfield_surfaces)
    wheel_surface_list = _parse_surfaces(wheel_surfaces)
    padding_raw = _parse_optional_float(farfield_padding)
    farfield_padding_value = 0.0 if padding_raw is None else padding_raw
    frontal_area_override = _parse_optional_float(frontal_area)

    file_suffix = Path(cad_file.filename).suffix or ".cad"
    upload_path = settings.uploads_dir / f"{uuid4().hex}{file_suffix}"
    with upload_path.open("wb") as buffer:
        buffer.write(await cad_file.read())

    job_id = job_store.create(f"AutoCFD run for {cad_label}")
    job_store.append(job_id, f"Uploaded CAD to {upload_path}.")

    shellpower_area_override = _parse_optional_float(shellpower_target_area)

    case_config = CaseConfig(
        cad_path=upload_path,
        cad_label=cad_label,
        project_name=project_name,
        farfield_direction=wind_direction_vec,
        farfield_speed=farfield_speed,
        ground_speed=ground_speed,
        mesh_min_size=mesh_min_size,
        mesh_max_size=mesh_max_size,
        transition_model=transition_model_value,
        turbulence_model=turbulence_model_value,
        farfield_multiplier=farfield_multiplier,
        farfield_padding=farfield_padding_value,
        farfield_center_override=farfield_center_vec,
        frontal_area_override=frontal_area_override,
        body_surfaces=body_surface_list or None,
        floor_surfaces=floor_surface_list or None,
        farfield_surfaces=farfield_surface_list or None,
        rotating_wheels=rotating_wheels,
        wheel_surfaces=wheel_surface_list or None,
        shellpower_enabled=shellpower_enabled,
        shellpower_target_area=shellpower_area_override,
        shellpower_lat=shellpower_lat,
        shellpower_lon=shellpower_lon,
        shellpower_dual_shadow=shellpower_dual_shadow,
        shellpower_ignore_curvature_limit=shellpower_ignore_curvature_limit,
    )

    def _log(message: str) -> None:
        job_store.append(job_id, message)

    def _check_cancelled() -> bool:
        return job_store.is_cancelled(job_id)

    def _run_pipeline() -> None:
        try:
            job_store.set_status(job_id, "running")
            # Create a new pipeline instance for this job to ensure thread safety
            job_pipeline = LuminaryCFDPipeline(settings)
            result = job_pipeline.run_case(case_config, _log, check_cancelled=_check_cancelled)
        except RuntimeError as exc:
            # Check if this was a cancellation
            if "cancelled by user" in str(exc).lower():
                _log("Job cancelled by user")
                job_store.set_status(job_id, "cancelled")
            else:
                _log(f"ERROR: {exc}")
                job_store.set_status(job_id, "failed", error=str(exc))
        except Exception as exc:  # pragma: no cover - network/SDK failures
            _log(f"ERROR: {exc}")
            job_store.set_status(job_id, "failed", error=str(exc))
        else:
            job_store.set_status(job_id, "completed", result=result)
        finally:
            upload_path.unlink(missing_ok=True)

    executor.submit(_run_pipeline)
    return JSONResponse({"job_id": job_id})


@app.post("/autoarray/run")
async def run_autoarray(
    request: Request,
    cad_file: UploadFile = File(...),
    cad_label: str = Form(...),
    project_name: str = Form(settings.luminary_project_name),
    body_surfaces: str = Form(""),
    mesh_min_size: float = Form(0.002),
    mesh_max_size: float = Form(0.05),
    shellpower_target_area: str = Form(""),
    shellpower_lat: float = Form(-23.7),
    shellpower_lon: float = Form(133.9),
    shellpower_dual_shadow: bool = Form(False),
    shellpower_ignore_curvature_limit: bool = Form(False),
) -> JSONResponse:
    global _last_submission_time  # noqa: PLW0603
    now = time.monotonic()
    with _submission_lock:
        if now - _last_submission_time < 10.0:
            raise HTTPException(
                status_code=429,
                detail="Please wait a few seconds before starting another job.",
            )
        _last_submission_time = now

    if not cad_file.filename:
        raise HTTPException(status_code=400, detail="CAD filename missing.")

    body_surface_list = _parse_surfaces(body_surfaces)
    shellpower_area_override = _parse_optional_float(shellpower_target_area)

    file_suffix = Path(cad_file.filename).suffix or ".cad"
    upload_path = settings.uploads_dir / f"{uuid4().hex}{file_suffix}"
    with upload_path.open("wb") as buffer:
        buffer.write(await cad_file.read())

    job_id = job_store.create(f"AutoArray run for {cad_label}")
    job_store.append(job_id, f"Uploaded geometry to {upload_path}.")

    autoarray_config = AutoArrayConfig(
        cad_path=upload_path,
        cad_label=cad_label,
        project_name=project_name,
        body_surfaces=body_surface_list or None,
        mesh_min_size=mesh_min_size,
        mesh_max_size=mesh_max_size,
        shellpower_target_area=shellpower_area_override,
        shellpower_lat=shellpower_lat,
        shellpower_lon=shellpower_lon,
        shellpower_dual_shadow=shellpower_dual_shadow,
        shellpower_ignore_curvature_limit=shellpower_ignore_curvature_limit,
    )

    def _log(message: str) -> None:
        job_store.append(job_id, message)

    def _check_cancelled() -> bool:
        return job_store.is_cancelled(job_id)

    def _run_pipeline() -> None:
        try:
            job_store.set_status(job_id, "running")
            job_pipeline = LuminaryCFDPipeline(settings)
            result = job_pipeline.run_auto_array(autoarray_config, _log, check_cancelled=_check_cancelled)
        except RuntimeError as exc:
            if "cancelled by user" in str(exc).lower():
                _log("Job cancelled by user")
                job_store.set_status(job_id, "cancelled")
            else:
                _log(f"ERROR: {exc}")
                job_store.set_status(job_id, "failed", error=str(exc))
        except Exception as exc:  # pragma: no cover
            _log(f"ERROR: {exc}")
            job_store.set_status(job_id, "failed", error=str(exc))
        else:
            job_store.set_status(job_id, "completed", result=result)
        finally:
            upload_path.unlink(missing_ok=True)

    executor.submit(_run_pipeline)
    return JSONResponse({"job_id": job_id})
