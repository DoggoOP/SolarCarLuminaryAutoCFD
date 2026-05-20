from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import luminarycloud as lc
from luminarycloud.enum import QuantityType, ResidualType, CalculationType
from luminarycloud.meshing import MeshGenerationParams, BoundaryLayerParams
from luminarycloud.outputs import ForceOutputDefinition, ResidualOutputDefinition
from luminarycloud.params.geometry import shapes as geom_shapes
from luminarycloud.pipelines import api as pipelines_api
from luminarycloud.types import Vector3

from .config import Settings
from .sheets_logger import SheetsLogger

StatusCallback = Callable[[str], None]
CancellationCheck = Callable[[], bool]

DEFAULT_TRANSITION_MODEL = "GAMMA_RE_THETA_2009"
DEFAULT_TURBULENCE_MODEL = "KOMEGA_SST"
TRANSITION_MODEL_CHOICES = {
    "NO_TRANSITION",
    "GAMMA_2015",
    "GAMMA_RE_THETA_2009",
    "AFT_2019",
}
TURBULENCE_MODEL_CHOICES = {
    "KOMEGA_SST",
    "SPALART_ALLMARAS",
}


def _normalize_choice(value: str, allowed: set[str], field_name: str) -> str:
    normalized = (value or "").strip().upper()
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"Invalid {field_name}: {value!r}. Expected one of: {allowed_text}.")
    return normalized


def _set_vector(target: dict, vector: Tuple[float, float, float]) -> None:
    for axis, value in zip(("x", "y", "z"), vector):
        if value == 0:
            target.setdefault(axis, {})
            target[axis].pop("value", None)
        else:
            target.setdefault(axis, {})["value"] = value




@dataclass
class CaseConfig:
    cad_path: Path
    cad_label: str
    project_name: str
    farfield_direction: Tuple[float, float, float]
    farfield_speed: float
    mesh_min_size: float
    mesh_max_size: float
    transition_model: str = DEFAULT_TRANSITION_MODEL
    turbulence_model: str = DEFAULT_TURBULENCE_MODEL
    farfield_multiplier: float = 20.0
    farfield_padding: float = 0.0
    farfield_center_override: Optional[Tuple[float, float, float]] = None
    body_surfaces: Optional[Sequence[str]] = None
    floor_surfaces: Optional[Sequence[str]] = None
    farfield_surfaces: Optional[Sequence[str]] = None
    ground_speed: float = 24.59  # Vehicle forward speed for moving floor (m/s)
    frontal_area_override: Optional[float] = None  # Manual frontal area (m²) - overrides calculation
    rotating_wheels: bool = False
    wheel_surfaces: Optional[Sequence[str]] = None
    wheel_rotation_rate: float = 110.2
    front_wheel_center: Tuple[float, float, float] = (0.0, 0.0, 0.28)
    rear_wheel_center: Tuple[float, float, float] = (-2.679, 0.0, 0.28)
    shellpower_enabled: bool = False
    shellpower_target_area: Optional[float] = None  # None = use Settings default
    shellpower_lat: float = -23.7
    shellpower_lon: float = 133.9
    shellpower_month: int = 8    # August — WSC race month
    shellpower_day: int = 25     # August 25 — approximate WSC start
    shellpower_dual_shadow: bool = False
    shellpower_ignore_curvature_limit: bool = False
    shellpower_min_angle: float = 62.0
    shellpower_edge_margin: float = 0.035


@dataclass
class AutoArrayConfig:
    cad_path: Path
    cad_label: str
    project_name: str
    body_surfaces: Optional[Sequence[str]] = None
    mesh_min_size: float = 0.002
    mesh_max_size: float = 0.05
    shellpower_target_area: Optional[float] = None
    shellpower_lat: float = -23.7
    shellpower_lon: float = 133.9
    shellpower_month: int = 8
    shellpower_day: int = 25
    shellpower_dual_shadow: bool = False
    shellpower_ignore_curvature_limit: bool = False
    shellpower_min_angle: float = 30.0
    shellpower_edge_margin: float = 0.035


