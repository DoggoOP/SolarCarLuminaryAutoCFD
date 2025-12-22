from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import List, Optional, Tuple
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .config import Settings, get_settings
from .job_store import JobStore
from .luminary_pipeline import CaseConfig, LuminaryCFDPipeline

app = FastAPI(title="Luminary AutoCFD Pipeline")
templates = Jinja2Templates(directory="app/templates")
settings: Settings = get_settings()
job_store = JobStore()
executor = ThreadPoolExecutor(max_workers=5)
_submission_lock = Lock()
_last_submission_time = 0.0


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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    jobs = job_store.list_jobs()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "jobs": jobs,
            "default_speed": settings.default_farfield_speed,
            "default_project": settings.luminary_project_name,
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
    mesh_min_size: float = Form(0.002),
    mesh_max_size: float = Form(0.05),
    frontal_area: str = Form(""),
    body_surfaces: str = Form(""),
    floor_surfaces: str = Form(""),
    farfield_surfaces: str = Form(""),
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
    body_surface_list = _parse_surfaces(body_surfaces)
    floor_surface_list = _parse_surfaces(floor_surfaces)
    farfield_surface_list = _parse_surfaces(farfield_surfaces)
    padding_raw = _parse_optional_float(farfield_padding)
    farfield_padding_value = 0.0 if padding_raw is None else padding_raw
    frontal_area_override = _parse_optional_float(frontal_area)

    file_suffix = Path(cad_file.filename).suffix or ".cad"
    upload_path = settings.uploads_dir / f"{uuid4().hex}{file_suffix}"
    with upload_path.open("wb") as buffer:
        buffer.write(await cad_file.read())

    job_id = job_store.create(f"AutoCFD run for {cad_label}")
    job_store.append(job_id, f"Uploaded CAD to {upload_path}.")

    case_config = CaseConfig(
        cad_path=upload_path,
        cad_label=cad_label,
        project_name=project_name,
        farfield_direction=wind_direction_vec,
        farfield_speed=farfield_speed,
        ground_speed=ground_speed,
        mesh_min_size=mesh_min_size,
        mesh_max_size=mesh_max_size,
        farfield_multiplier=farfield_multiplier,
        farfield_padding=farfield_padding_value,
        farfield_center_override=farfield_center_vec,
        frontal_area_override=frontal_area_override,
        body_surfaces=body_surface_list or None,
        floor_surfaces=floor_surface_list or None,
        farfield_surfaces=farfield_surface_list or None,
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
