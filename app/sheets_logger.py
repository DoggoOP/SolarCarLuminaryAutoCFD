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

        # Set up headers
        headers = [
            "Timestamp",
            "Job Name",
            "Simulation ID",
            "Force X (N)",
            "Coefficient X",
            "Force Y (N)",
            "Coefficient Y",
            "Force Z (N)",
            "Coefficient Z",
            "Convergence Status",
            "Max Iterations",
            "Wind Speed (m/s)",
            "Frontal Area (m²)",
            "Luminary Link",
        ]
        self._sheet.update("A1:N1", [headers])

        # Format header row
        self._sheet.format(
            "A1:N1",
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

        row_data = [
            timestamp,
            job_name,
            simulation_id,
            force_results.get("force_x", "N/A"),
            force_results.get("coeff_x", "N/A"),
            force_results.get("force_y", "N/A"),
            force_results.get("coeff_y", "N/A"),
            force_results.get("force_z", "N/A"),
            force_results.get("coeff_z", "N/A"),
            convergence_info.get("status", "Unknown"),
            convergence_info.get("iterations", "N/A"),
            wind_speed,
            frontal_area,
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
