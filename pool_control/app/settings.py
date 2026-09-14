from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path

LOGGER = logging.getLogger(__name__)

RANGES = {
    "target": (80.0, 104.0),
    "buffer": (1.0, 10.0),
    "off_early": (0.0, 5.0),
}


@dataclass(frozen=True)
class ThermostatSettings:
    enabled: bool = False
    target: float = 100.0
    buffer: float = 3.0
    off_early: float = 0.0

    def with_changes(self, **changes) -> "ThermostatSettings":
        clean = {}
        for key, value in changes.items():
            if key == "enabled":
                clean[key] = bool(value)
            elif key in RANGES:
                low, high = RANGES[key]
                number = float(value)
                if not (low <= number <= high):
                    raise ValueError(f"{key} must be between {low:g} and {high:g}")
                clean[key] = number
            else:
                raise ValueError(f"unknown setting: {key}")
        updated = replace(self, **clean)
        # off_early >= buffer inverts the hysteresis band (off threshold at or below the
        # on threshold), which makes the heater cycle at a constant temperature.
        if updated.off_early >= updated.buffer:
            raise ValueError("Off early must be less than Buffer")
        return updated

    def to_dict(self) -> dict:
        return asdict(self)


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> ThermostatSettings:
        try:
            data = json.loads(self.path.read_text())
            return ThermostatSettings().with_changes(**data)
        except FileNotFoundError:
            return ThermostatSettings()
        except (ValueError, TypeError) as exc:
            LOGGER.warning("Ignoring unreadable settings file %s: %s", self.path, exc)
            return ThermostatSettings()

    def save(self, settings: ThermostatSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings.to_dict()))
        tmp.replace(self.path)
