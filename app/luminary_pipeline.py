from __future__ import annotations

import copy
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import luminarycloud as lc
from luminarycloud.enum import QuantityType, ResidualType, CalculationType
from luminarycloud.meshing import MeshGenerationParams
from luminarycloud.outputs import ForceOutputDefinition, ResidualOutputDefinition
from luminarycloud.params.geometry import shapes as geom_shapes
from luminarycloud.pipelines import api as pipelines_api
from luminarycloud.types import Vector3

from .config import Settings
from .sheets_logger import SheetsLogger

StatusCallback = Callable[[str], None]
CancellationCheck = Callable[[], bool]


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
    farfield_multiplier: float = 25.0
    farfield_padding: float = 0.0
    farfield_center_override: Optional[Tuple[float, float, float]] = None
    body_surfaces: Optional[Sequence[str]] = None
    floor_surfaces: Optional[Sequence[str]] = None
    farfield_surfaces: Optional[Sequence[str]] = None
    ground_speed: float = 24.59  # Vehicle forward speed for moving floor (m/s)
    frontal_area_override: Optional[float] = None  # Manual frontal area (m²) - overrides calculation


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
        ground_speed: float = 24.59,
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

        car_bc = self._build_wall_bc(
            wall_template,
            list(body_surfaces),
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
        mach = max(farfield_speed / sound_speed, 1e-4)
        farfield_bc.setdefault("farfieldMachNumber", {})["value"] = mach

        physics["boundaryConditionsFluid"] = other_bcs + [car_bc, floor_bc, farfield_bc]

        uniform_v = physics.setdefault("initializationFluid", {}).setdefault("uniformV", {})
        _set_vector(uniform_v, farfield_vector)
        # Use motion frame for moving floor (always at constant ground speed, not wind speed)
        self._attach_floor_motion(payload, floor_surfaces, ground_speed)
        self._normalize_physics_metadata(payload)
        amr = payload.setdefault("adaptiveMeshRefinement", {})
        amr["meshingMethod"] = "MESH_METHOD_AUTO"
        amr.setdefault("target_cv_millions", {})["value"] = 10
        return payload

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

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[lc.Client] = None
        self._template_builder = SimulationTemplateBuilder(settings.base_sim_template_path)
        self._sheets_logger: Optional[SheetsLogger] = SheetsLogger.from_env()

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
        body_surfaces: Sequence[str],
        callback: StatusCallback,
    ) -> None:
        """Configure convergence criteria and force outputs via API after template creation."""
        callback("Setting up stopping conditions and force outputs...")

        # Set general stopping conditions
        template.update_general_stopping_conditions(
            max_iterations=7500,
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
        # In global frame (body frame for stationary vehicle):
        # DRAG = force along x-axis (backward = positive drag)
        # SIDEFORCE = force along y-axis (lateral)
        # LIFT = force along z-axis (upward = positive lift)
        callback("Creating force and area output definitions...")
        force_outputs = [
            ("Drag (Fx)", QuantityType.DRAG),
            ("Side Force (Fy)", QuantityType.SIDEFORCE),
            ("Lift (Fz)", QuantityType.LIFT),
        ]

        for name, quantity in force_outputs:
            try:
                force_def = ForceOutputDefinition(
                    name=name,
                    quantity=quantity,
                    surfaces=list(body_surfaces),
                    # Use global frame (which is the body frame for stationary vehicle)
                    reference_frame_id="global_frame_id",
                )
                template.create_output_definition(force_def)
                callback(f"  Created force output: {name}")
            except Exception as exc:
                callback(f"  Warning: Could not create force output for {name}: {exc}")
                # Continue even if one fails

        # Area is a built-in output in Luminary, no need to create custom output definition
        callback("  Area output is built-in to Luminary")

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
        length = max(dims[1] * config.farfield_multiplier, dims[1] + 0.1)
        padding = config.farfield_padding
        floor_z = min(bbox_min[2], bbox_max[2]) - 0.001 - padding
        z_height = max(dims[2] * config.farfield_multiplier, dims[2] + 0.05)
        z_max = floor_z + z_height + padding
        min_corner = (
            center[0] - width / 2 - padding,
            center[1] - length / 2 - padding,
            floor_z,
        )
        max_corner = (
            center[0] + width / 2 + padding,
            center[1] + length / 2 + padding,
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
        mesh_params = MeshGenerationParams(
            geometry_id=geometry.id,
            min_size=config.mesh_min_size,
            max_size=config.mesh_max_size,
            add_refinement=True,
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

        remaining_exclude = farfield_surfaces + floor_surfaces
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

        callback(
            "Using body surfaces: "
            f"{body_surfaces}; floor surfaces: {floor_surfaces}; farfield surfaces: {farfield_surfaces}."
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
            ground_speed=config.ground_speed,
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

        # Set up stopping conditions and force outputs via API
        physics_id = "m3lahi1ckjf8ustjtjedwtxs1es8sre8"
        self._setup_stopping_conditions(template, physics_id, body_surfaces, callback)

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
            ref_area=frontal_area,
            ref_velocity=config.farfield_speed,
            project=project,
            body_surfaces=body_surfaces,
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
        callback(f"Calculating wetted area using {len(body_surfaces)} body surfaces...")
        wetted_area = self._calculate_wetted_area(simulation, template, body_surfaces, callback)
        force_results["wetted_area"] = wetted_area
        if wetted_area > 0:
            callback(f"✓ Wetted area: {wetted_area:.4f} m²")
        else:
            callback("⚠ Wetted area returned 0 - check error above")

        # Calculate CdA and CdW
        cd = force_results.get("coeff_x", 0)
        force_results["cd_a"] = cd * frontal_area  # Cd × frontal area
        force_results["cd_w"] = cd * wetted_area if wetted_area > 0 else 0  # Cd × wetted area

        # Log results to Google Sheets if configured
        if self._sheets_logger:
            try:
                callback("Logging results to Google Sheets...")
                convergence_info = {
                    "status": status.name,
                    "iterations": 7500,  # Max iterations from stopping conditions
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
            "force_results": force_results,
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
    def _calculate_wetted_area(
        simulation: lc.Simulation,
        template: lc.SimulationTemplate,
        body_surfaces: List[str],
        callback: Optional[StatusCallback] = None,
    ) -> float:
        """
        Get wetted area from Luminary's built-in area output.

        Parameters
        ----------
        simulation : lc.Simulation
            Completed simulation object
        template : lc.SimulationTemplate
            Simulation template (not used, kept for compatibility)
        body_surfaces : List[str]
            List of body surface names
        callback : Optional[StatusCallback]
            Callback for logging messages

        Returns
        -------
        float
            Total wetted area in m²
        """
        try:
            import pandas as pd

            # Download area output for body surfaces using built-in AREA quantity type
            with simulation.download_surface_output(
                QuantityType.AREA,
                body_surfaces,
                calculation_type=CalculationType.AGGREGATE,
            ) as stream:
                area_df = pd.read_csv(stream, index_col="Iteration index")

            # Get the last value (total wetted area)
            wetted_area = area_df.iloc[-1, -1]
            return float(wetted_area)

        except Exception as exc:
            if callback:
                callback(f"Error: Wetted area calculation failed: {exc}")
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

            # Download force components (average last 10 iterations)
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
                    avg_force = df.tail(10).iloc[:, 0].mean()
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
                    avg_moment = df.tail(10).iloc[:, 0].mean()
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
        ref_area: float,
        ref_velocity: float,
        ref_density: float = 1.225,
        project: Optional[lc.Project] = None,
        body_surfaces: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """Fetch force output values and calculate coefficients."""
        results = {}

        try:
            import io
            import csv
            from luminarycloud.enum import QuantityType, CalculationType

            if not project:
                results["error"] = "Project not provided"
                return results

            # Use provided body surfaces, or try to infer them as fallback
            if not body_surfaces:
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
                body_surfaces = [s for s in all_surfaces if 'BC_13' not in s and 'BC_14' not in s]

            if not body_surfaces:
                results["error"] = "No body surfaces found"
                return results

            # Download force outputs
            # In global frame: DRAG=Fx (along x), SIDEFORCE=Fy (along y), LIFT=Fz (along z)
            force_types = [
                (QuantityType.DRAG, "force_x"),        # Drag is force along x-axis
                (QuantityType.SIDEFORCE, "force_y"),   # Side force is force along y-axis
                (QuantityType.LIFT, "force_z"),        # Lift is force along z-axis
            ]

            for quantity_type, result_key in force_types:
                try:
                    with simulation.download_surface_output(
                        quantity_type=quantity_type,
                        surface_ids=body_surfaces,
                        calculation_type=CalculationType.AGGREGATE,
                        frame_id="global_frame_id"
                    ) as dl:
                        content = dl.read()
                        # Parse CSV and get last value
                        reader = csv.DictReader(io.StringIO(content))
                        last_row = None
                        for row in reader:
                            last_row = row
                        if last_row:
                            # Get the force column name (should be the last column)
                            force_col = list(last_row.keys())[-1]
                            results[result_key] = float(last_row[force_col])
                except Exception as e:
                    # If this force type fails, continue with others
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
