from __future__ import annotations

import json
import os
from pathlib import Path

from cpa_inspector.constants import CONFIG_DIR_NAME
from cpa_inspector.models import AppSettings, ConnectionProfile


class ProfileStore:
    def __init__(
        self,
        config_path: Path | None = None,
        settings_path: Path | None = None,
    ) -> None:
        self.config_path = config_path or self.default_path()
        self.settings_path = settings_path or self.default_settings_path()

    @staticmethod
    def default_path() -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / CONFIG_DIR_NAME / "profiles.json"

    @staticmethod
    def default_settings_path() -> Path:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / CONFIG_DIR_NAME / "settings.json"

    def load_profiles(self) -> list[ConnectionProfile]:
        if not self.config_path.exists():
            return []
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        items = [ConnectionProfile.from_dict(item) for item in payload if isinstance(item, dict)]
        return [item for item in items if item.name and item.base_url]

    def save_profiles(self, profiles: list[ConnectionProfile]) -> None:
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [profile.to_dict() for profile in profiles]
            self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            # Local config writes should never crash the app.
            return

    def load_app_settings(self) -> AppSettings:
        if not self.settings_path.exists():
            return AppSettings()
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AppSettings()
        if not isinstance(payload, dict):
            return AppSettings()
        return AppSettings.from_dict(payload)

    def save_app_settings(self, settings: AppSettings) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(
                json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return
