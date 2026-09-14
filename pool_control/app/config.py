from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    iaqualink_email: str
    iaqualink_password: str
    iaqualink_serial: str | None
    data_dir: Path
    ha_ws_url: str
    ha_token: str
    log_level: str
    testing: bool

    @classmethod
    def from_env(cls) -> "Config":
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            ha_ws_url = "ws://supervisor/core/websocket"
            ha_token = supervisor_token
        else:
            ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
            ha_ws_url = ha_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/api/websocket"
            ha_token = os.environ.get("HA_TOKEN", "")
        serial = os.environ.get("IAQUALINK_SERIAL", "").strip() or None
        return cls(
            iaqualink_email=os.environ.get("IAQUALINK_EMAIL", ""),
            iaqualink_password=os.environ.get("IAQUALINK_PASSWORD", ""),
            iaqualink_serial=serial,
            data_dir=Path(os.environ.get("DATA_DIR", "./data")),
            ha_ws_url=ha_ws_url,
            ha_token=ha_token,
            log_level=os.environ.get("LOG_LEVEL", "info"),
            testing=os.environ.get("POOL_CONTROL_TESTING") == "1",
        )
