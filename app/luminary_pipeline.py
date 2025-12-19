from __future__ import annotations

import copy
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import luminarycloud as lc
from luminarycloud.enum import QuantityType, ResidualType
from luminarycloud.meshing import MeshGenerationParams
from luminarycloud.outputs import ForceOutputDefinition, ResidualOutputDefinition
from luminarycloud.params.geometry import shapes as geom_shapes
from luminarycloud.pipelines import api as pipelines_api
from luminarycloud.types import Vector3

from .config import Settings
from .sheets_logger import SheetsLogger

StatusCallback = Callable[[str], None]


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
        # Use motion frame for moving floor
        self._attach_floor_motion(payload, floor_surfaces, farfield_speed)
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
        if not floor_surfaces:
            return
        current_motion = payload.setdefault("motionData", [])
        filtered_motion = [
            entry for entry in current_motion if entry.get("frameId") != "moving_floor_frame"
        ]
        # Moving floor velocity in x-direction at wind speed
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

        # Create force output definitions for drag, side force, and lift
        callback("Creating force output definitions...")
        force_outputs = [
            ("Drag", QuantityType.DRAG),
            ("Side Force", QuantityType.SIDEFORCE),
            ("Lift", QuantityType.LIFT),
        ]

        for name, quantity in force_outputs:
            try:
                force_def = ForceOutputDefinition(
                    name=name,
                    quantity=quantity,
                    surfaces=list(body_surfaces),
                    reference_frame_id="body_frame_id",
                )
                template.create_output_definition(force_def)
                callback(f"  Created force output: {name}")
            except Exception as exc:
                callback(f"  Warning: Could not create force output for {name}: {exc}")
                # Continue even if one fails

    def run_case(self, config: CaseConfig, callback: StatusCallback) -> dict:
        client = self._client_or_create()
        case_name = f"{config.cad_label.strip()}-{datetime.utcnow():%Y%m%d-%H%M%S}"

        project = self._ensure_project(client, config.project_name, callback)

        callback("Uploading CAD and creating geometry …")
        geometry = project.create_geometry(
            config.cad_path,
            name=f"{case_name}-geometry",
            wait=True,
        )
        callback(f"Geometry created with id={geometry.id}. Computing bounding box …")
        bbox_min, bbox_max = self._geometry_bounds(geometry)
        dims = tuple(max(bmax - bmin, 1e-4) for bmin, bmax in zip(bbox_min, bbox_max))
        center = tuple((bmin + bmax) / 2 for bmin, bmax in zip(bbox_min, bbox_max))

        # Calculate frontal area (YZ projection, assuming X is forward)
        frontal_area = self._calculate_frontal_area(bbox_min, bbox_max)
        callback(f"Calculated frontal area (YZ projection): {frontal_area:.4f} m²")
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
        geometry_ok, issues = geometry.check()
        callback(f"Geometry check returned ok={geometry_ok}. Issues: {issues}")
        if not geometry_ok:
            raise RuntimeError(
                "Geometry check failed. Please resolve the reported issues and try again."
            )

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
        if mesh_status.name != "COMPLETED":
            raise RuntimeError(f"Meshing failed with status {mesh_status.name}")

        metadata = mesh.get_metadata()
        surface_map = self._collect_surface_metadata(metadata)
        surface_names = sorted(surface_map.keys())
        callback(f"Detected surfaces: {', '.join(surface_names) or 'none'}")

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
        payload = self._template_builder.build_payload(
            body_surfaces=body_surfaces,
            floor_surfaces=floor_surfaces,
            farfield_surfaces=farfield_surfaces,
            farfield_vector=farfield_vector,
            farfield_speed=config.farfield_speed,
            sound_speed=self._settings.sound_speed,
            frontal_area=frontal_area,
        )
        tmp_params = self._template_builder.dump_payload(payload, label=case_name)
        # Debug: log the motion data being sent
        callback(f"DEBUG motionData: {json.dumps(payload.get('motionData', []), indent=2)}")
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

        callback("Launching simulation …")

        simulation = project.create_simulation(
            mesh.id,
            f"{case_name}-simulation",
            template.id,
        )
        status = simulation.wait(interval_seconds=15, print_residuals=False)
        callback(f"Simulation completed with status {status.name}.")
        if status.name != "COMPLETED":
            detail = self._fetch_failure_details(simulation)
            if detail:
                callback(f"Failure details: {detail}")
                raise RuntimeError(
                    f"Simulation terminated with status {status.name}: {detail}"
                )
            raise RuntimeError(
                f"Simulation terminated with status {status.name}. Check Luminary logs."
            )

        # Fetch force results
        callback("Fetching force results...")
        force_results = self._fetch_force_results(
            simulation,
            ref_area=frontal_area,
            ref_velocity=config.farfield_speed,
            project=project,
        )

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
                    simulation_id=simulation.id,
                    force_results=force_results,
                    wind_speed=config.farfield_speed,
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
        if "drag_force" in force_results:
            callback(
                f"Results: Drag={force_results['drag_force']:.3f}N (Cd={force_results.get('drag_coefficient', 0):.4f}), "
                f"Lift={force_results['lift_force']:.3f}N (Cl={force_results.get('lift_coefficient', 0):.4f}), "
                f"Side={force_results['sideforce']:.3f}N (Cs={force_results.get('sideforce_coefficient', 0):.4f})"
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
    def _fetch_force_results(
        simulation: lc.Simulation,
        ref_area: float,
        ref_velocity: float,
        ref_density: float = 1.225,
        project: Optional[lc.Project] = None,
    ) -> Dict[str, float]:
        """Fetch force output values and calculate coefficients."""
        results = {}

        try:
            import io
            import csv
            from luminarycloud.enum import QuantityType, CalculationType

            # Get the project if not provided
            if not project:
                # Try to get project from client
                for proj in lc.iterate_projects():
                    if proj.id == simulation.project_id:
                        project = proj
                        break

            if not project:
                results["error"] = "Project not found"
                return results

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

            # Filter to body surfaces (exclude farfield/floor which have BC_13x/BC_14x pattern)
            body_surfaces = [s for s in all_surfaces if 'BC_13' not in s and 'BC_14' not in s]

            if not body_surfaces:
                results["error"] = "No body surfaces found"
                return results

            # Download force outputs for each force type
            force_types = [
                (QuantityType.DRAG, "drag_force"),
                (QuantityType.LIFT, "lift_force"),
                (QuantityType.SIDEFORCE, "sideforce"),
            ]

            for quantity_type, result_key in force_types:
                try:
                    with simulation.download_surface_output(
                        quantity_type=quantity_type,
                        surface_ids=body_surfaces,
                        calculation_type=CalculationType.AGGREGATE,
                        frame_id="body_frame_id"
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

            # Calculate coefficients: C = F / (0.5 * rho * V^2 * A)
            q_inf = 0.5 * ref_density * ref_velocity ** 2 * ref_area
            if q_inf > 0:
                results["drag_coefficient"] = results.get("drag_force", 0) / q_inf
                results["lift_coefficient"] = results.get("lift_force", 0) / q_inf
                results["sideforce_coefficient"] = results.get("sideforce", 0) / q_inf

        except Exception as exc:
            # If fetching fails, return error
            results["error"] = str(exc)

        return results

    def _fetch_failure_details(self, simulation: lc.Simulation) -> Optional[str]:
        """Retrieve workflow log snippets for debugging failed simulations."""
        try:
            workflow_id = simulation._get_workflow_id()
        except Exception:
            return None
        if not workflow_id:
            return None
        try:
            job = pipelines_api.get_pipeline_job(workflow_id)
        except Exception:
            return f"Workflow {workflow_id} exists but details could not be retrieved."
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
