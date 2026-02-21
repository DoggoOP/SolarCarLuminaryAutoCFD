"""Unit tests for the Shellpower OBJ mesh export helper."""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Minimal fake meshio data structures
# ---------------------------------------------------------------------------

class FakeCellBlock:
    def __init__(self, cell_type, data):
        self.type = cell_type
        self.data = np.array(data, dtype=np.int32)


class FakeMeshioMesh:
    """2×1m rectangle at Z=0 in Luminary coords (X_fwd, Y_side, Z_up=0)."""
    def __init__(self):
        self.points = np.array([
            [0.0, 0.0, 0.0],   # vertex 0
            [2.0, 0.0, 0.0],   # vertex 1
            [2.0, 1.0, 0.0],   # vertex 2
            [0.0, 1.0, 0.0],   # vertex 3
        ], dtype=float)
        self.cells = [FakeCellBlock("triangle", [[0, 1, 2], [0, 2, 3]])]
        self.cell_sets = {}


# ---------------------------------------------------------------------------
# Helper: build a fake simulation whose download extracts one VTU file
# ---------------------------------------------------------------------------

def _make_fake_simulation(fake_mesh_data):
    """Return a mock lc.Simulation that produces fake surface VTU data."""
    def fake_extractall(tdir_path):
        # Write a sentinel .vtu file so the glob inside _export_shellpower_mesh finds it
        (Path(tdir_path) / "car_body.vtu").write_text("fake-vtu-content")

    fake_tf = MagicMock()
    fake_tf.__enter__ = MagicMock(return_value=fake_tf)
    fake_tf.__exit__ = MagicMock(return_value=False)
    fake_tf.extractall = fake_extractall

    fake_solution = MagicMock()
    fake_solution.download_surface_data.return_value = fake_tf

    fake_simulation = MagicMock()
    fake_simulation.list_solutions.return_value = [fake_solution]
    return fake_simulation


def _make_fake_project(simulation_mesh_id="mesh-1"):
    """Return a mock project whose list_meshes() raises (simulating no metadata)."""
    fake_project = MagicMock()
    # list_meshes returns an empty list so _export_shellpower_mesh falls back to
    # "include all" mode — safe for unit tests that use a simple fake VTU filename.
    fake_project.list_meshes.return_value = []
    return fake_project


def _run_export(fake_mesh_data, body_surfaces=None):
    from app.luminary_pipeline import LuminaryCFDPipeline

    logs = []
    cb = lambda msg: logs.append(msg)

    fake_simulation = _make_fake_simulation(fake_mesh_data)
    fake_project = _make_fake_project()

    # meshio may not be installed in the test environment.
    # Inject a fake module so the import inside _export_shellpower_mesh succeeds.
    fake_meshio = MagicMock()
    fake_meshio.read.return_value = fake_mesh_data

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "shellpower_input.obj"
        with patch.dict("sys.modules", {"meshio": fake_meshio}):
            result = LuminaryCFDPipeline._export_shellpower_mesh(
                simulation=fake_simulation,
                body_surfaces=body_surfaces or ["car_body"],
                project=fake_project,
                out_obj_path=out,
                callback=cb,
            )
        # Read content inside the with block — tempdir is deleted on exit.
        content = out.read_text() if out.exists() else None
    return result, content, logs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_export_writes_obj():
    """Successfully exports an OBJ file with the right vertex and face counts."""
    result, content, logs = _run_export(FakeMeshioMesh())
    assert result is True
    assert content is not None
    v_lines = [l for l in content.splitlines() if l.startswith("v ")]
    f_lines = [l for l in content.splitlines() if l.startswith("f ")]
    assert len(v_lines) == 4   # 4 vertices
    assert len(f_lines) == 2   # 2 triangles


def test_coord_transform_y_up():
    """Luminary Z_up (=0 for all vertices) maps to Shellpower Y (=0)."""
    result, content, _ = _run_export(FakeMeshioMesh())
    assert result is True
    lines = [l for l in content.splitlines() if l.startswith("v ")]
    for line in lines:
        parts = line.split()
        y_val = float(parts[2])  # Y is the second float after "v"
        assert y_val == pytest.approx(0.0), f"Expected Y=0, got {y_val}"


def test_min_shift_zero():
    """After export, minimum X and Z coordinates in the OBJ are shifted to 0."""
    result, content, _ = _run_export(FakeMeshioMesh())
    assert result is True
    lines = [l for l in content.splitlines() if l.startswith("v ")]
    xs = [float(l.split()[1]) for l in lines]
    zs = [float(l.split()[3]) for l in lines]
    assert min(xs) == pytest.approx(0.0)
    assert min(zs) == pytest.approx(0.0)


def test_returns_false_on_missing_dependency():
    """Returns False (and logs a message) when meshio is not available."""
    from app.luminary_pipeline import LuminaryCFDPipeline
    logs = []
    with patch.dict("sys.modules", {"meshio": None}):
        result = LuminaryCFDPipeline._export_shellpower_mesh(
            simulation=MagicMock(),
            body_surfaces=[],
            project=MagicMock(),
            out_obj_path=Path("/tmp/never.obj"),
            callback=lambda msg: logs.append(msg),
        )
    assert result is False
    assert any("skip" in m.lower() or "missing" in m.lower() or "import" in m.lower() for m in logs)
