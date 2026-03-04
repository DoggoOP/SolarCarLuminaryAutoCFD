"""Google Sheets integration for logging simulation results."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

import gspread
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.service_account import Credentials


class SheetsLogger:
    """Manages logging of CFD simulation results to Google Sheets."""

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self, credentials_path: str, spreadsheet_id: str) -> None:
        """
        Initialize Google Sheets logger.

        Parameters
        ----------
        credentials_path : str
            Path to Google service account credentials JSON file OR
            the JSON content itself as a string (for cloud deployment)
        spreadsheet_id : str
            ID of the Google Spreadsheet to write to
        """
        self.credentials_path = credentials_path
        self.spreadsheet_id = spreadsheet_id
        self._client: Optional[gspread.Client] = None
        self._sheet: Optional[gspread.Worksheet] = None
        self._creds: Optional[Credentials] = None

    def _connect(self) -> None:
        """Establish connection to Google Sheets."""
        if self._client is None:
            # Check if credentials_path is a JSON string or file path
            credentials_str = self.credentials_path.strip()
            if credentials_str.startswith('{'):
                # It's a JSON string (for cloud deployment)
                credentials_info = json.loads(credentials_str)
                creds = Credentials.from_service_account_info(
                    credentials_info,
                    scopes=self.SCOPES,
                )
            else:
                # It's a file path (for local development)
                creds = Credentials.from_service_account_file(
                    self.credentials_path,
                    scopes=self.SCOPES,
                )
            self._creds = creds
            self._client = gspread.authorize(creds)

        if self._sheet is None:
            spreadsheet = self._client.open_by_key(self.spreadsheet_id)
            # Use the first worksheet, or create it if it doesn't exist
            try:
                self._sheet = spreadsheet.sheet1
            except gspread.exceptions.WorksheetNotFound:
                self._sheet = spreadsheet.add_worksheet(
                    title="Simulation Results",
                    rows=1000,
                    cols=20,
                )

            # Initialize headers if this is a new sheet
            self._initialize_headers()

    def _upload_image_to_drive(self, image_b64: str, filename: str) -> Optional[str]:
        """Upload a base64-encoded PNG to Google Drive and return a public view URL.

        Uses the Drive REST API via an AuthorizedSession so no extra packages are needed.
        Returns None if the upload fails for any reason.
        """
        self._connect()
        if self._creds is None:
            return None
        try:
            session = AuthorizedSession(self._creds)
            png_bytes = base64.b64decode(image_b64)
            metadata = json.dumps({"name": filename, "mimeType": "image/png"})
            resp = session.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                files={
                    "metadata": ("metadata", metadata, "application/json"),
                    "file": (filename, png_bytes, "image/png"),
                },
            )
            resp.raise_for_status()
            file_id = resp.json()["id"]
            # Share with anyone who has the link (read-only)
            session.post(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                json={"role": "reader", "type": "anyone"},
            )
            return f"https://drive.google.com/uc?id={file_id}&export=view"
        except Exception:
            return None

    def _initialize_headers(self) -> None:
        """Set up column headers if sheet is empty."""
        if self._sheet is None:
            return

        # Check if headers already exist
        try:
            existing_headers = self._sheet.row_values(1)
            if existing_headers and len(existing_headers) > 0:
                return  # Headers already exist
        except Exception:
            pass

        # Set up headers - organized with parameters first, then grouped results
        headers = [
            # Simulation identification and parameters
            "Timestamp",
            "Job Name",
            "Simulation ID",
            "Wind Speed (m/s)",
            "Wind Direction X",
            "Wind Direction Y",
            "Wind Direction Z",
            "Frontal Area (m²)",
            "Wetted Area (m²)",
            # Drag results grouped together
            "Drag Force (N)",
            "Viscous Drag (N)",
            "Pressure Drag (N)",
            "Drag Coefficient (Cd)",
            "CdA (Cd × Frontal Area)",
            "CdW (Cd × Wetted Area)",
            # Side force results
            "Side Force (N)",
            "Side Force Coefficient (Cy)",
            # Lift force results
            "Lift Force (N)",
            "Lift Coefficient (Cz)",
            # Center of pressure and force analysis
            "CoP X (m)",
            "CoP Y (m)",
            "CoP Z (m)",
            "Total Force Magnitude (N)",
            "Force Direction X",
            "Force Direction Y",
            "Force Direction Z",
            # Moments
            "Moment X (N·m)",
            "Moment Y (N·m)",
            "Moment Z (N·m)",
            # Convergence info
            "Convergence Status",
            "Max Iterations",
            # Solar Array (Shellpower)
            "Solar Cells (Shadow-Aware)",
            "Solar Peak Power (Shadow-Aware)",
            "Solar Daily Energy (Shadow-Aware)",
            "Solar Cells (Symmetric)",
            "Solar Peak Power (Symmetric)",
            "Solar Daily Energy (Symmetric)",
            "Solar Array Map (Shadow-Aware)",
            "Solar Array Map (Symmetric)",
            # Link
            "Luminary Link",
        ]
        self._sheet.update("A1:AN1", [headers])

        # Format header row
        self._sheet.format(
            "A1:AN1",
            {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.2, "green": 0.3, "blue": 0.8},
                "horizontalAlignment": "CENTER",
            },
        )

    def append_result(
        self,
        job_name: str,
        project_id: str,
        simulation_id: str,
        force_results: Dict[str, float],
        wind_speed: float,
        wind_direction: tuple,
        frontal_area: float,
        convergence_info: Dict[str, Any],
        shellpower_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Append simulation results to the Google Sheet.

        Parameters
        ----------
        job_name : str
            Name/label of the simulation job
        project_id : str
            Luminary Cloud project ID
        simulation_id : str
            Luminary Cloud simulation ID
        force_results : dict
            Dictionary containing drag, lift, and side force values and coefficients
        wind_speed : float
            Reference wind speed in m/s
        wind_direction : tuple
            Wind direction vector (x, y, z)
        frontal_area : float
            Reference frontal area in m²
        convergence_info : dict
            Information about simulation convergence
        """
        self._connect()

        if self._sheet is None:
            raise RuntimeError("Failed to connect to Google Sheets")

        # Extract shellpower variants when available
        variants = (shellpower_data or {}).get("variants") if shellpower_data else None
        shadow_variant: Optional[Dict[str, Any]] = None
        symmetric_variant: Optional[Dict[str, Any]] = None
        if isinstance(variants, list):
            for variant in variants:
                mode = variant.get("mode")
                if mode == "shadow" and shadow_variant is None:
                    shadow_variant = variant
                elif mode == "no_shadow" and symmetric_variant is None:
                    symmetric_variant = variant
        if shellpower_data and shadow_variant is None:
            shadow_variant = shellpower_data

        def _shellpower_metric(source: Optional[Dict[str, Any]], key: str):
            if not source:
                return "N/A"
            value = source.get(key)
            return value if value is not None else "N/A"

        # Prepare row data
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        # Use HYPERLINK formula to make link clickable in Google Sheets
        luminary_link = f'=HYPERLINK("https://app.luminarycloud.com/project/{project_id}/simulation/{simulation_id}", "View Simulation")'

        # Row data matching the reorganized header order
        row_data = [
            # Simulation identification and parameters
            timestamp,
            job_name,
            simulation_id,
            wind_speed,
            wind_direction[0],  # Wind Direction X
            wind_direction[1],  # Wind Direction Y
            wind_direction[2],  # Wind Direction Z
            frontal_area,
            force_results.get("wetted_area", "N/A"),
            # Drag results grouped together
            force_results.get("force_x", "N/A"),
            force_results.get("viscous_drag", "N/A"),
            force_results.get("pressure_drag", "N/A"),
            force_results.get("coeff_x", "N/A"),
            force_results.get("cd_a", "N/A"),
            force_results.get("cd_w", "N/A"),
            # Side force results
            force_results.get("force_y", "N/A"),
            force_results.get("coeff_y", "N/A"),
            # Lift force results
            force_results.get("force_z", "N/A"),
            force_results.get("coeff_z", "N/A"),
            # Center of pressure and force analysis
            force_results.get("cop_x", "N/A"),
            force_results.get("cop_y", "N/A"),
            force_results.get("cop_z", "N/A"),
            force_results.get("force_magnitude", "N/A"),
            force_results.get("force_dir_x", "N/A"),
            force_results.get("force_dir_y", "N/A"),
            force_results.get("force_dir_z", "N/A"),
            # Moments
            force_results.get("moment_x", "N/A"),
            force_results.get("moment_y", "N/A"),
            force_results.get("moment_z", "N/A"),
            # Convergence info
            convergence_info.get("status", "Unknown"),
            convergence_info.get("iterations", "N/A"),
            # Solar Array (Shellpower)
            _shellpower_metric(shadow_variant, "cells_placed"),
            _shellpower_metric(shadow_variant, "instant_power_w"),
            _shellpower_metric(shadow_variant, "daily_energy_wh"),
            _shellpower_metric(symmetric_variant, "cells_placed"),
            _shellpower_metric(symmetric_variant, "instant_power_w"),
            _shellpower_metric(symmetric_variant, "daily_energy_wh"),
            self._make_array_map_formula(shadow_variant, simulation_id, "shadow"),
            self._make_array_map_formula(symmetric_variant, simulation_id, "sym"),
            # Link
            luminary_link,
        ]

        # Append the row
        self._sheet.append_row(row_data, value_input_option="USER_ENTERED")

    def _make_array_map_formula(
        self,
        shellpower_source: Optional[Dict[str, Any]],
        simulation_id: str,
        suffix: str = "",
    ) -> str:
        """Upload array map to Drive and return an =IMAGE() formula, or '' if unavailable."""
        if not shellpower_source:
            return ""
        image_b64 = shellpower_source.get("array_map_b64")
        if not image_b64:
            return ""
        filename = f"solar_array_{simulation_id[:8]}"
        if suffix:
            filename += f"_{suffix}"
        filename += ".png"
        url = self._upload_image_to_drive(image_b64, filename)
        if url:
            return f'=IMAGE("{url}")'
        return ""

    @staticmethod
    def is_enabled(settings=None) -> bool:
        """Check if Google Sheets logging is enabled."""
        if settings:
            return bool(
                settings.google_sheets_credentials
                and settings.google_sheets_spreadsheet_id
            )
        return bool(
            os.getenv("GOOGLE_SHEETS_CREDENTIALS")
            and os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        )

    @classmethod
    def from_env(cls, settings=None) -> Optional[SheetsLogger]:
        """
        Create SheetsLogger from environment variables or settings.

        Parameters
        ----------
        settings : Settings, optional
            Settings object with Google Sheets configuration.
            If not provided, will try to load from environment variables.

        Returns None if credentials are not configured.

        Environment Variables / Settings Fields
        ---------------------------------------
        GOOGLE_SHEETS_CREDENTIALS / google_sheets_credentials : str
            Path to Google service account credentials JSON
        GOOGLE_SHEETS_SPREADSHEET_ID / google_sheets_spreadsheet_id : str
            Google Spreadsheet ID
        """
        if not cls.is_enabled(settings):
            return None

        if settings:
            credentials_path = settings.google_sheets_credentials or ""
            spreadsheet_id = settings.google_sheets_spreadsheet_id or ""
        else:
            credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "")
            spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")

        return cls(credentials_path, spreadsheet_id)
