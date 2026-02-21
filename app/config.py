from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration sourced from environment variables."""

    luminary_api_key: str = Field(..., alias="LUMINARY_API_KEY")
    luminary_project_name: str = Field(
        "AutoCFD Solar Car", alias="LUMINARY_PROJECT_NAME"
    )
    default_farfield_speed: float = Field(
        24.59, alias="DEFAULT_FARFIELD_SPEED", description="m/s"
    )
    base_sim_template_path: Path = Field(
        Path("data/base_simulation_params.json"),
        alias="BASE_SIM_TEMPLATE_PATH",
    )
    sound_speed: float = Field(340.29, alias="SPEED_OF_SOUND", description="m/s")
    uploads_dir: Path = Field(Path("uploads"), alias="UPLOADS_DIR")

    # Google Sheets Integration (optional)
    google_sheets_credentials: Optional[str] = Field(
        None, alias="GOOGLE_SHEETS_CREDENTIALS"
    )
    google_sheets_spreadsheet_id: Optional[str] = Field(
        None, alias="GOOGLE_SHEETS_SPREADSHEET_ID"
    )

    # Shellpower CLI integration (optional)
    shellpower_cli_path: Optional[str] = Field(
        None, alias="SHELLPOWER_CLI_PATH",
        description="Path to shellpower-cli binary. Feature disabled if not set.",
    )
    shellpower_target_area: float = Field(
        6.0, alias="SHELLPOWER_TARGET_AREA", description="Default target array area (m²)"
    )
    shellpower_enable_daily_sim: bool = Field(
        True, alias="SHELLPOWER_ENABLE_DAILY_SIM"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    def ensure_paths(self) -> None:
        """Make sure required directories exist."""
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    def farfield_defaults(self) -> Tuple[float, float]:
        """Convenience tuple of (speed, sound_speed)."""
        return self.default_farfield_speed, self.sound_speed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_paths()
    return settings