class SimulationTemplateBuilder:
    """Utility to copy and tweak the baseline JSON template."""

    def __init__(self, template_path: Path) -> None:
        if not template_path.exists():
            raise FileNotFoundError(f"Simulation template file {template_path} missing.")
        self._template_path = template_path
        self._base_payload = json.loads(template_path.read_text())

    def build_payload(
        self,
        *,
        body_surfaces: Sequence[str],
        floor_surfaces: Sequence[str],
        farfield_surfaces: Sequence[str],
        farfield_vector: Tuple[float, float, float],
        farfield_speed: float,
        sound_speed: float,
        frontal_area: float,
        transition_model: str = DEFAULT_TRANSITION_MODEL,
        turbulence_model: str = DEFAULT_TURBULENCE_MODEL,
        ground_speed: float = 24.59,
        rotating_wheels: bool = False,
        front_wheel_surfaces: Optional[Sequence[str]] = None,
        rear_wheel_surfaces: Optional[Sequence[str]] = None,
        wheel_rotation_rate: float = 110.2,
        front_wheel_center: Tuple[float, float, float] = (0.0, 0.0, 0.28),
        rear_wheel_center: Tuple[float, float, float] = (-2.679, 0.0, 0.28),
    ) -> dict:
        payload = copy.deepcopy(self._base_payload)
        ref_values = payload.setdefault("referenceValues", {})
        ref_values.setdefault("vRef", {})["value"] = farfield_speed
        ref_values.setdefault("lengthRef", {})["value"] = 5.8
        ref_values.setdefault("areaRef", {})["value"] = frontal_area

        physics = payload["physics"][0]["fluid"]
        all_bcs = physics.get("boundaryConditionsFluid", [])
        other_bcs: List[dict] = []
        wall_template: Optional[dict] = None
        farfield_template: Optional[dict] = None
        for bc in all_bcs:
            boundary_type = bc.get("physicalBoundary")
            if boundary_type == "WALL" and wall_template is None:
                wall_template = copy.deepcopy(bc)
            elif boundary_type == "FARFIELD" and farfield_template is None:
                farfield_template = copy.deepcopy(bc)
            else:
                other_bcs.append(copy.deepcopy(bc))

        if wall_template is None or farfield_template is None:
            raise ValueError("Base simulation template is missing wall or farfield boundary data.")

        # Prepare wheel surfaces for boundary conditions
        all_wheel_surfaces: List[str] = []
        if rotating_wheels:
            if front_wheel_surfaces:
                all_wheel_surfaces.extend(front_wheel_surfaces)
            if rear_wheel_surfaces:
                all_wheel_surfaces.extend(rear_wheel_surfaces)

        # Create car body BC, excluding wheel surfaces if wheels are rotating
        car_body_surfaces = list(body_surfaces)
        if rotating_wheels and all_wheel_surfaces:
            # Remove wheel surfaces from body surfaces
            car_body_surfaces = [s for s in body_surfaces if s not in all_wheel_surfaces]

        car_bc = self._build_wall_bc(
            wall_template,
            car_body_surfaces,
            "car_body_wall",
        )
        floor_bc = self._build_wall_bc(
            wall_template,
            list(floor_surfaces),
            "moving_floor_wall",
        )

        farfield_bc = copy.deepcopy(farfield_template)
        farfield_bc["boundaryConditionName"] = "auto_farfield"
        farfield_bc["surfaces"] = list(farfield_surfaces)
        _set_vector(farfield_bc.setdefault("farfieldFlowDirection", {}), farfield_vector)
        farfield_bc.setdefault("farfieldVelocityMagnitude", {})["value"] = farfield_speed
        mach = max(farfield_speed / sound_speed, 1e-4)
        farfield_bc.setdefault("farfieldMachNumber", {})["value"] = mach

        # Create wheel BC if rotating wheels enabled
        if rotating_wheels and all_wheel_surfaces:
            wheel_bc = self._build_wall_bc(
                wall_template,
                all_wheel_surfaces,
                "rotating_wheels_wall",
            )
            physics["boundaryConditionsFluid"] = other_bcs + [car_bc, floor_bc, wheel_bc, farfield_bc]
        else:
            physics["boundaryConditionsFluid"] = other_bcs + [car_bc, floor_bc, farfield_bc]

        uniform_v = physics.setdefault("initializationFluid", {}).setdefault("uniformV", {})
        _set_vector(uniform_v, farfield_vector)
        # Use motion frame for moving floor (always at constant ground speed, not wind speed)
        self._attach_floor_motion(payload, floor_surfaces, ground_speed)

        # Attach wheel motion frames if rotating wheels enabled
        if rotating_wheels and (front_wheel_surfaces or rear_wheel_surfaces):
            self._attach_wheel_motion(
                payload,
                front_wheel_surfaces=front_wheel_surfaces or [],
                rear_wheel_surfaces=rear_wheel_surfaces or [],
                rotation_rate=wheel_rotation_rate,
                front_center=front_wheel_center,
                rear_center=rear_wheel_center,
            )

        # Don't normalize physics - let Luminary use the template's original physics ID
        # self._normalize_physics_metadata(payload)

        self._configure_physics_models(
            payload,
            transition_model=transition_model,
            turbulence_model=turbulence_model,
        )

        # Configure adaptive mesh refinement
        # On Railway (production), enable Lumi Mesh Adaptation with target of 10M CVs
        # On localhost (development), disable adaptation completely (generate minimal mesh only)
        import os
        is_production = os.getenv("RAILWAY_ENVIRONMENT") is not None

        amr = payload.setdefault("adaptiveMeshRefinement", {})

        if is_production:
            # Production: Enable Lumi Mesh Adaptation with 10M CVs
            amr["meshingMethod"] = "MESH_METHOD_AUTO"
            amr.setdefault("target_cv_millions", {})["value"] = 10
        else:
            # Local development: Generate minimal mesh only (no adaptation)
            amr["meshingMethod"] = "MESH_METHOD_MINIMAL"

        return payload

    @staticmethod
    def _configure_physics_models(
        payload: dict,
        *,
        transition_model: str,
        turbulence_model: str,
    ) -> None:
        transition_model = _normalize_choice(
            transition_model,
            TRANSITION_MODEL_CHOICES,
            "transition model",
        )
        turbulence_model = _normalize_choice(
            turbulence_model,
            TURBULENCE_MODEL_CHOICES,
            "turbulence model",
        )

        physics_list = payload.get("physics") or []
        if not physics_list:
            return

        fluid_physics = physics_list[0].setdefault("fluid", {})
        basic = fluid_physics.setdefault("basicFluid", {})
        turbulence = fluid_physics.setdefault("turbulence", {})

        basic["viscousModel"] = "RANS"
        turbulence["turbulenceModel"] = turbulence_model
        turbulence["transitionModel"] = transition_model

        if turbulence_model == "KOMEGA_SST":
            turbulence.setdefault("qcrSst", "SST_QCR2000")
            turbulence.pop("qcrSa", None)
            turbulence.pop("rotationCorrectionSa", None)
        elif turbulence_model == "SPALART_ALLMARAS":
            turbulence.setdefault("qcrSa", "QCR_OFF")
            turbulence.setdefault("rotationCorrectionSa", "ROTATION_CORRECTION_OFF")
            turbulence.pop("qcrSst", None)

    def dump_payload(self, payload: dict, *, label: Optional[str] = None) -> Path:
        """Persist the payload to dumps/ for debugging."""
        dumps_dir = Path(__file__).resolve().parent.parent / "dumps"
        dumps_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        safe_label = "params"
        if label:
            safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in label) or "params"
        filename = f"{safe_label}-{timestamp}-{uuid.uuid4().hex[:8]}.json"
        dump_path = dumps_dir / filename
        dump_path.write_text(json.dumps(payload, indent=2))
        return dump_path

    @staticmethod
    def _build_wall_bc(
        template: dict,
        surfaces: Sequence[str],
        name: str,
    ) -> dict:
        if not surfaces:
            raise ValueError(f"No surfaces provided for wall boundary '{name}'.")
        bc = copy.deepcopy(template)
        bc["boundaryConditionName"] = name
        bc["surfaces"] = list(surfaces)
        if bc.get("physicalBoundary") == "WALL":
            bc["wallMomentum"] = "NO_SLIP"
        # Clear deprecated wall movement fields - motion is specified via frames
        for key in ("wallMovementTranslation", "wallMovementRotationCenter", "wallMovementAngularVelocity"):
            if key in bc:
                del bc[key]
        return bc

    def _attach_floor_motion(
        self,
        payload: dict,
        floor_surfaces: Sequence[str],
        speed: float,
    ) -> None:
        """
        Attach moving floor boundary condition.

        The floor moves at constant forward speed (ground speed) to simulate
        the vehicle moving through stationary air. This is independent of the
        wind speed/direction for crosswind scenarios.
        """
        if not floor_surfaces:
            return
        current_motion = payload.setdefault("motionData", [])
        filtered_motion = [
            entry for entry in current_motion if entry.get("frameId") != "moving_floor_frame"
        ]
        # Moving floor velocity in x-direction at ground speed
        zero_vector = {
            "x": {"value": 0.0},
            "y": {"value": 0.0},
            "z": {"value": 0.0},
        }
        translation = copy.deepcopy(zero_vector)
        translation_velocity = {
            "x": {"value": -speed},
            "y": {"value": 0.0},
            "z": {"value": 0.0},
        }
        motion_entry = {
            "frameId": "moving_floor_frame",
            "frameName": "Moving Floor",
            "frameParent": "global_frame_id",
            "attachedBoundaries": list(floor_surfaces),
            "motionType": "CONSTANT_TRANSLATION_MOTION",
            "motionSpecification": "MOTION_SPECIFICATION_NORMAL",
            "motionFormulation": "MRF_MOTION_FORMULATION",
            "motionTranslation": translation,
            "motionTranslationVelocity": translation_velocity,
            "motionAngularVelocity": copy.deepcopy(zero_vector),
            "motionRotationAngles": copy.deepcopy(zero_vector),
            # Provide snake_case aliases for backward compatibility
            "motion_type": "CONSTANT_TRANSLATION_MOTION",
            "motion_specification": "MOTION_SPECIFICATION_NORMAL",
            "motion_formulation": "MRF_MOTION_FORMULATION",
            "motion_translation": translation,
            "motion_translation_velocity": translation_velocity,
            "motion_angular_velocity": copy.deepcopy(zero_vector),
            "motion_rotation_angles": copy.deepcopy(zero_vector),
        }
        filtered_motion.append(motion_entry)
        payload["motionData"] = filtered_motion

    def _attach_wheel_motion(
        self,
        payload: dict,
        front_wheel_surfaces: Sequence[str],
        rear_wheel_surfaces: Sequence[str],
        rotation_rate: float,
        front_center: Tuple[float, float, float],
        rear_center: Tuple[float, float, float],
    ) -> None:
        """
        Attach rotating wheel motion frames.

        Parameters
        ----------
        payload : dict
            Simulation payload to modify
        front_wheel_surfaces : Sequence[str]
            Surface names for front wheels
        rear_wheel_surfaces : Sequence[str]
            Surface names for rear wheels
        rotation_rate : float
            Angular velocity in rad/s (around Y-axis)
        front_center : Tuple[float, float, float]
            Front wheel rotation center (x, y, z) in global coordinates
        rear_center : Tuple[float, float, float]
            Rear wheel rotation center (x, y, z) in global coordinates
        """
        current_motion = payload.setdefault("motionData", [])

        # Remove existing wheel frames if present (idempotent)
        filtered_motion = [
            entry for entry in current_motion
            if entry.get("frameId") not in ("front_wheels_frame", "rear_wheels_frame")
        ]

        zero_vector = {
            "x": {"value": 0.0},
            "y": {"value": 0.0},
            "z": {"value": 0.0},
        }

        # Create front wheels frame if surfaces provided
        if front_wheel_surfaces:
            front_angular_velocity = {
                "x": {"value": 0.0},
                "y": {"value": rotation_rate},
                "z": {"value": 0.0},
            }

            front_wheel_entry = {
                "frameId": "front_wheels_frame",
                "frameName": "Front Wheels",
                "frameParent": "global_frame_id",
                "attachedBoundaries": list(front_wheel_surfaces),
                "motionType": "CONSTANT_ANGULAR_MOTION",
                "motionSpecification": "MOTION_SPECIFICATION_NORMAL",
                "motionFormulation": "MRF_MOTION_FORMULATION",
                "motionTranslation": copy.deepcopy(zero_vector),
                "motionTranslationVelocity": copy.deepcopy(zero_vector),
                "motionAngularVelocity": front_angular_velocity,
                "motionRotationAngles": copy.deepcopy(zero_vector),
                "frameTransforms": [
                    {
                        "transformName": "Front Wheels-origin",
                        "transformType": "TRANSLATIONAL_TRANSFORM",
                        "transformRotationAngles": copy.deepcopy(zero_vector),
                        "transformTranslation": {
                            "x": {"value": front_center[0], "quantityType": "LENGTH"},
                            "y": {"value": front_center[1], "quantityType": "LENGTH"},
                            "z": {"value": front_center[2], "quantityType": "LENGTH"},
                        },
                    }
                ],
                # Provide snake_case aliases for backward compatibility
                "motion_type": "CONSTANT_ANGULAR_MOTION",
                "motion_specification": "MOTION_SPECIFICATION_NORMAL",
                "motion_formulation": "MRF_MOTION_FORMULATION",
                "motion_translation": copy.deepcopy(zero_vector),
                "motion_translation_velocity": copy.deepcopy(zero_vector),
                "motion_angular_velocity": front_angular_velocity,
                "motion_rotation_angles": copy.deepcopy(zero_vector),
            }
            filtered_motion.append(front_wheel_entry)

        # Create rear wheels frame if surfaces provided
        if rear_wheel_surfaces:
            rear_angular_velocity = {
                "x": {"value": 0.0},
                "y": {"value": rotation_rate},
                "z": {"value": 0.0},
            }

            rear_wheel_entry = {
                "frameId": "rear_wheels_frame",
                "frameName": "Rear Wheels",
                "frameParent": "global_frame_id",
                "attachedBoundaries": list(rear_wheel_surfaces),
                "motionType": "CONSTANT_ANGULAR_MOTION",
                "motionSpecification": "MOTION_SPECIFICATION_NORMAL",
                "motionFormulation": "MRF_MOTION_FORMULATION",
                "motionTranslation": copy.deepcopy(zero_vector),
                "motionTranslationVelocity": copy.deepcopy(zero_vector),
                "motionAngularVelocity": rear_angular_velocity,
                "motionRotationAngles": copy.deepcopy(zero_vector),
                "frameTransforms": [
                    {
                        "transformName": "Rear Wheels-origin",
                        "transformType": "TRANSLATIONAL_TRANSFORM",
                        "transformRotationAngles": copy.deepcopy(zero_vector),
                        "transformTranslation": {
                            "x": {"value": rear_center[0], "quantityType": "LENGTH"},
                            "y": {"value": rear_center[1], "quantityType": "LENGTH"},
                            "z": {"value": rear_center[2], "quantityType": "LENGTH"},
                        },
                    }
                ],
                # Provide snake_case aliases for backward compatibility
                "motion_type": "CONSTANT_ANGULAR_MOTION",
                "motion_specification": "MOTION_SPECIFICATION_NORMAL",
                "motion_formulation": "MRF_MOTION_FORMULATION",
                "motion_translation": copy.deepcopy(zero_vector),
                "motion_translation_velocity": copy.deepcopy(zero_vector),
                "motion_angular_velocity": rear_angular_velocity,
                "motion_rotation_angles": copy.deepcopy(zero_vector),
            }
            filtered_motion.append(rear_wheel_entry)

        payload["motionData"] = filtered_motion

    @staticmethod
    def _normalize_physics_metadata(payload: dict) -> None:
        """Ensure physics identifiers match the expected Fluid Flow 1 naming."""
        physics_list = payload.get("physics") or []
        if not physics_list:
            return
        fluid = physics_list[0]
        identifier = fluid.setdefault("physicsIdentifier", {})
        identifier["id"] = "m3lahi1ckjf8ustjtjedwtxs1es8sre8"
        identifier["name"] = "Fluid Flow 1"

        relationships = (
            payload.setdefault("entityRelationships", {}).get("volumePhysicsRelationship") or []
        )
        for rel in relationships:
            rel_identifier = rel.setdefault("physicsIdentifier", {})
            rel_identifier["id"] = identifier["id"]
            rel_identifier["name"] = identifier["name"]


