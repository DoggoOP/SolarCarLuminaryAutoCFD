"""Google Sheets integration for logging simulation results."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

import gspread
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
            # Link
            "Luminary Link",
        ]
        self._sheet.update("A1:AG1", [headers])

        # Format header row
        self._sheet.format(
            "A1:AG1",
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
            # Link
            luminary_link,
        ]

        # Append the row
        self._sheet.append_row(row_data, value_input_option="USER_ENTERED")

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
