from __future__ import annotations

from dataclasses import dataclass, field

from cpa_inspector.models import AppSettings, ConnectionProfile, CredentialRecord


@dataclass
class AppState:
    current_profile: ConnectionProfile | None = None
    credentials: list[CredentialRecord] = field(default_factory=list)
    settings: AppSettings = field(default_factory=AppSettings)
    connected: bool = False