class LuminaryCFDPipeline:
    """Encapsulates all Luminary Cloud API interactions for the AutoCFD flow."""

    AUTOARRAY_TOP_SHELL_MIN_Y = 0.35
    AUTOARRAY_TOP_SHELL_MAX_Y = 0.80

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[lc.Client] = None
        self._template_builder = SimulationTemplateBuilder(settings.base_sim_template_path)
        self._sheets_logger: Optional[SheetsLogger] = SheetsLogger.from_env()

    def _run_shellpower_cli_for_mesh(
        self,
        mesh_path: Path,
        callback: StatusCallback,
        *,
        target_area: Optional[float],
        lat: float,
        lon: float,
        month: int,
        day: int,
        dual_shadow: bool,
        ignore_curvature_limit: bool,
        min_angle: float,
        edge_margin: float,
    ) -> Optional[dict]:
        if not self._settings.shellpower_cli_path:
            callback("Shellpower enabled but SHELLPOWER_CLI_PATH not set — skipping")
            return None

        resolved_target_area = (
            target_area if target_area is not None else self._settings.shellpower_target_area
        )

        with tempfile.TemporaryDirectory() as sp_tmp:
            base_cmd = [
                self._settings.shellpower_cli_path,
                "--mesh", str(mesh_path),
                "--target-area", str(resolved_target_area),
                "--min-angle", str(min_angle),
                "--lat", str(lat),
                "--lon", str(lon),
                "--month", str(month),
                "--day", str(day),
                "--preset", "maxeon-gen7",
                "--grid-spacing", "0.126",
                "--time-samples", "12",
                "--sim-start-hour", "8",
                "--sim-end-hour", "17",
                "--heading-samples", "7",
                "--min-heading", "55",
                "--max-heading", "125",
                "--edge-margin", str(edge_margin),
            ]
            if self._settings.shellpower_enable_daily_sim:
                base_cmd.append("--daily-sim")
            if ignore_curvature_limit:
                base_cmd.append("--ignore-curvature-limit")

            run_variants = [
                {
                    "key": "no_shadow",
                    "label": "Symmetric (no shadow)",
                    "mode": "no_shadow",
                    "extra": ["--no-occlusion-opt", "--ignore-shading"],
                    "occlusion": False,
                }
            ]
            if dual_shadow:
                run_variants.append(
                    {
                        "key": "shadow",
                        "label": "Shadow-aware comparison",
                        "mode": "shadow",
                        "extra": [],
                        "occlusion": True,
                    }
                )

            shellpower_runs: List[Dict[str, Any]] = []
            for variant in run_variants:
                json_path = Path(sp_tmp) / f"shellpower_result_{variant['key']}.json"
                cmd = [*base_cmd, "--output", str(json_path), *variant["extra"]]
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=1500,
                    )
                except subprocess.TimeoutExpired:
                    callback(f"Shellpower ({variant['label']}) timed out (600 s)")
                    continue
                except Exception as exc:  # pragma: no cover
                    callback(f"Shellpower ({variant['label']}) error: {exc}")
                    continue

                if proc.returncode != 0 or not json_path.exists():
                    stderr_excerpt = (proc.stderr or "").strip()[:200]
                    callback(
                        f"Shellpower ({variant['label']}) failed (exit {proc.returncode}): "
                        f"{stderr_excerpt}"
                    )
                    continue

                try:
                    raw = json.loads(json_path.read_text())
                except Exception as exc:  # pragma: no cover
                    callback(f"Shellpower ({variant['label']}) produced invalid JSON: {exc}")
                    continue

                meta = raw.get("metadata", {})
                cells = meta.get("cell_count", 0)
                total_area_m2 = meta.get("total_area_m2")
                max_curvature = meta.get("max_curvature_deg")
                curvature_limit = meta.get("curvature_limit_deg")
                curvature_violations = meta.get("curvature_violations")
                curvature_limit_ignored = meta.get("curvature_limit_ignored")
                energy = raw.get("daily_energy", {}).get("total_energy_wh")
                peak_power = raw.get("daily_energy", {}).get("peak_power_w", 0.0)
                shaded_pct = raw.get("instant_power", {}).get("shaded_pct")
                sun_alt = (
                    raw.get("daily_energy", {}).get("sun_altitude_at_peak")
                    or raw.get("instant_power", {}).get("sun_altitude")
                )
                layout = raw.get("layout", [])
                array_map_b64: Optional[str] = None
                if layout:
                    try:
                        array_map_b64 = LuminaryCFDPipeline._generate_array_map(layout)
                    except Exception as map_exc:  # pragma: no cover
                        callback(f"Array map generation failed ({variant['label']}): {map_exc}")

                variant_result = {
                    "mode": variant["mode"],
                    "label": variant["label"],
                    "occlusion_optimized": variant["occlusion"],
                    "cells_placed": cells,
                    "total_area_m2": total_area_m2,
                    "instant_power_w": peak_power,
                    "daily_energy_wh": energy,
                    "shaded_pct": shaded_pct,
                    "sun_altitude": sun_alt,
                    "array_map_b64": array_map_b64,
                    "max_curvature_deg": max_curvature,
                    "curvature_limit_deg": curvature_limit,
                    "curvature_violations": curvature_violations,
                    "curvature_limit_ignored": curvature_limit_ignored,
                }
                shellpower_runs.append(variant_result)

                msg = f"✓ Shellpower ({variant['label']}): {cells} cells"
                if total_area_m2 is not None:
                    msg += f" ({total_area_m2:.2f} m²)"
                msg += f", peak {peak_power:.1f} W"
                if energy is not None:
                    msg += f", {energy:.0f} Wh/day"
                if shaded_pct is not None:
                    msg += f", {shaded_pct:.0f}% shaded"
                if sun_alt is not None:
                    msg += f" (sun at peak: {sun_alt:.0f}°)"
                if max_curvature is not None:
                    msg += f", max curvature {max_curvature:.1f}°"
                    if curvature_limit and curvature_limit > 0:
                        msg += f" (limit {curvature_limit:.1f}°)"
                if curvature_violations:
                    msg += f", {curvature_violations} over limit"
                callback(msg)

            if not shellpower_runs:
                return None

            primary = shellpower_runs[0]
            return {
                "cells_placed": primary["cells_placed"],
                "total_area_m2": primary["total_area_m2"],
                "instant_power_w": primary["instant_power_w"],
                "daily_energy_wh": primary["daily_energy_wh"],
                "shaded_pct": primary["shaded_pct"],
                "sun_altitude": primary["sun_altitude"],
                "array_map_b64": primary["array_map_b64"],
                "max_curvature_deg": primary.get("max_curvature_deg"),
                "curvature_limit_deg": primary.get("curvature_limit_deg"),
                "curvature_violations": primary.get("curvature_violations"),
                "curvature_limit_ignored": primary.get("curvature_limit_ignored"),
                "variants": shellpower_runs,
            }

    @staticmethod
    def _prepare_autoarray_mesh_for_shellpower(
        input_mesh_path: Path,
        output_obj_path: Path,
        callback: StatusCallback,
    ) -> Path:
        try:
            import meshio
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"AutoArray mesh preprocessing requires meshio: {exc}") from exc

        try:
            mesh = meshio.read(str(input_mesh_path))
        except Exception as exc:
            raise RuntimeError(f"Failed to read uploaded mesh '{input_mesh_path.name}': {exc}") from exc

        points = np.asarray(mesh.points)
        if points.size == 0:
            raise RuntimeError("Uploaded mesh contains no vertices")

        triangles: List[np.ndarray] = []
        for block in mesh.cells:
            block_data = np.asarray(block.data)
            if block.type == "triangle":
                triangles.append(block_data)
            elif block.type == "quad":
                triangles.append(block_data[:, [0, 1, 2]])
                triangles.append(block_data[:, [0, 2, 3]])

        if not triangles:
            raise RuntimeError("Uploaded mesh contains no triangle faces")

        tris = np.vstack(triangles).astype(np.int32)

        transformed = np.empty_like(points)
        transformed[:, 0] = points[:, 0]
        transformed[:, 1] = points[:, 2]
        transformed[:, 2] = points[:, 1]

        axis_labels = ("X", "Y", "Z")
        axis_spans = transformed.max(axis=0) - transformed.min(axis=0)
        scaled_axes: List[str] = []
        for axis_index, span in enumerate(axis_spans):
            if span > 50.0:
                transformed[:, axis_index] /= 1000.0
                scaled_axes.append(axis_labels[axis_index])
        if scaled_axes:
            callback(
                "AutoArray unit normalization: scaled axes from mm to m for "
                + ", ".join(scaled_axes)
            )

        min_y = LuminaryCFDPipeline.AUTOARRAY_TOP_SHELL_MIN_Y
        max_y = LuminaryCFDPipeline.AUTOARRAY_TOP_SHELL_MAX_Y
        tri_y = transformed[tris][:, :, 1]
        tri_min_y = tri_y.min(axis=1)
        tri_max_y = tri_y.max(axis=1)
        keep_mask = (tri_max_y >= min_y) & (tri_min_y <= max_y)
        kept_count = int(np.count_nonzero(keep_mask))
        if kept_count == 0:
            raise RuntimeError(
                f"No triangles remain after top-shell filter {min_y:.2f}–{max_y:.2f} m in Y-up"
            )

        filtered_tris = tris[keep_mask]
        used_vertices = np.unique(filtered_tris)
        remap = np.zeros(len(transformed), dtype=np.int32)
        remap[used_vertices] = np.arange(len(used_vertices), dtype=np.int32)
        filtered_verts = transformed[used_vertices]
        filtered_tris = remap[filtered_tris]

        filtered_verts[:, 0] -= filtered_verts[:, 0].min()
        filtered_verts[:, 2] -= filtered_verts[:, 2].min()

        centroid = filtered_verts.mean(axis=0)
        v0 = filtered_verts[filtered_tris[:, 0]]
        v1 = filtered_verts[filtered_tris[:, 1]]
        v2 = filtered_verts[filtered_tris[:, 2]]
        face_normals = np.cross(v1 - v0, v2 - v0)
        face_centers = (v0 + v1 + v2) / 3.0
        inward = np.sum(face_normals * (face_centers - centroid), axis=1) < 0
        filtered_tris[inward] = filtered_tris[inward][:, [0, 2, 1]]

        output_obj_path.parent.mkdir(parents=True, exist_ok=True)
        with output_obj_path.open("w") as f:
            f.write("# autoarray shellpower mesh export\n")
            for vertex in filtered_verts:
                f.write(f"v {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}\n")
            for tri in filtered_tris:
                f.write(f"f {tri[0] + 1} {tri[1] + 1} {tri[2] + 1}\n")

        callback(
            "AutoArray mesh filter: "
            f"{len(tris)} input triangles → {len(filtered_tris)} top-shell triangles "
            f"using Y-up overlap window [{min_y:.2f}, {max_y:.2f}] m"
        )
        callback(
            f"AutoArray filtered OBJ bounds: X=[{filtered_verts[:,0].min():.2f}, {filtered_verts[:,0].max():.2f}] "
            f"Y=[{filtered_verts[:,1].min():.2f}, {filtered_verts[:,1].max():.2f}] "
            f"Z=[{filtered_verts[:,2].min():.2f}, {filtered_verts[:,2].max():.2f}]"
        )
        return output_obj_path

    def run_auto_array(
        self,
        config: AutoArrayConfig,
        callback: StatusCallback,
        check_cancelled: Optional[CancellationCheck] = None,
    ) -> dict:
        def _check_cancellation() -> None:
            if check_cancelled and check_cancelled():
                raise RuntimeError("Job cancelled by user")

        suffix = config.cad_path.suffix.lower()
        if suffix in {".stl", ".obj"}:
            callback("Detected mesh input; filtering top shell and converting to Shellpower coordinates...")
            with tempfile.TemporaryDirectory() as mesh_tmp:
                filtered_obj_path = Path(mesh_tmp) / "autoarray_shellpower_input.obj"
                prepared_mesh = self._prepare_autoarray_mesh_for_shellpower(
                    config.cad_path,
                    filtered_obj_path,
                    callback,
                )
                shellpower_data = self._run_shellpower_cli_for_mesh(
                    prepared_mesh,
                    callback,
                    target_area=config.shellpower_target_area,
                    lat=config.shellpower_lat,
                    lon=config.shellpower_lon,
                    month=config.shellpower_month,
                    day=config.shellpower_day,
                    dual_shadow=config.shellpower_dual_shadow,
                    ignore_curvature_limit=config.shellpower_ignore_curvature_limit,
                    min_angle=config.shellpower_min_angle,
                    edge_margin=config.shellpower_edge_margin,
                )
            if not shellpower_data:
                raise RuntimeError("Shellpower did not produce results for the uploaded mesh")
            return {
                "input_filename": config.cad_path.name,
                "shellpower_data": shellpower_data,
            }

        if suffix in {".step", ".stp"}:
            callback("Detected STEP input; using Luminary meshing path before Shellpower...")
            _check_cancellation()
            case_config = CaseConfig(
                cad_path=config.cad_path,
                cad_label=config.cad_label,
                project_name=config.project_name,
                farfield_direction=(1.0, 0.0, 0.0),
                farfield_speed=self._settings.default_farfield_speed,
                mesh_min_size=config.mesh_min_size,
                mesh_max_size=config.mesh_max_size,
                body_surfaces=config.body_surfaces,
                shellpower_enabled=True,
                shellpower_target_area=config.shellpower_target_area,
                shellpower_lat=config.shellpower_lat,
                shellpower_lon=config.shellpower_lon,
                shellpower_month=config.shellpower_month,
                shellpower_day=config.shellpower_day,
                shellpower_dual_shadow=config.shellpower_dual_shadow,
                shellpower_ignore_curvature_limit=config.shellpower_ignore_curvature_limit,
                shellpower_min_angle=config.shellpower_min_angle,
                shellpower_edge_margin=config.shellpower_edge_margin,
            )
            result = self.run_case(case_config, callback, check_cancelled=check_cancelled)
            return {
                "project_id": result.get("project_id"),
                "simulation_id": result.get("simulation_id"),
                "input_filename": config.cad_path.name,
                "shellpower_data": result.get("shellpower_data"),
            }

        raise RuntimeError("AutoArray supports STL, OBJ, STEP, and STP files")

    def _client_or_create(self) -> lc.Client:
        if not self._client:
            self._client = lc.Client(api_key=self._settings.luminary_api_key)
            # Set as default client for this pipeline instance's operations
            lc.set_default_client(self._client)
        return self._client

    def _setup_stopping_conditions(
        self,
        template: lc.SimulationTemplate,
        physics_id: str,
        force_surfaces: Sequence[str],
        callback: StatusCallback,
    ) -> int:
        """
        Configure convergence criteria and force outputs via API after template creation.

        Returns
        -------
        int
            Maximum number of iterations configured
        """
        callback("Setting up stopping conditions and force outputs...")

        # Set general stopping conditions
        max_iterations = 12500
        template.update_general_stopping_conditions(
            max_iterations=max_iterations,
            stop_on_any=True,
        )

        # Define residual output definitions to monitor
        residual_quantities = [
            (QuantityType.RESIDUAL_DENSITY, "Continuity Residual"),
            (QuantityType.RESIDUAL_X_MOMENTUM, "X-Momentum Residual"),
            (QuantityType.RESIDUAL_Y_MOMENTUM, "Y-Momentum Residual"),
            (QuantityType.RESIDUAL_Z_MOMENTUM, "Z-Momentum Residual"),
            (QuantityType.RESIDUAL_ENERGY, "Energy Residual"),
            (QuantityType.RESIDUAL_TKE, "TKE Residual"),
            (QuantityType.RESIDUAL_OMEGA, "Omega Residual"),
        ]

        threshold = 0.0001  # Convergence threshold for residuals

        for quantity, name in residual_quantities:
            try:
                # Create residual output definition
                output_def = ResidualOutputDefinition(
                    name=name,
                    include={quantity: True},
                    residual_type=ResidualType.RELATIVE,
                    physics_id=physics_id,
                )
                created_def = template.create_output_definition(output_def)

                # Create stopping condition on this output
                template.create_or_update_stopping_condition(
                    output_definition_id=created_def.id,
                    threshold=threshold,
                )
                callback(f"  Created stopping condition for {name}")
            except Exception as exc:
                callback(f"  Warning: Could not create stopping condition for {name}: {exc}")
                # Continue with other residuals even if one fails

        # Create force output definitions
        # Use force_direction to specify exact direction in global coordinates:
        # Drag = total force in -x direction (flow from +x, drag opposes it)
        # Viscous drag = viscous force in -x direction
        # Pressure drag = pressure force in -x direction
        # Sideforce = force in y direction (lateral)
        # Lift = force in z direction (upward)
        callback("Creating force and area output definitions...")
        drag_vector = Vector3(-1, 0, 0)
        force_outputs = [
            ("Drag (Fx)", QuantityType.TOTAL_FORCE, drag_vector),  # Force component along global -x
            ("Viscous Drag", QuantityType.VISCOUS_DRAG, drag_vector),
            ("Pressure Drag", QuantityType.PRESSURE_DRAG, drag_vector),
            ("Side Force (Fy)", QuantityType.TOTAL_FORCE, Vector3(0, 1, 0)),
            ("Lift (Fz)", QuantityType.TOTAL_FORCE, Vector3(0, 0, 1)),
        ]

        for name, quantity, direction in force_outputs:
            try:
                force_def = ForceOutputDefinition(
                    name=name,
                    quantity=quantity,
                    surfaces=list(force_surfaces),
                    # Use global frame for force direction
                    reference_frame_id="global_frame_id",
                    force_direction=direction,
                )
                created_def = template.create_output_definition(force_def)
                callback(f"  ✓ Created force output: {name} (ID: {created_def.id})")
            except Exception as exc:
                callback(f"  ✗ ERROR creating force output for {name}: {exc}")
                # Continue even if one fails

        # Create area output for wetted area calculation
        # Note: Unlike TOTAL_FORCE/TOTAL_MOMENT, AREA requires an output definition to be created
        area_output_created = False
        try:
            from luminarycloud.outputs import SurfaceAverageOutputDefinition
            area_def = SurfaceAverageOutputDefinition(
                name="Body Surface Area",
                quantity=QuantityType.AREA,
                surfaces=list(force_surfaces),
                calc_type=CalculationType.AGGREGATE,
            )
            created_def = template.create_output_definition(area_def)
            callback(f"  ✓ Created area output: Body Surface Area (ID: {created_def.id})")
            area_output_created = True
        except Exception as exc:
            callback(f"  ✗ Warning: Could not create area output: {exc}")
            # Don't fail - we can still get force results

        return max_iterations, area_output_created

    def run_case(
        self,
        config: CaseConfig,
        callback: StatusCallback,
        check_cancelled: Optional[CancellationCheck] = None,
    ) -> dict:
        def _check_cancellation() -> None:
            """Check if job has been cancelled and raise exception if so."""
            if check_cancelled and check_cancelled():
                raise RuntimeError("Job cancelled by user")

        client = self._client_or_create()
        case_name = f"{config.cad_label.strip()}-{datetime.utcnow():%Y%m%d-%H%M%S}"

        project = self._ensure_project(client, config.project_name, callback)
        _check_cancellation()

        callback("Uploading CAD and creating geometry …")
        geometry = project.create_geometry(
            config.cad_path,
            name=f"{case_name}-geometry",
            wait=True,
        )
        callback(f"Geometry created with id={geometry.id}. Computing bounding box …")
        _check_cancellation()
        bbox_min, bbox_max = self._geometry_bounds(geometry)
        dims = tuple(max(bmax - bmin, 1e-4) for bmin, bmax in zip(bbox_min, bbox_max))
        center = tuple((bmin + bmax) / 2 for bmin, bmax in zip(bbox_min, bbox_max))

        # Use manual frontal area if provided (will be calculated from mesh later if not provided)
        if config.frontal_area_override:
            frontal_area = config.frontal_area_override
            callback(f"Using manual frontal area: {frontal_area:.4f} m²")
        else:
            # Will be computed from mesh projection after meshing
            frontal_area = None
        if config.farfield_center_override:
            center = (
                config.farfield_center_override[0],
                config.farfield_center_override[1],
                center[2],
            )
        width = max(dims[0] * config.farfield_multiplier, dims[0] + 0.1)
        front = max(dims[1] * 30, dims[1] + 0.1)
        back = min(dims[1] * -60, - dims[1] -0.1)
        padding = config.farfield_padding
        floor_z = min(bbox_min[2], bbox_max[2]) - 0.01 - padding
        z_height = max(dims[2] * config.farfield_multiplier, dims[2] + 0.05)
        z_max = floor_z + z_height + padding
        min_corner = (
            center[0] + back - padding,
            center[1] - width / 2 - padding,
            floor_z,
        )
        max_corner = (
            center[0] + front + padding,
            center[1] + width / 2 + padding,
            z_max,
        )
        farfield_bounds = (min_corner, max_corner)
        callback(
            "Bounding box dimensions "
            f"{dims} m. Using rectangular farfield with min {min_corner} and max {max_corner} "
            f"(multiplier {config.farfield_multiplier}, padding {padding})."
        )
        callback("Adding farfield volume …")
        farfield_shape = geom_shapes.Cube(
            min=Vector3(*min_corner),
            max=Vector3(*max_corner),
        )
        geometry.add_farfield(farfield_shape)
        _check_cancellation()
        geometry_ok, issues = geometry.check()
        callback(f"Geometry check returned ok={geometry_ok}. Issues: {issues}")
        if not geometry_ok:
            raise RuntimeError(
                "Geometry check failed. Please resolve the reported issues and try again."
            )

        _check_cancellation()
        callback("Generating mesh with Luminary meshing service …")

        # Configure adaptive boundary layer
        # Apply to all surfaces - Luminary will only apply to solid walls (body surfaces)
        # farfield and symmetry boundaries won't get boundary layers
        boundary_layer = BoundaryLayerParams(
            surfaces=["*"],  # Wildcard to apply to all applicable surfaces
            n_layers=40,
            initial_size=0.00003484511739,
            growth_rate=1.15,
        )

        mesh_params = MeshGenerationParams(
            geometry_id=geometry.id,
            min_size=config.mesh_min_size,
            max_size=config.mesh_max_size,
            add_refinement=True,
            boundary_layer_params=[boundary_layer],
        )
        mesh = project.create_or_get_mesh(mesh_params, name=f"{case_name}-mesh")
        mesh_status = mesh.wait(interval_seconds=10)
        callback(f"Mesh generation finished with status {mesh_status.name}.")
        _check_cancellation()
        if mesh_status.name != "COMPLETED":
            raise RuntimeError(f"Meshing failed with status {mesh_status.name}")

        metadata = mesh.get_metadata()
        surface_map = self._collect_surface_metadata(metadata)
        surface_names = sorted(surface_map.keys())
        callback(f"Detected surfaces: {', '.join(surface_names) or 'none'}")

        # Calculate frontal area if not manually provided
        if frontal_area is None:  # Not set yet (no manual override)
            callback("Computing actual frontal area from mesh geometry...")
            frontal_area_computed = self._compute_projected_area_from_mesh(mesh, axis='x')
            if frontal_area_computed > 0:
                frontal_area = frontal_area_computed
                callback(f"✓ Computed frontal area from mesh projection: {frontal_area:.4f} m²")
            else:
                # Fallback to bounding box
                frontal_area = self._calculate_frontal_area(bbox_min, bbox_max)
                callback(f"⚠ Using bounding box frontal area (mesh projection unavailable): {frontal_area:.4f} m²")

        floor_surfaces = list(
            config.floor_surfaces
            or self._infer_surfaces(
                surface_names,
                ["floor", "ground", "road", "deck", "runway"],
            )
        )
        if not floor_surfaces:
            floor_surfaces = self._infer_floor_surfaces_by_z(
                surface_map,
                floor_z,
            )

        if not floor_surfaces:
            raise RuntimeError(
                "Failed to infer floor surfaces automatically. "
                "Please specify them explicitly in the request."
            )

        farfield_surfaces = list(
            config.farfield_surfaces
            or self._infer_surfaces(
                surface_names,
                ["far", "domain"],
                exclude=floor_surfaces,
            )
        )
        if not farfield_surfaces:
            farfield_surfaces = self._infer_farfield_surfaces_by_bounds(
                surface_map,
                farfield_bounds,
                exclude=floor_surfaces,
            )

        if not farfield_surfaces:
            raise RuntimeError(
                "Failed to infer farfield surfaces automatically. "
                "Please specify them explicitly in the request."
            )

        # Detect wheel surfaces if rotating wheels enabled
        front_wheel_surfaces: List[str] = []
        rear_wheel_surfaces: List[str] = []
        wheel_surfaces_for_area: List[str] = []

        if config.rotating_wheels:
            callback("Rotating wheels enabled - detecting wheel surfaces...")

            wheel_surfaces = list(
                config.wheel_surfaces
                or self._infer_wheel_surfaces_by_z(
                    surface_map,
                    floor_z,
                    exclude=floor_surfaces + farfield_surfaces,
                    z_min=0.0,
                    z_max=0.065,
                )
            )

            if wheel_surfaces:
                callback(f"Detected {len(wheel_surfaces)} wheel surfaces: {wheel_surfaces}")
                front_wheel_surfaces, rear_wheel_surfaces = self._categorize_wheels_by_x(
                    surface_map, wheel_surfaces
                )
                callback(f"Front wheels: {front_wheel_surfaces}, Rear: {rear_wheel_surfaces}")
                wheel_surfaces_for_area = front_wheel_surfaces + rear_wheel_surfaces
            else:
                callback("Warning: No wheel surfaces detected")

        # Exclude wheels from body surfaces if rotating wheels enabled
        remaining_exclude = farfield_surfaces + floor_surfaces
        if config.rotating_wheels and (front_wheel_surfaces or rear_wheel_surfaces):
            remaining_exclude = remaining_exclude + front_wheel_surfaces + rear_wheel_surfaces

        body_surfaces = list(
            config.body_surfaces
            or self._infer_surfaces(
                surface_names,
                ["car", "body", "vehicle", "shell", "fairing"],
                exclude=remaining_exclude,
            )
        )
        if not body_surfaces:
            body_surfaces = [
                name for name in surface_names if name not in remaining_exclude
            ]
        if not body_surfaces:
            raise RuntimeError(
                "Failed to infer body surfaces. Provide explicit surface names for the solar car."
            )

        force_surfaces = list(dict.fromkeys(body_surfaces + wheel_surfaces_for_area))

        callback(
            "Using body surfaces: "
            f"{body_surfaces}; floor surfaces: {floor_surfaces}; farfield surfaces: {farfield_surfaces}."
        )
        callback(
            "CFD models: "
            f"turbulence={config.turbulence_model}, transition={config.transition_model}."
        )

        farfield_vector = self._direction_vector(
            config.farfield_direction, config.farfield_speed
        )
        # Build simulation payload
        # Note: For crosswind scenarios (e.g., wind_direction=(-24.59,10,0), wind_speed=26.55):
        #   - farfield_speed (26.55) = magnitude of wind vector, used for reference velocity and coefficients
        #   - ground_speed (24.59) = constant forward speed, used for moving floor boundary condition
        #   This separation ensures the floor moves at vehicle speed, not wind speed
        payload = self._template_builder.build_payload(
            body_surfaces=body_surfaces,
            floor_surfaces=floor_surfaces,
            farfield_surfaces=farfield_surfaces,
            farfield_vector=farfield_vector,
            farfield_speed=config.farfield_speed,
            sound_speed=self._settings.sound_speed,
            frontal_area=frontal_area,
            transition_model=config.transition_model,
            turbulence_model=config.turbulence_model,
            ground_speed=config.ground_speed,
            rotating_wheels=config.rotating_wheels,
            front_wheel_surfaces=front_wheel_surfaces,
            rear_wheel_surfaces=rear_wheel_surfaces,
            wheel_rotation_rate=config.wheel_rotation_rate,
            front_wheel_center=config.front_wheel_center,
            rear_wheel_center=config.rear_wheel_center,
        )
        tmp_params = self._template_builder.dump_payload(payload, label=case_name)
        try:
            template = project.create_simulation_template(
                f"{case_name}-template",
                params_json_path=tmp_params,
            )
        finally:
            # Keep the file for debugging
            callback(f"DEBUG: Simulation params saved to {tmp_params}")
            # tmp_params.unlink(missing_ok=True)
        callback(f"Created simulation template {template.id}.")

        # Verify the transition model in the created template
        try:
            template_params = template.get_parameters()
            physics_list = getattr(template_params, 'physics', None) or []
            if physics_list and len(physics_list) > 0:
                fluid_physics = physics_list[0]
                if hasattr(fluid_physics, 'fluid') and hasattr(fluid_physics.fluid, 'turbulence'):
                    transition_model = getattr(fluid_physics.fluid.turbulence, 'transition_model', 'UNKNOWN')
                    turbulence_model = getattr(fluid_physics.fluid.turbulence, 'turbulence_model', 'UNKNOWN')
                    callback(
                        "DEBUG: Template CFD models: "
                        f"turbulence={turbulence_model}, transition={transition_model}"
                    )
        except Exception as exc:
            callback(f"DEBUG: Could not verify transition model: {exc}")

        # Get physics_id from the template parameters (don't hardcode it)
        try:
            template_params = template.get_parameters()
            physics_list = getattr(template_params, 'physics', None) or []
            if physics_list and len(physics_list) > 0:
                physics_id = physics_list[0].physics_identifier.id
                callback(f"DEBUG: Using physics_id from template: {physics_id}")
            else:
                # Fallback to base template's physics ID if not found
                physics_id = "2924da26-7049-4381-8e51-3fce6539d124"
                callback(f"DEBUG: Using base template physics_id: {physics_id}")
        except Exception as exc:
            # Fallback to base template's physics ID if extraction fails
            physics_id = "2924da26-7049-4381-8e51-3fce6539d124"
            callback(f"DEBUG: Failed to extract physics_id ({exc}), using base template ID: {physics_id}")

        # Set up stopping conditions and force outputs via API
        max_iterations, area_output_created = self._setup_stopping_conditions(
            template,
            physics_id,
            force_surfaces,
            callback,
        )

        _check_cancellation()
        callback("Launching simulation …")
        callback(f"DEBUG: mesh_id={mesh.id}, template_id={template.id}")
        callback(f"DEBUG: project_id={project.id}, simulation_name={case_name}-simulation")

        simulation = project.create_simulation(
            mesh.id,
            f"{case_name}-simulation",
            template.id,
        )
        callback(f"Simulation created with ID: {simulation.id}")
        callback("Waiting for simulation to complete...")
        status = simulation.wait(interval_seconds=15, print_residuals=False)
        callback(f"Simulation completed with status {status.name}.")
        _check_cancellation()
        if status.name != "COMPLETED":
            # Try to get detailed error information
            error_info = []

            # Check simulation object for error messages
            try:
                if hasattr(simulation, 'status_message') and simulation.status_message:
                    error_info.append(f"Status message: {simulation.status_message}")
            except Exception:
                pass

            # Check simulation events
            try:
                events = simulation.list_events()
                if events:
                    error_events = [e for e in events if hasattr(e, 'level') and e.level == 'ERROR']
                    if error_events:
                        for event in error_events[-3:]:  # Last 3 errors
                            error_info.append(f"Event: {event.message if hasattr(event, 'message') else str(event)}")
            except Exception:
                pass

            # Try workflow details as fallback
            workflow_detail = self._fetch_failure_details(simulation)
            if workflow_detail and "404 Error" not in workflow_detail:
                error_info.append(workflow_detail)

            if error_info:
                detail = "; ".join(error_info)
                callback(f"Failure details: {detail}")
                raise RuntimeError(
                    f"Simulation terminated with status {status.name}: {detail}"
                )
            else:
                callback(f"Simulation failed with status {status.name}. No detailed error info available.")
                callback(f"Check simulation in Luminary Cloud UI: https://app.luminarycloud.com/project/{project.id}/simulation/{simulation.id}")
                raise RuntimeError(
                    f"Simulation terminated with status {status.name}. "
                    f"View in Luminary Cloud: https://app.luminarycloud.com/project/{project.id}/simulation/{simulation.id}"
                )

        # Fetch force results
        callback("Fetching force results...")
        force_results = self._fetch_force_results(
            simulation,
            template,
            ref_area=frontal_area,
            ref_velocity=config.farfield_speed,
            project=project,
            force_surfaces=force_surfaces,
            callback=callback,
        )

        # Calculate center of pressure
        callback(f"Calculating center of pressure using {len(body_surfaces)} body surfaces...")
        cop_results = self._calculate_center_of_pressure(
            simulation,
            body_surfaces=body_surfaces,
        )
        # Check for errors in CoP calculation
        if "cop_error" in cop_results:
            callback(f"Warning: CoP calculation failed: {cop_results['cop_error']}")
        else:
            callback(f"✓ CoP calculated: ({cop_results.get('cop_x', 0):.3f}, {cop_results.get('cop_y', 0):.3f}, {cop_results.get('cop_z', 0):.3f}) m")
        # Merge CoP results into force_results
        force_results.update(cop_results)

        # Calculate wetted area
        total_surfaces_count = len(body_surfaces) + len(wheel_surfaces_for_area)
        callback(f"Calculating wetted area using {total_surfaces_count} surfaces (body + wheels)...")
        wetted_area = self._calculate_wetted_area(
            simulation,
            template,
            body_surfaces,
            wheel_surfaces=wheel_surfaces_for_area if wheel_surfaces_for_area else None,
            area_output_created=area_output_created,
            callback=callback,
        )
        force_results["wetted_area"] = wetted_area
        if wetted_area > 0:
            callback(f"✓ Wetted area: {wetted_area:.4f} m²")
        else:
            callback("⚠ Wetted area returned 0 - check error above")

        # Calculate CdA and CdW
        cd = force_results.get("coeff_x", 0)
        force_results["cd_a"] = cd * frontal_area  # Cd × frontal area
        force_results["cd_w"] = cd * wetted_area if wetted_area > 0 else 0  # Cd × wetted area

        # Run Shellpower solar analysis if enabled
        shellpower_data: Optional[dict] = None
        if config.shellpower_enabled and self._settings.shellpower_cli_path:
            callback("Running Shellpower solar analysis...")
            with tempfile.TemporaryDirectory() as sp_tmp:
                obj_path = Path(sp_tmp) / "shellpower_input.obj"
                ok = self._export_shellpower_mesh(
                    simulation=simulation,
                    body_surfaces=body_surfaces,
                    project=project,
                    out_obj_path=obj_path,
                    callback=callback,
                )
                if ok:
                    shellpower_data = self._run_shellpower_cli_for_mesh(
                        obj_path,
                        callback,
                        target_area=config.shellpower_target_area,
                        lat=config.shellpower_lat,
                        lon=config.shellpower_lon,
                        month=config.shellpower_month,
                        day=config.shellpower_day,
                        dual_shadow=config.shellpower_dual_shadow,
                        ignore_curvature_limit=config.shellpower_ignore_curvature_limit,
                        min_angle=config.shellpower_min_angle,
                        edge_margin=config.shellpower_edge_margin,
                    )
                else:
                    callback("Shellpower: mesh export failed, skipping CLI run")
        elif config.shellpower_enabled:
            callback("Shellpower enabled but SHELLPOWER_CLI_PATH not set — skipping")

        # Log results to Google Sheets if configured
        if self._sheets_logger:
            try:
                callback("Logging results to Google Sheets...")
                convergence_info = {
                    "status": status.name,
                    "iterations": max_iterations,
                }
                self._sheets_logger.append_result(
                    job_name=case_name,
                    project_id=project.id,
                    simulation_id=simulation.id,
                    force_results=force_results,
                    wind_speed=config.farfield_speed,
                    wind_direction=config.farfield_direction,
                    frontal_area=frontal_area,
                    convergence_info=convergence_info,
                    model_settings={
                        "transition_model": config.transition_model,
                        "turbulence_model": config.turbulence_model,
                    },
                    shellpower_data=shellpower_data,
                )
                callback("✓ Results logged to Google Sheets")
            except Exception as exc:
                callback(f"Warning: Failed to log to Google Sheets: {exc}")

        # Include force results in return value
        result = {
            "project_id": project.id,
            "geometry_id": geometry.id,
            "mesh_id": mesh.id,
            "simulation_id": simulation.id,
            "template_id": template.id,
            "status": status.name,
            "transition_model": config.transition_model,
            "turbulence_model": config.turbulence_model,
            "force_results": force_results,
            "shellpower_data": shellpower_data,
        }

        # Log force values for visibility
        if "force_x" in force_results:
            callback(
                f"Results: Fx={force_results['force_x']:.3f}N (Cx={force_results.get('coeff_x', 0):.4f}), "
                f"Fy={force_results['force_y']:.3f}N (Cy={force_results.get('coeff_y', 0):.4f}), "
                f"Fz={force_results['force_z']:.3f}N (Cz={force_results.get('coeff_z', 0):.4f})"
            )

        return result

    def _ensure_project(
        self, client: lc.Client, project_name: str, callback: StatusCallback
    ) -> lc.Project:
        callback(f"Ensuring project '{project_name}' exists …")
        for existing in lc.list_projects():
            if existing.name == project_name:
                callback(f"Re-using existing project {existing.id}.")
                return existing
        callback("Project not found. Creating a new project …")
        return lc.create_project(project_name, "Auto-created by the AutoCFD pipeline.")

    @staticmethod
    def _collect_surface_metadata(metadata: Any) -> Dict[str, Any]:
        boundaries: Dict[str, Any] = {}
        for zone in metadata.zones:
            for boundary in zone.boundaries:
                boundaries[boundary.name] = boundary
        return boundaries

    @staticmethod
    def _infer_surfaces(
        candidates: Iterable[str],
        tokens: Sequence[str],
        *,
        invert: bool = False,
        exclude: Optional[Sequence[str]] = None,
    ) -> List[str]:
        exclude = exclude or []
        normalized_exclude = {name.lower() for name in exclude}
        selected: List[str] = []
        for name in candidates:
            lower_name = name.lower()
            if lower_name in normalized_exclude:
                continue
            matches = any(token in lower_name for token in tokens)
            if (matches and not invert) or (invert and not matches):
                selected.append(name)
        if not selected and invert:
            # fall back to everything not excluded
            selected = [name for name in candidates if name.lower() not in normalized_exclude]
        return selected

    @staticmethod
    def _direction_vector(
        direction: Tuple[float, float, float], speed: float
    ) -> Tuple[float, float, float]:
        norm = math.sqrt(sum(axis**2 for axis in direction))
        if norm == 0:
            raise ValueError("Wind direction vector cannot be zero.")
        unit = tuple(axis / norm for axis in direction)
        return tuple(component * speed for component in unit)

    @staticmethod
    def _infer_floor_surfaces_by_z(
        surface_map: Dict[str, Any],
        floor_z: float,
        tolerance: float = 0.02,
    ) -> List[str]:
        """Identify every horizontal surface located at the inferred floor plane."""
        candidates: List[str] = []
        for name, boundary in surface_map.items():
            stats = getattr(boundary, "stats", None)
            if not stats:
                continue
            min_coord = getattr(stats, "min_coord", None)
            max_coord = getattr(stats, "max_coord", None)
            if not min_coord or not max_coord:
                continue
            min_z = getattr(min_coord, "z", None)
            max_z = getattr(max_coord, "z", None)
            if min_z is None or max_z is None:
                continue
            is_flat_at_floor = (
                abs(min_z - floor_z) <= tolerance
                and abs(max_z - floor_z) <= tolerance
            )
            if is_flat_at_floor:
                candidates.append(name)
        return sorted(candidates)

    @staticmethod
    def _infer_farfield_surfaces_by_bounds(
        surface_map: Dict[str, Any],
        bounds: Tuple[Tuple[float, float, float], Tuple[float, float, float]],
        *,
        exclude: Optional[Sequence[str]] = None,
        tolerance: float = 0.05,
    ) -> List[str]:
        min_corner, max_corner = bounds
        exclude_lower = {name.lower() for name in (exclude or [])}
        candidates: List[str] = []
        for name, boundary in surface_map.items():
            if name.lower() in exclude_lower:
                continue
            stats = getattr(boundary, "stats", None)
            if not stats:
                continue
            min_coord = getattr(stats, "min_coord", None)
            max_coord = getattr(stats, "max_coord", None)
            if not min_coord or not max_coord:
                continue
            hits = (
                abs(min_coord.x - min_corner[0]) <= tolerance
                or abs(max_coord.x - max_corner[0]) <= tolerance
                or abs(min_coord.y - min_corner[1]) <= tolerance
                or abs(max_coord.y - max_corner[1]) <= tolerance
                or abs(max_coord.z - max_corner[2]) <= tolerance
            )
            if hits:
                candidates.append(name)
        return candidates

    @staticmethod
    def _infer_wheel_surfaces_by_z(
        surface_map: Dict[str, Any],
        floor_z: float,
        exclude: Optional[Sequence[str]] = None,
        z_min: float = 0.0,
        z_max: float = 0.065,
    ) -> List[str]:
        """
        Identify surfaces with Z-coordinates in the wheel contact zone.

        Parameters
        ----------
        surface_map : Dict[str, Any]
            Mapping of surface names to boundary metadata
        floor_z : float
            Z-coordinate of the floor (typically -0.01m)
        exclude : Optional[Sequence[str]]
            Surface names to exclude (e.g., floor, farfield)
        z_min : float
            Minimum Z-coordinate for wheel detection (default: 0.0)
        z_max : float
            Maximum Z-coordinate for wheel detection (default: 0.065)

        Returns
        -------
        List[str]
            Sorted list of wheel surface names
        """
        exclude_lower = {name.lower() for name in (exclude or [])}
        candidates: List[str] = []

        for name, boundary in surface_map.items():
            # Skip excluded surfaces
            if name.lower() in exclude_lower:
                continue

            stats = getattr(boundary, "stats", None)
            if not stats:
                continue

            min_coord = getattr(stats, "min_coord", None)
            max_coord = getattr(stats, "max_coord", None)
            if not min_coord or not max_coord:
                continue

            min_z = getattr(min_coord, "z", None)
            max_z = getattr(max_coord, "z", None)
            if min_z is None or max_z is None:
                continue

            # Check if surface overlaps the wheel zone [z_min, z_max]
            touches_wheel_zone = (min_z <= z_max) and (max_z >= z_min)

            if touches_wheel_zone:
                candidates.append(name)

        return sorted(candidates)

    @staticmethod
    def _categorize_wheels_by_x(
        surface_map: Dict[str, Any],
        wheel_surfaces: Sequence[str],
        x_threshold: float = -1.0,
    ) -> Tuple[List[str], List[str]]:
        """
        Categorize wheel surfaces into front and rear based on X-coordinate threshold.

        Surfaces with X < x_threshold are rear wheels, otherwise front wheels.

        Parameters
        ----------
        surface_map : Dict[str, Any]
            Mapping of surface names to boundary metadata
        wheel_surfaces : Sequence[str]
            List of wheel surface names to categorize
        x_threshold : float
            X-coordinate threshold for front/rear split (default: -1.0m)
            Surfaces with X < threshold are rear wheels

        Returns
        -------
        Tuple[List[str], List[str]]
            Tuple of (front_wheels, rear_wheels) surface name lists
        """
        if not wheel_surfaces:
            return [], []

        front_wheels: List[str] = []
        rear_wheels: List[str] = []

        for name in wheel_surfaces:
            boundary = surface_map.get(name)
            if not boundary:
                continue

            stats = getattr(boundary, "stats", None)
            if not stats:
                continue

            min_coord = getattr(stats, "min_coord", None)
            max_coord = getattr(stats, "max_coord", None)
            if not min_coord or not max_coord:
                continue

            # Calculate centroid X coordinate
            centroid_x = (getattr(min_coord, "x", 0) + getattr(max_coord, "x", 0)) / 2

            # Categorize based on threshold
            if centroid_x < x_threshold:
                rear_wheels.append(name)
            else:
                front_wheels.append(name)

        return front_wheels, rear_wheels

    @staticmethod
    def _geometry_bounds(
        geometry: lc.Geometry,
    ) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        try:
            _, volumes = geometry.list_entities()
        except Exception:
            volumes = []
        if not volumes and hasattr(geometry, "latest_version"):
            version = geometry.latest_version()
            if version:
                volumes = getattr(version, "volumes", []) or []
        if not volumes:
            tags = geometry.list_tags()
            volumes = []
            for tag in tags:
                volumes.extend(getattr(tag, "volumes", []) or [])
        min_vals = [math.inf, math.inf, math.inf]
        max_vals = [-math.inf, -math.inf, -math.inf]
        for volume in volumes:
            bbox_min = getattr(volume, "bbox_min", None)
            bbox_max = getattr(volume, "bbox_max", None)
            if not bbox_min or not bbox_max:
                continue
            mins = (bbox_min.x, bbox_min.y, bbox_min.z)
            maxs = (bbox_max.x, bbox_max.y, bbox_max.z)
            for idx in range(3):
                min_vals[idx] = min(min_vals[idx], mins[idx])
                max_vals[idx] = max(max_vals[idx], maxs[idx])
        if any(math.isinf(val) for val in min_vals + max_vals):
            raise RuntimeError("Unable to extract bounding box information from the geometry tags.")
        return tuple(min_vals), tuple(max_vals)

    @staticmethod
    def _calculate_frontal_area(
        bbox_min: Tuple[float, float, float],
        bbox_max: Tuple[float, float, float],
    ) -> float:
        """Calculate frontal area as YZ plane projection (assuming X is forward direction)."""
        # Frontal area = height (Z) × width (Y)
        height = abs(bbox_max[2] - bbox_min[2])
        width = abs(bbox_max[1] - bbox_min[1])
        frontal_area = height * width
        return max(frontal_area, 0.01)  # Ensure minimum area to avoid division by zero

    @staticmethod
    def _compute_projected_area_from_mesh(mesh: lc.Mesh, axis: str = 'x') -> float:
        """
        Compute projected area of mesh onto a plane perpendicular to the given axis.

        Parameters
        ----------
        mesh : lc.Mesh
            Luminary mesh object
        axis : str
            Projection axis ('x', 'y', or 'z'). Default 'x' projects onto YZ plane (frontal area).

        Returns
        -------
        float
            Projected area in m², or 0 if computation fails
        """
        try:
            import numpy as np
            import tempfile
            import os

            # Download mesh data
            with mesh.download() as download:
                # Save to temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix='.vtu') as tmp:
                    tmp.write(download.read())
                    tmp_path = tmp.name

                try:
                    # Try meshio first (simpler, more reliable)
                    try:
                        import meshio

                        mesh_data = meshio.read(tmp_path)
                        points = mesh_data.points

                        # Get all surface triangles
                        triangles = []
                        for cell_block in mesh_data.cells:
                            if cell_block.type == "triangle":
                                triangles.extend(cell_block.data)

                        if not triangles:
                            return 0.0

                        # Calculate projected area
                        total_area = 0.0
                        for tri in triangles:
                            p0 = points[tri[0]]
                            p1 = points[tri[1]]
                            p2 = points[tri[2]]

                            # Project triangle onto plane perpendicular to axis
                            if axis.lower() == 'x':
                                # Project onto YZ plane (frontal area)
                                p0_proj = np.array([p0[1], p0[2]])
                                p1_proj = np.array([p1[1], p1[2]])
                                p2_proj = np.array([p2[1], p2[2]])
                            elif axis.lower() == 'y':
                                # Project onto XZ plane (side area)
                                p0_proj = np.array([p0[0], p0[2]])
                                p1_proj = np.array([p1[0], p1[2]])
                                p2_proj = np.array([p2[0], p2[2]])
                            else:  # 'z'
                                # Project onto XY plane (plan area)
                                p0_proj = np.array([p0[0], p0[1]])
                                p1_proj = np.array([p1[0], p1[1]])
                                p2_proj = np.array([p2[0], p2[1]])

                            # Calculate area of projected triangle using cross product
                            v1 = p1_proj - p0_proj
                            v2 = p2_proj - p0_proj
                            # 2D cross product magnitude = |v1_x * v2_y - v1_y * v2_x|
                            area = 0.5 * abs(v1[0] * v2[1] - v1[1] * v2[0])
                            total_area += area

                        return total_area

                    except ImportError:
                        # meshio not available, return 0
                        return 0.0

                finally:
                    # Clean up temp file
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)

        except Exception as e:
            # If anything fails, return 0 to trigger fallback
            return 0.0

    @staticmethod
    def _generate_array_map(layout: List[dict]) -> Optional[str]:
        """Generate a top-down solar array layout map and return as a base64-encoded PNG.

        Returns None if matplotlib is unavailable or the layout is empty.
        """
        if not layout:
            return None
        try:
            import base64
            from io import BytesIO

            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.patches as mpatches
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        # Extract X (car length) and Z (car width) for a top-down view.
        # Shellpower coords: X = forward, Y = up, Z = right.
        xs = np.array([c["position"][0] for c in layout])
        zs = np.array([c["position"][2] for c in layout])
        normals_y = np.array([c["normal"][1] for c in layout])

        cell_w, cell_h = 0.125, 0.125

        fig, ax = plt.subplots(figsize=(11, 5))
        fig.patch.set_facecolor("#111827")
        ax.set_facecolor("#1f2937")

        ny_min, ny_range = float(normals_y.min()), max(float(normals_y.max() - normals_y.min()), 1e-4)
        for x, z, ny in zip(xs, zs, normals_y):
            t = (ny - ny_min) / ny_range          # 0 = most tilted, 1 = flattest
            color = (0.9 - 0.5 * t, 0.55 + 0.4 * t, 0.1)  # orange → green
            rect = mpatches.FancyBboxPatch(
                (x - cell_w / 2, z - cell_h / 2),
                cell_w, cell_h,
                boxstyle="square,pad=0",
                facecolor=color,
                edgecolor="#374151",
                linewidth=0.4,
            )
            ax.add_patch(rect)

        pad = 0.25
        ax.set_xlim(xs.min() - pad, xs.max() + pad)
        ax.set_ylim(zs.min() - pad, zs.max() + pad)
        ax.set_aspect("equal")
        ax.set_xlabel("Car Length →  (m)", color="#d1d5db", fontsize=10)
        ax.set_ylabel("Car Width  (m)", color="#d1d5db", fontsize=10)
        ax.set_title(f"Solar Array Layout — {len(layout)} cells", color="#f9fafb", fontsize=12, pad=8)
        ax.tick_params(colors="#9ca3af")
        for spine in ax.spines.values():
            spine.set_edgecolor("#374151")
        ax.grid(True, alpha=0.12, color="#6b7280")

        flagged = [
            (c["position"][0], c["position"][2])
            for c in layout
            if c.get("over_curvature_limit")
        ]
        if flagged:
            fx, fz = zip(*flagged)
            ax.scatter(fx, fz, s=28, color="#dc2626", edgecolors="#ffffff",
                       linewidths=0.6, zorder=5)

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("ascii")

    @staticmethod
    def _export_shellpower_mesh(
        simulation: "lc.Simulation",
        body_surfaces: List[str],
        project: Any,
        out_obj_path: "Path",
        callback: "StatusCallback",
    ) -> bool:
        """
        Download Luminary surface VTU (from the latest solution), filter to
        car-body triangles only, apply coordinate transform, and write an OBJ
        for shellpower-cli.

        Coordinate transform maps Luminary (X_fwd, Y_side, Z_up) to
        Shellpower (X_fwd, Y_up, Z_right):
            new_x =  x
            new_y =  z
            new_z = -y
        Vertices are then shifted so min(x)=0 and min(z)=0.

        Parameters
        ----------
        simulation : lc.Simulation
            Completed simulation — latest solution is used to download surface VTU.
        body_surfaces : list of str
            Luminary surface names to INCLUDE (e.g. "0/bound/car_body").
            VTU files for any other surface (farfield, floor, wheels) are skipped.
        project : lc.Project
            Project object used to retrieve mesh metadata for index→name mapping.
        out_obj_path : Path
            Output path for the OBJ file.
        callback : StatusCallback
            Function to call with log messages.

        Returns True on success, False on any failure (caller skips Shellpower).
        """
        try:
            import meshio
            import numpy as np
        except ImportError as exc:
            callback(f"Shellpower mesh export skipped (missing dependency): {exc}")
            return False

        # Build an index→boundary-name map from the mesh metadata so we can
        # identify each numbered VTU file ("..._N_0.vtu") by its Luminary name.
        # Falls back to an empty map (all files included) on any error.
        index_to_name: dict = {}
        try:
            for m in project.list_meshes():
                if m.id == simulation.mesh_id:
                    meta = m.get_metadata()
                    boundaries = meta.zones[0].boundaries
                    # VTU files use 1-based surface indices (_1_0, _2_0, …)
                    for i, b in enumerate(boundaries):
                        index_to_name[i + 1] = b.name
                    callback(
                        f"Shellpower: mesh has {len(boundaries)} boundaries: "
                        f"{[b.name for b in boundaries]}"
                    )
                    break
        except Exception as exc:
            callback(f"Shellpower: could not load mesh metadata ({exc}); will include all surfaces")

        body_set = set(body_surfaces)

        def _vtu_is_body(vtu_path: "Path") -> bool:
            """Return True if this VTU file corresponds to a body surface."""
            if not index_to_name:
                # No metadata — include everything (old behaviour)
                return True
            # Extract the surface index from "…_sol-<uuid>_<N>_<M>.vtu"
            stem = vtu_path.stem  # e.g. "surface_solution_sol-abc_5_0"
            parts = stem.rsplit("_", 2)  # ["surface_solution_sol-abc", "5", "0"]
            if len(parts) < 2:
                return True  # Can't parse — include
            try:
                surf_idx = int(parts[-2])
            except ValueError:
                return True
            name = index_to_name.get(surf_idx)
            if name is None:
                return True  # Index not in map — include to be safe
            return name in body_set

        # Get latest solution and download surface VTU tar
        try:
            solutions = simulation.list_solutions()
            if not solutions:
                callback("Shellpower mesh export: simulation has no solutions yet")
                return False
            solution = solutions[-1]
            with tempfile.TemporaryDirectory() as tdir:
                tdir_path = Path(tdir)
                with solution.download_surface_data() as tf:
                    tf.extractall(tdir_path)
                vtu_files = sorted(tdir_path.rglob("*.vtu"))
                if not vtu_files:
                    callback("Shellpower mesh export: no VTU files in surface data download")
                    return False

                callback(f"Shellpower: VTU files in tar: {[f.stem for f in vtu_files]}")

                all_points_list: list = []
                all_tris_list: list = []
                point_offset = 0

                for vtu_file in vtu_files:
                    if not _vtu_is_body(vtu_file):
                        callback(f"Shellpower: excluding non-body surface {vtu_file.stem}")
                        continue

                    try:
                        md = meshio.read(str(vtu_file))
                    except Exception as exc:
                        callback(f"Shellpower: skipping {vtu_file.name}: {exc}")
                        continue

                    pts = md.points

                    g_idx = 0
                    for blk in md.cells:
                        for _, face in enumerate(blk.data):
                            if blk.type == "triangle":
                                all_tris_list.append(face + point_offset)
                            g_idx += 1

                    all_points_list.append(pts)
                    point_offset += len(pts)

        except Exception as exc:
            callback(f"Shellpower mesh export failed (VTU download/read): {exc}")
            return False

        if not all_points_list or not all_tris_list:
            callback("Shellpower mesh export: no triangles found after filtering")
            return False

        points = np.vstack(all_points_list)
        triangles = all_tris_list

        if not triangles:
            callback("Shellpower mesh export: no triangles found after filtering")
            return False

        tris = np.array(triangles, dtype=np.int32)
        callback(
            f"Shellpower body mesh: {len(tris)} triangles, {len(points)} vertices"
        )

        # Coordinate transform: Luminary (X_fwd, Y_side, Z_up) -> Shellpower (X_fwd, Y_up, Z_right)
        new_verts = np.empty_like(points)
        new_verts[:, 0] =  points[:, 0]   # X stays
        new_verts[:, 1] =  points[:, 2]   # Y_new = Z_luminary (up)
        new_verts[:, 2] = -points[:, 1]   # Z_new = -Y_luminary

        # Shift so min(x) = 0 and min(z) = 0
        new_verts[:, 0] -= new_verts[:, 0].min()
        new_verts[:, 2] -= new_verts[:, 2].min()

        # Ensure face normals point outward from the mesh centroid.
        # CFD surface meshes often have normals pointing into the fluid domain
        # (away from the solid), but the VTU winding may be inverted relative
        # to what the CLI cross-product expects.  For each face, if the computed
        # normal points toward the centroid (inward), swap v1↔v2 to flip it.
        centroid = new_verts.mean(axis=0)
        v0 = new_verts[tris[:, 0]]
        v1 = new_verts[tris[:, 1]]
        v2 = new_verts[tris[:, 2]]
        face_normals = np.cross(v1 - v0, v2 - v0)           # (N, 3)
        face_centers = (v0 + v1 + v2) / 3.0
        inward = np.sum(face_normals * (face_centers - centroid), axis=1) < 0
        tris[inward] = tris[inward][:, [0, 2, 1]]            # flip winding
        n_flipped = int(inward.sum())
        if n_flipped:
            callback(f"Shellpower: flipped {n_flipped} inward-facing triangles")

        # Recompute normals after flip for diagnostics
        v0d = new_verts[tris[:, 0]]
        v1d = new_verts[tris[:, 1]]
        v2d = new_verts[tris[:, 2]]
        fn = np.cross(v1d - v0d, v2d - v0d)
        fn_len = np.linalg.norm(fn, axis=1, keepdims=True)
        fn_len = np.where(fn_len < 1e-10, 1.0, fn_len)
        fn /= fn_len
        ny = fn[:, 1]
        callback(
            f"Shellpower OBJ bounds: X=[{new_verts[:,0].min():.2f}, {new_verts[:,0].max():.2f}] "
            f"Y=[{new_verts[:,1].min():.2f}, {new_verts[:,1].max():.2f}] "
            f"Z=[{new_verts[:,2].min():.2f}, {new_verts[:,2].max():.2f}]"
        )
        callback(
            f"Shellpower normal.y: min={ny.min():.3f} max={ny.max():.3f} "
            f"faces_ny>0.5: {int((ny>0.5).sum())} "
            f"faces_ny>0.883: {int((ny>0.883).sum())} "
            f"faces_ny<0: {int((ny<0).sum())}"
        )

        # Write OBJ
        out_obj_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with out_obj_path.open("w") as f:
                f.write("# shellpower mesh export\n")
                for v in new_verts:
                    f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
                for tri in tris:
                    f.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")
        except OSError as exc:
            callback(f"Shellpower mesh export failed (write): {exc}")
            return False

        callback(
            f"Shellpower OBJ exported: {out_obj_path.name} "
            f"({len(tris)} triangles, {len(new_verts)} vertices)"
        )
        return True

    @staticmethod
    def _calculate_wetted_area(
        simulation: lc.Simulation,
        template: lc.SimulationTemplate,
        body_surfaces: List[str],
        wheel_surfaces: Optional[List[str]] = None,
        area_output_created: bool = False,
        callback: Optional[StatusCallback] = None,
    ) -> float:
        """
        Calculate wetted area from output definition.

        Note: The Luminary SDK currently doesn't expose a method to download
        output definition results (area, custom outputs, etc.). The SDK only has:
        - simulation.download_surface_output() - for on-demand force/moment computation
        - simulation.download_global_residuals() - for residual data

        Output definitions created via template.create_output_definition() are
        visible in the Luminary Cloud UI but cannot be downloaded via SDK yet.

        Parameters
        ----------
        simulation : lc.Simulation
            Completed simulation object
        template : lc.SimulationTemplate
            Simulation template with output definitions
        body_surfaces : List[str]
            List of body surface names
        wheel_surfaces : Optional[List[str]]
            List of wheel surface names (if rotating wheels enabled)
        area_output_created : bool
            Whether the area output definition was successfully created
        callback : Optional[StatusCallback]
            Callback for logging messages

        Returns
        -------
        float
            Wetted area in m² (returns 0.0 - SDK limitation)
        """
        if callback:
            callback("Note: Wetted area download not supported by SDK - returning 0")
            callback("(Area output is visible in Luminary Cloud UI)")

        # SDK limitation: Cannot download output definition results
        # Would need: simulation.download_output_definition_results(output_id)
        # Or: simulation.download_output(output_id)
        # Neither method exists in SDK v0.22.3
        return 0.0

    @staticmethod
    def _calculate_center_of_pressure(
        simulation: lc.Simulation,
        body_surfaces: List[str],
    ) -> Dict[str, Any]:
        """
        Calculate center of pressure for the completed simulation.

        Returns dictionary with:
            - cop_x, cop_y, cop_z: Center of pressure coordinates (m)
            - total_force_x, total_force_y, total_force_z: Force components (N)
            - force_magnitude: Total force magnitude (N)
            - force_dir_x, force_dir_y, force_dir_z: Force direction unit vector
            - moment_x, moment_y, moment_z: Moments about origin (N·m)
        """
        try:
            import pandas as pd

            # Get reference values
            ref_vals = simulation.get_parameters().reference_values

            # Use global frame for CoP calculation (not body frame which may be rotated)
            frame_id = "global_frame_id"

            # Download force components (average last 250 iterations)
            force_components = []
            for direction in [Vector3(1, 0, 0), Vector3(0, 1, 0), Vector3(0, 0, 1)]:
                with simulation.download_surface_output(
                    QuantityType.TOTAL_FORCE,
                    body_surfaces,
                    reference_values=ref_vals,
                    calculation_type=CalculationType.AGGREGATE,
                    frame_id=frame_id,
                    force_direction=direction,
                ) as stream:
                    df = pd.read_csv(stream, index_col="Iteration index")
                    df = df.drop(["Time step", "Physical time"], axis=1, errors='ignore')
                    avg_force = df.tail(250).iloc[:, 0].mean()
                    force_components.append(avg_force)

            total_force = np.array(force_components)
            force_magnitude = np.linalg.norm(total_force)

            # Download moment components about origin
            moment_components = []
            for direction in [Vector3(1, 0, 0), Vector3(0, 1, 0), Vector3(0, 0, 1)]:
                with simulation.download_surface_output(
                    QuantityType.TOTAL_MOMENT,
                    body_surfaces,
                    reference_values=ref_vals,
                    calculation_type=CalculationType.AGGREGATE,
                    frame_id=frame_id,
                    moment_center=Vector3(0, 0, 0),
                    force_direction=direction,
                ) as stream:
                    df = pd.read_csv(stream, index_col="Iteration index")
                    df = df.drop(["Time step", "Physical time"], axis=1, errors='ignore')
                    avg_moment = df.tail(250).iloc[:, 0].mean()
                    moment_components.append(avg_moment)

            total_moment = np.array(moment_components)

            # Calculate center of pressure: r_cp = (F × M) / |F|^2
            if force_magnitude > 1e-10:
                force_cross_moment = np.cross(total_force, total_moment)
                cop = force_cross_moment / (force_magnitude ** 2)
                # Calculate force direction (unit vector)
                force_direction = total_force / force_magnitude
            else:
                cop = np.array([0, 0, 0])
                force_direction = np.array([0, 0, 0])

            return {
                "cop_x": float(cop[0]),
                "cop_y": float(cop[1]),
                "cop_z": float(cop[2]),
                "total_force_x": float(total_force[0]),
                "total_force_y": float(total_force[1]),
                "total_force_z": float(total_force[2]),
                "force_magnitude": float(force_magnitude),
                "force_dir_x": float(force_direction[0]),
                "force_dir_y": float(force_direction[1]),
                "force_dir_z": float(force_direction[2]),
                "moment_x": float(total_moment[0]),
                "moment_y": float(total_moment[1]),
                "moment_z": float(total_moment[2]),
            }

        except Exception as exc:
            # Return default values if CoP calculation fails, with error message
            return {
                "cop_x": 0.0,
                "cop_y": 0.0,
                "cop_z": 0.0,
                "total_force_x": 0.0,
                "total_force_y": 0.0,
                "total_force_z": 0.0,
                "force_magnitude": 0.0,
                "force_dir_x": 0.0,
                "force_dir_y": 0.0,
                "force_dir_z": 0.0,
                "moment_x": 0.0,
                "moment_y": 0.0,
                "moment_z": 0.0,
                "cop_error": str(exc),
            }

    @staticmethod
    def _fetch_force_results(
        simulation: lc.Simulation,
        template: lc.SimulationTemplate,
        ref_area: float,
        ref_velocity: float,
        ref_density: float = 1.225,
        project: Optional[lc.Project] = None,
        force_surfaces: Optional[List[str]] = None,
        callback: Optional[StatusCallback] = None,
    ) -> Dict[str, float]:
        """Fetch force output values and calculate coefficients."""
        results = {}

        def _log(msg: str) -> None:
            if callback:
                callback(msg)

        try:
            import io
            import csv
            import pandas as pd
            from luminarycloud.enum import QuantityType, CalculationType

            if not project:
                results["error"] = "Project not provided"
                return results

            # Use provided surfaces (body + wheels), or try to infer them as fallback
            if not force_surfaces:
                # Get the mesh to find body surfaces
                mesh = None
                for m in project.list_meshes():
                    if m.id == simulation.mesh_id:
                        mesh = m
                        break

                if not mesh:
                    results["error"] = "Mesh not found"
                    return results

                # Get boundary names from mesh metadata
                metadata = mesh.get_metadata()
                boundaries = metadata.zones[0].boundaries
                all_surfaces = [b.name for b in boundaries]

                # Filter to body surfaces (exclude farfield/floor - this is a fallback heuristic)
                force_surfaces = [s for s in all_surfaces if 'BC_13' not in s and 'BC_14' not in s]

            if not force_surfaces:
                results["error"] = "No body surfaces found"
                return results

            # Download force outputs using download_surface_output with force_direction
            # Map force descriptions to result keys and parameters
            drag_vector = Vector3(-1, 0, 0)
            force_downloads = [
                ("force_x", QuantityType.TOTAL_FORCE, drag_vector),
                ("viscous_drag", QuantityType.FRICTION_FORCE, drag_vector),
                ("pressure_drag", QuantityType.PRESSURE_FORCE, drag_vector),
                ("force_y", QuantityType.TOTAL_FORCE, Vector3(0, 1, 0)),
                ("force_z", QuantityType.TOTAL_FORCE, Vector3(0, 0, 1)),
            ]

            for result_key, quantity_type, direction in force_downloads:
                try:
                    with simulation.download_surface_output(
                        quantity_type=quantity_type,
                        surface_ids=force_surfaces,
                        calculation_type=CalculationType.AGGREGATE,
                        frame_id="global_frame_id",
                        force_direction=direction,
                    ) as dl:
                        content = dl.read()
                        # Parse CSV and average last 250 iterations
                        df = pd.read_csv(io.StringIO(content), index_col="Iteration index")
                        df = df.drop(["Time step", "Physical time"], axis=1, errors='ignore')
                        # Average the last 250 iterations
                        avg_force = df.tail(250).iloc[:, 0].mean()
                        results[result_key] = float(avg_force)
                except Exception as e:
                    # If this force type fails, log error and continue with others
                    _log(f"Warning: Failed to fetch {result_key}: {e}")
                    results[result_key] = 0.0

            # Calculate force coefficients: C = F / (0.5 * rho * V^2 * A)
            q_inf = 0.5 * ref_density * ref_velocity ** 2 * ref_area
            if q_inf > 0:
                results["coeff_x"] = results.get("force_x", 0) / q_inf
                results["coeff_y"] = results.get("force_y", 0) / q_inf
                results["coeff_z"] = results.get("force_z", 0) / q_inf

        except Exception as exc:
            # If fetching fails, return error
            results["error"] = str(exc)

        return results

    def _fetch_failure_details(self, simulation: lc.Simulation) -> Optional[str]:
        """Retrieve workflow log snippets for debugging failed simulations."""
        try:
            workflow_id = simulation._get_workflow_id()
        except Exception as exc:
            return f"Could not get workflow ID: {exc}"
        if not workflow_id:
            return None
        try:
            job = pipelines_api.get_pipeline_job(workflow_id)
        except Exception as exc:
            return f"Workflow {workflow_id} exists but details could not be retrieved: {exc}"
        try:
            logs = job.logs()
        except Exception:
            logs = []
        lines = []
        for line in logs[-5:]:
            try:
                timestamp = line.timestamp.isoformat()
                lines.append(f"[{timestamp}] {line.message}")
            except Exception:
                continue
        snippet = " | ".join(lines)
        if snippet:
            return f"Pipeline job {workflow_id} status {job.status}. Recent logs: {snippet}"
        return f"Pipeline job {workflow_id} status {job.status}. No recent logs available."
