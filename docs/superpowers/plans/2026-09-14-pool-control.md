# Pool Control Add-on Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Home Assistant add-on that serves a phone-friendly pool page, controls the variable speed pump through the iAqualink WebTouch cloud protocol, and runs a spa thermostat by cycling the spa heater through Home Assistant.

**Architecture:** One Python process (FastAPI + uvicorn) with three components: an `HAClient` (Home Assistant websocket for switches, lights, temperatures), an `AqualinkClient` (login, WebTouch session, stream parser, page navigation, pump/waterfall commands), and the web app (JSON API, server-sent events, static page) plus a thermostat runner and spa session helper. All state is merged into one snapshot dict pushed to the page.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, httpx (HTTP + streaming), websockets (HA websocket), pytest + pytest-asyncio, vanilla HTML/CSS/JS (no build step). Home Assistant local add-on with Ingress.

**Spec:** `docs/superpowers/specs/2026-09-14-pool-control-design.md` (protocol details in `docs/iaqualink-webtouch-protocol.md`). Read both before starting any task.

## Global Constraints

- Python 3.12; the add-on image is `ghcr.io/home-assistant/{arch}-base-python:3.12-alpine3.20`.
- No long-lived Home Assistant token in the add-on: use `SUPERVISOR_TOKEN` and `ws://supervisor/core/websocket`. A local dev fallback reads `HA_URL` + `HA_TOKEN` env vars.
- Entity ids are fixed constants in `app/entities.py` (see spec section 2). Never hard-code them elsewhere.
- Pump preset labels and RPMs come from the stream, never from constants.
- Thermostat only ever calls `switch.turn_on/turn_off` on `switch.spa_heater`. Minimum 300 s between thermostat-initiated switches.
- Thermostat defaults: `enabled=False, target=100, buffer=3, off_early=0`. Ranges: target 80–104, buffer 1–10, off_early 0–5.
- Portal API calls use `Authorization: Bearer <IdToken>`; WebTouch init/command calls use `Authorization: <IdToken>` (no Bearer).
- All page URLs are relative (Ingress serves the app under a prefix).
- Tests run with `cd /Users/garyfowler/Projects/pool_control && python3 -m pytest -q`. Every task ends green.
- Commit after every task with a message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File Structure

```
pool_control/                      # repo root
  pool_control/                    # the add-on folder (copied to HA /addons/pool_control)
    config.yaml                    # add-on manifest (ingress, options, schema, watchdog)
    build.yaml                     # base images per arch
    Dockerfile
    run.sh                         # exports options as env vars, starts uvicorn
    requirements.txt
    app/
      __init__.py
      config.py                    # Config.from_env()
      entities.py                  # HA entity id constants
      settings.py                  # ThermostatSettings + SettingsStore (JSON in DATA_DIR)
      thermostat.py                # pure decision engine: evaluate(ThermostatInput) -> Decision
      webtouch_parser.py           # StreamParser (chunks -> NLMessage), ScreenModel
      aqualink_auth.py             # login/refresh/discover touchLink
      aqualink_client.py           # session, stream task, navigation, commands, AqualinkState
      ha_client.py                 # HA websocket client
      notifier.py                  # async broadcast used by SSE
      spa.py                       # SpaSession: start/end, session label
      thermostat_runner.py         # ThermostatRunner: wires engine to HA + settings
      main.py                      # FastAPI app, lifespan, routes, SSE, static
      static/index.html
      static/style.css
      static/app.js
  tests/
    conftest.py
    test_settings.py
    test_thermostat.py
    test_webtouch_parser.py
    test_aqualink_auth.py
    test_aqualink_client.py
    test_ha_client.py
    test_spa.py
    test_thermostat_runner.py
    test_api.py
    test_integration_live.py       # opt-in, real cloud
  pyproject.toml                   # pytest config (pythonpath = pool_control, asyncio_mode = auto)
  requirements-dev.txt
  run_local.sh                     # dev runner on the Mac
  README.md
  docs/...
```

---

### Task 1: Scaffold the add-on and the test harness

**Files:**
- Create: `pool_control/config.yaml`, `pool_control/build.yaml`, `pool_control/Dockerfile`, `pool_control/run.sh`, `pool_control/requirements.txt`, `pool_control/app/__init__.py`, `pool_control/app/config.py`, `pool_control/app/entities.py`, `pool_control/app/main.py` (health only for now), `pyproject.toml`, `requirements-dev.txt`, `.gitignore`, `tests/conftest.py`, `tests/test_api.py` (health test only)

**Interfaces:**
- Produces: `Config` dataclass with fields `iaqualink_email: str, iaqualink_password: str, iaqualink_serial: str | None, data_dir: Path, ha_ws_url: str, ha_token: str, log_level: str, testing: bool` and classmethod `Config.from_env() -> Config`.
- Produces: `entities.SWITCHES`, `entities.LIGHTS`, `entities.SENSORS`, `entities.ALL_ENTITY_IDS`.
- Produces: FastAPI `app` in `app.main` with `GET /api/health -> {"ok": true}`.

- [ ] **Step 1: Create the Python environment and dev requirements**

```bash
cd /Users/garyfowler/Projects/pool_control
cat > requirements-dev.txt <<'EOF'
-r pool_control/requirements.txt
pytest==8.3.3
pytest-asyncio==0.24.0
EOF
mkdir -p pool_control/app tests
cat > pool_control/requirements.txt <<'EOF'
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
websockets==13.1
EOF
cat > pyproject.toml <<'EOF'
[tool.pytest.ini_options]
pythonpath = ["pool_control"]
asyncio_mode = "auto"
testpaths = ["tests"]
EOF
cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.pyc
.pytest_cache/
.env
data/
EOF
python3 -m venv .venv && . .venv/bin/activate && pip install -q -r requirements-dev.txt
```

- [ ] **Step 2: Write the failing health test**

`tests/conftest.py`:
```python
import os

os.environ["POOL_CONTROL_TESTING"] = "1"
```

`tests/test_api.py`:
```python
from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `. .venv/bin/activate && python3 -m pytest tests/test_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 4: Write config, entities, and a minimal app**

`pool_control/app/__init__.py`: empty file.

`pool_control/app/config.py`:
```python
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
```

`pool_control/app/entities.py`:
```python
"""Home Assistant entity ids used by the add-on. Change here only."""

SWITCHES = {
    "filter_pump": "switch.pool_pump",
    "spa": "switch.spa_pump",
    "spa_heat": "switch.spa_heater",
    "jet_pump": "switch.jet_pump",
    "pool_heat": "switch.pool_heater",
}

LIGHTS = {
    "light_shallow": "light.pool_pool_light_shallow_end",
    "light_middle": "light.pool_pool_light_middle",
    "light_deep": "light.pool_pool_light_deep_end",
}

SENSORS = {
    "pool_temp": "sensor.pool_temp",
    "spa_temp": "sensor.spa_temp",
}

ALL_ENTITY_IDS = [*SWITCHES.values(), *LIGHTS.values(), *SENSORS.values()]
```

`pool_control/app/main.py` (minimal; replaced in Task 9):
```python
from fastapi import FastAPI

app = FastAPI(title="Pool Control")


@app.get("/api/health")
async def health():
    return {"ok": True}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `. .venv/bin/activate && python3 -m pytest tests/test_api.py -q`
Expected: `1 passed`

- [ ] **Step 6: Write the add-on manifest, Dockerfile, and run script**

`pool_control/config.yaml`:
```yaml
name: Pool Control
version: "0.1.0"
slug: pool_control
description: Simple pool and spa control page with pump speed presets and a spa thermostat
arch:
  - amd64
  - aarch64
startup: application
boot: auto
init: false
homeassistant_api: true
ingress: true
ingress_port: 8099
panel_icon: mdi:pool
panel_title: Pool
watchdog: "http://[HOST]:[PORT:8099]/api/health"
options:
  iaqualink_email: ""
  iaqualink_password: ""
  serial: ""
  log_level: info
schema:
  iaqualink_email: str
  iaqualink_password: password
  serial: str?
  log_level: list(debug|info|warning|error)?
```

`pool_control/build.yaml`:
```yaml
build_from:
  amd64: ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
  aarch64: ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
```

`pool_control/Dockerfile`:
```dockerfile
ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app ./app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
```

`pool_control/run.sh`:
```bash
#!/usr/bin/with-contenv bashio
export IAQUALINK_EMAIL="$(bashio::config 'iaqualink_email')"
export IAQUALINK_PASSWORD="$(bashio::config 'iaqualink_password')"
export IAQUALINK_SERIAL="$(bashio::config 'serial' '')"
export LOG_LEVEL="$(bashio::config 'log_level' 'info')"
export DATA_DIR=/data
cd /app
bashio::log.info "Starting Pool Control on port 8099"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --log-level "${LOG_LEVEL}"
```

Run: `chmod +x pool_control/run.sh`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Scaffold pool_control add-on, config, entities, health endpoint

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Persisted thermostat settings

**Files:**
- Create: `pool_control/app/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `ThermostatSettings` dataclass (`enabled: bool=False, target: float=100.0, buffer: float=3.0, off_early: float=0.0`) with `with_changes(**changes) -> ThermostatSettings` (validates, raises `ValueError`) and `to_dict()`.
- Produces: `SettingsStore(path: Path)` with `load() -> ThermostatSettings` and `save(settings: ThermostatSettings) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_settings.py`:
```python
import json

import pytest

from app.settings import SettingsStore, ThermostatSettings


def test_defaults():
    s = ThermostatSettings()
    assert s.to_dict() == {"enabled": False, "target": 100.0, "buffer": 3.0, "off_early": 0.0}


def test_with_changes_validates_ranges():
    s = ThermostatSettings()
    assert s.with_changes(target=95, buffer=2, off_early=1, enabled=True).to_dict() == {
        "enabled": True, "target": 95.0, "buffer": 2.0, "off_early": 1.0,
    }
    with pytest.raises(ValueError):
        s.with_changes(target=79)
    with pytest.raises(ValueError):
        s.with_changes(target=105)
    with pytest.raises(ValueError):
        s.with_changes(buffer=0.5)
    with pytest.raises(ValueError):
        s.with_changes(off_early=6)
    with pytest.raises(ValueError):
        s.with_changes(bogus=1)


def test_store_round_trip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.load() == ThermostatSettings()
    store.save(ThermostatSettings(enabled=True, target=98, buffer=4, off_early=1))
    assert store.load() == ThermostatSettings(enabled=True, target=98, buffer=4, off_early=1)
    assert json.loads((tmp_path / "settings.json").read_text())["target"] == 98


def test_store_ignores_corrupt_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    assert SettingsStore(p).load() == ThermostatSettings()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_settings.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.settings'`

- [ ] **Step 3: Implement settings**

`pool_control/app/settings.py`:
```python
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
        return replace(self, **clean)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_settings.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/settings.py tests/test_settings.py
git commit -m "Add persisted thermostat settings

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Thermostat decision engine

**Files:**
- Create: `pool_control/app/thermostat.py`
- Test: `tests/test_thermostat.py`

**Interfaces:**
- Produces: `ThermostatInput` dataclass: `enabled: bool, spa_on: bool, spa_temp: float | None, heater_on: bool, target: float, buffer: float, off_early: float, now: float, last_switch_at: float | None`.
- Produces: `Decision` dataclass: `action: str` (`"on"`, `"off"`, or `"hold"`), `status: str` (short page text), `reason: str`.
- Produces: `MIN_CYCLE_SECONDS = 300` and `evaluate(inp: ThermostatInput) -> Decision`.

- [ ] **Step 1: Write the failing tests**

`tests/test_thermostat.py`:
```python
from app.thermostat import MIN_CYCLE_SECONDS, ThermostatInput, evaluate


def make(**overrides):
    base = dict(enabled=True, spa_on=True, spa_temp=90.0, heater_on=False,
                target=100.0, buffer=3.0, off_early=0.0, now=10_000.0, last_switch_at=None)
    base.update(overrides)
    return ThermostatInput(**base)


def test_disabled_holds():
    d = evaluate(make(enabled=False))
    assert d.action == "hold" and d.status == "Thermostat off"


def test_spa_off_holds():
    d = evaluate(make(spa_on=False))
    assert d.action == "hold" and d.status == "Spa is off"


def test_unknown_temp_holds():
    d = evaluate(make(spa_temp=None))
    assert d.action == "hold" and d.status == "No spa temperature"


def test_turns_on_when_at_or_below_target_minus_buffer():
    assert evaluate(make(spa_temp=97.0, heater_on=False)).action == "on"
    assert evaluate(make(spa_temp=96.0, heater_on=False)).action == "on"
    d = evaluate(make(spa_temp=97.5, heater_on=False))
    assert d.action == "hold" and d.status == "Holding 97.5°"


def test_turns_off_at_target_minus_off_early():
    assert evaluate(make(spa_temp=100.0, heater_on=True)).action == "off"
    assert evaluate(make(spa_temp=99.0, heater_on=True)).action == "hold"
    assert evaluate(make(spa_temp=99.0, heater_on=True, off_early=1.0)).action == "off"
    assert evaluate(make(spa_temp=99.0, heater_on=True)).status == "Heating 99°"


def test_min_cycle_time_blocks_switch():
    d = evaluate(make(spa_temp=100.0, heater_on=True, now=10_000.0, last_switch_at=10_000.0 - MIN_CYCLE_SECONDS + 1))
    assert d.action == "hold" and d.status == "Waiting (min. cycle time)"
    d = evaluate(make(spa_temp=100.0, heater_on=True, now=10_000.0, last_switch_at=10_000.0 - MIN_CYCLE_SECONDS))
    assert d.action == "off"


def test_manual_on_respected_until_off_threshold():
    # user turned heater on while at 98 (between thresholds): hold, do not fight it
    assert evaluate(make(spa_temp=98.0, heater_on=True)).action == "hold"
    # user turned heater off at 98: hold until it drops to 97
    assert evaluate(make(spa_temp=98.0, heater_on=False)).action == "hold"


def test_reasons_include_temperature():
    assert evaluate(make(spa_temp=100.0, heater_on=True)).reason == "Reached 100°"
    assert evaluate(make(spa_temp=96.0, heater_on=False)).reason == "Dropped to 96°"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_thermostat.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.thermostat'`

- [ ] **Step 3: Implement the engine**

`pool_control/app/thermostat.py`:
```python
"""Pure spa thermostat rules. No I/O here; the runner does the switching."""
from __future__ import annotations

from dataclasses import dataclass

MIN_CYCLE_SECONDS = 300


@dataclass(frozen=True)
class ThermostatInput:
    enabled: bool
    spa_on: bool
    spa_temp: float | None
    heater_on: bool
    target: float
    buffer: float
    off_early: float
    now: float
    last_switch_at: float | None


@dataclass(frozen=True)
class Decision:
    action: str  # "on" | "off" | "hold"
    status: str
    reason: str = ""


def _fmt(temp: float) -> str:
    return f"{temp:g}°"


def evaluate(inp: ThermostatInput) -> Decision:
    if not inp.enabled:
        return Decision("hold", "Thermostat off")
    if not inp.spa_on:
        return Decision("hold", "Spa is off")
    if inp.spa_temp is None:
        return Decision("hold", "No spa temperature")

    temp = inp.spa_temp
    off_at = inp.target - inp.off_early
    on_at = inp.target - inp.buffer

    wanted = None
    if inp.heater_on and temp >= off_at:
        wanted = ("off", f"Reached {_fmt(temp)}")
    elif not inp.heater_on and temp <= on_at:
        wanted = ("on", f"Dropped to {_fmt(temp)}")

    if wanted is None:
        status = f"Heating {_fmt(temp)}" if inp.heater_on else f"Holding {_fmt(temp)}"
        return Decision("hold", status)

    if inp.last_switch_at is not None and inp.now - inp.last_switch_at < MIN_CYCLE_SECONDS:
        return Decision("hold", "Waiting (min. cycle time)", wanted[1])

    return Decision(wanted[0], f"Heater {wanted[0]}", wanted[1])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_thermostat.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/thermostat.py tests/test_thermostat.py
git commit -m "Add spa thermostat decision engine

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: WebTouch stream parser and screen model

**Files:**
- Create: `pool_control/app/webtouch_parser.py`
- Test: `tests/test_webtouch_parser.py`

**Interfaces:**
- Produces: `NLMessage(code: int, params: list[str])`.
- Produces: `StreamParser().feed(text: str) -> list[NLMessage]` (buffers partial chunks between calls).
- Produces: `Button(index: int, state: int, image: str, label: str, value: str)`.
- Produces: `ScreenModel` with attributes `page_id: str | None`, `buttons: dict[int, Button]`, `info: dict[int, str]`, method `apply(msg: NLMessage) -> None`, and `button_by_label(label: str) -> Button | None` (case-insensitive, whitespace-insensitive; matches either the label alone or label+value joined, because Home page buttons split their caption across the two fields, e.g. "Water-" + "fall").
- Constants: `PAGE_HOME = "1"`, `PAGE_MENU = "15"`, `PAGE_DEVICES = "54"`, `PAGE_VSP = "30"`, `CODE_PAGE = 23`, `CODE_BUTTON = 24`, `CODE_INFO = 25`.
- Function: `command_for_button(index: int) -> int` returning `17 + index`.

- [ ] **Step 1: Write the failing tests**

`tests/test_webtouch_parser.py`:
```python
from app.webtouch_parser import (
    CODE_BUTTON, PAGE_VSP, NLMessage, ScreenModel, StreamParser, command_for_button,
)

CHUNK = "<script type='text/javascript'>parent.printNL({code}, \"{params}\");</script>"


def script(code, params):
    return CHUNK.format(code=code, params=params)


def test_parser_extracts_messages_and_splits_params():
    p = StreamParser()
    msgs = p.feed(script(23, "30") + script(24, "0||1||0||Pool||2950"))
    assert msgs == [NLMessage(23, ["30"]), NLMessage(24, ["0", "1", "0", "Pool", "2950"])]


def test_parser_buffers_partial_chunks():
    p = StreamParser()
    whole = script(25, "0||2800")
    assert p.feed(whole[:20]) == []
    assert p.feed(whole[20:]) == [NLMessage(25, ["0", "2800"])]


def test_parser_tolerates_single_quotes_and_whitespace():
    p = StreamParser()
    text = "<script type='text/javascript'>parent.printNL(28, '9||14||26||10||10') </script>"
    assert p.feed(text) == [NLMessage(28, ["9", "14", "26", "10", "10"])]


def test_screen_model_tracks_page_buttons_and_info():
    m = ScreenModel()
    for msg in StreamParser().feed(
        script(23, "30") + script(24, "0||1||0||Pool||2950") + script(24, "6||0||0||Cloudy||2800") + script(25, "0||2950")
    ):
        m.apply(msg)
    assert m.page_id == PAGE_VSP
    assert m.buttons[0].label == "Pool" and m.buttons[0].value == "2950" and m.buttons[0].state == 1
    assert m.buttons[6].state == 0
    assert m.info[0] == "2950"
    # speed change on the same page updates in place (captured live)
    for msg in StreamParser().feed(script(24, "0||0||0||Pool||2950") + script(24, "6||1||0||Cloudy||2800") + script(25, "0||2800")):
        m.apply(msg)
    assert m.buttons[0].state == 0 and m.buttons[6].state == 1 and m.info[0] == "2800"


def test_screen_model_resets_on_page_change():
    m = ScreenModel()
    m.apply(NLMessage(23, ["54"]))
    m.apply(NLMessage(CODE_BUTTON, ["2", "0", "0", "VSP1 Spd", "ADJ"]))
    m.apply(NLMessage(23, ["1"]))
    assert m.page_id == "1" and m.buttons == {} and m.info == {}


def test_button_by_label_is_forgiving():
    m = ScreenModel()
    m.apply(NLMessage(23, ["54"]))
    m.apply(NLMessage(CODE_BUTTON, ["2", "0", "0", "VSP1 Spd", "ADJ"]))
    m.apply(NLMessage(CODE_BUTTON, ["6", "1", "0", "Waterfall", "ON"]))
    assert m.button_by_label("vsp1 spd").index == 2
    assert m.button_by_label("WATERFALL ").state == 1
    assert m.button_by_label("nope") is None
    # Home page buttons split the label across label and value ("Water-" + "fall")
    m.apply(NLMessage(CODE_BUTTON, ["4", "0", "3", "Water-", "fall"]))
    assert m.button_by_label("Water-fall").index == 4


def test_command_for_button():
    assert command_for_button(0) == 17
    assert command_for_button(7) == 24
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_webtouch_parser.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.webtouch_parser'`

- [ ] **Step 3: Implement the parser and model**

`pool_control/app/webtouch_parser.py`:
```python
"""Parses the WebTouch server-push stream and keeps a model of the current screen.

The stream body is a sequence of
  <script type='text/javascript'>parent.printNL(<code>, "<params>");</script>
chunks. params fields are separated by "||". See docs/iaqualink-webtouch-protocol.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PAGE_HOME = "1"
PAGE_MENU = "15"
PAGE_DEVICES = "54"
PAGE_VSP = "30"

CODE_PAGE = 23
CODE_BUTTON = 24
CODE_INFO = 25

_SCRIPT_RE = re.compile(
    r"<script[^>]*>\s*parent\.printNL\(\s*(\d+)\s*,\s*(['\"])(.*?)\2\s*\)\s*;?\s*</script>",
    re.S,
)


@dataclass(frozen=True)
class NLMessage:
    code: int
    params: list[str]


@dataclass(frozen=True)
class Button:
    index: int
    state: int
    image: str
    label: str
    value: str


def command_for_button(index: int) -> int:
    return 17 + index


class StreamParser:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[NLMessage]:
        self._buffer += text
        messages: list[NLMessage] = []
        last_end = 0
        for match in _SCRIPT_RE.finditer(self._buffer):
            code = int(match.group(1))
            raw = match.group(3).replace("\\xC3\\u201A", "")
            messages.append(NLMessage(code, raw.split("||")))
            last_end = match.end()
        self._buffer = self._buffer[last_end:]
        # keep the buffer from growing without bound if garbage arrives
        if len(self._buffer) > 200_000:
            self._buffer = self._buffer[-50_000:]
        return messages


def _norm(label: str) -> str:
    return re.sub(r"\s+", "", label).lower()


@dataclass
class ScreenModel:
    page_id: str | None = None
    buttons: dict[int, Button] = field(default_factory=dict)
    info: dict[int, str] = field(default_factory=dict)

    def apply(self, msg: NLMessage) -> None:
        if msg.code == CODE_PAGE:
            self.page_id = msg.params[0].strip()
            self.buttons = {}
            self.info = {}
        elif msg.code == CODE_BUTTON and len(msg.params) >= 5:
            index = int(msg.params[0])
            state = int(msg.params[1]) if msg.params[1].strip().isdigit() else 0
            self.buttons[index] = Button(index, state, msg.params[2], msg.params[3].strip(), msg.params[4].strip())
        elif msg.code == CODE_INFO and len(msg.params) >= 2:
            self.info[int(msg.params[0])] = msg.params[1].strip()

    def button_by_label(self, label: str) -> Button | None:
        wanted = _norm(label)
        for button in self.buttons.values():
            if _norm(button.label) == wanted or _norm(button.label + button.value) == wanted:
                return button
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_webtouch_parser.py -q`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/webtouch_parser.py tests/test_webtouch_parser.py
git commit -m "Add WebTouch stream parser and screen model

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: iAqualink login, refresh, and device discovery

**Files:**
- Create: `pool_control/app/aqualink_auth.py`
- Test: `tests/test_aqualink_auth.py`

**Interfaces:**
- Produces: `AqualinkAuth(http: httpx.AsyncClient, email: str, password: str, clock: Callable[[], float] = time.time)` with:
  - `async login() -> None`
  - `async ensure_token() -> str` (logs in if no token; refreshes if fewer than 300 s remain; falls back to login if refresh fails)
  - `async discover_touch_link(serial: str | None) -> str` (raises `AqualinkAuthError` if none)
  - attributes `id_token: str`, `refresh_token: str`, `expires_at: float`.
- Produces: `AqualinkAuthError(Exception)`.
- Constants: `LOGIN_URL`, `REFRESH_URL`, `API_KEY = "EOOEMOW4YR6QNB07"`, `PORTAL_URL = "https://prm.iaqualink.net/v2"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_aqualink_auth.py`:
```python
import json

import httpx
import pytest

from app.aqualink_auth import API_KEY, AqualinkAuth, AqualinkAuthError

LOGIN_BODY = {"userPoolOAuth": {"IdToken": "tok1", "RefreshToken": "ref1", "ExpiresIn": 3600}}


def make_auth(handler, now=1000.0):
    clock = {"now": now}
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    auth = AqualinkAuth(http, "me@example.com", "pw", clock=lambda: clock["now"])
    return auth, clock


async def test_login_posts_api_key_and_stores_token():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json=LOGIN_BODY)

    auth, _ = make_auth(handler)
    await auth.login()
    assert seen["url"] == "https://prod.zodiac-io.com/users/v1/login"
    assert seen["json"] == {"api_key": API_KEY, "email": "me@example.com", "password": "pw"}
    assert auth.id_token == "tok1" and auth.refresh_token == "ref1"
    assert auth.expires_at == 1000.0 + 3600


async def test_login_failure_raises():
    def handler(request):
        return httpx.Response(401, json={"message": "bad"})

    auth, _ = make_auth(handler)
    with pytest.raises(AqualinkAuthError):
        await auth.login()


async def test_ensure_token_refreshes_when_expiring():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json=LOGIN_BODY)
        assert json.loads(request.content) == {"email": "me@example.com", "refresh_token": "ref1"}
        return httpx.Response(200, json={"userPoolOAuth": {"IdToken": "tok2", "RefreshToken": "ref2", "ExpiresIn": 3600}})

    auth, clock = make_auth(handler)
    assert await auth.ensure_token() == "tok1"
    clock["now"] = 1000.0 + 3600 - 200  # 200 s left -> refresh
    assert await auth.ensure_token() == "tok2"
    assert calls == ["/users/v1/login", "/users/v1/refresh"]


async def test_ensure_token_falls_back_to_login_when_refresh_fails():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/refresh"):
            return httpx.Response(400, json={})
        return httpx.Response(200, json=LOGIN_BODY)

    auth, clock = make_auth(handler)
    await auth.ensure_token()
    clock["now"] = 1000.0 + 3600
    await auth.ensure_token()
    assert calls == ["/users/v1/login", "/users/v1/refresh", "/users/v1/login"]


async def test_discover_touch_link_uses_bearer_and_picks_iaqua_device():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json=LOGIN_BODY)
        if request.url.path == "/v2/userId":
            return httpx.Response(200, json={"session_user_id": "SESS1"})
        if request.url.path == "/v2/users/SESS1/locations":
            return httpx.Response(200, json={"locations": [
                {"Name": "Robot", "device_type": "vr", "serial_number": "R1", "touchLink": "no"},
                {"Name": "Fowler Pool", "device_type": "iaqua", "serial_number": "QK1", "touchLink": "LinkA"},
                {"Name": "Other", "device_type": "iaqua", "serial_number": "QK2", "touchLink": "LinkB"},
            ]})
        return httpx.Response(404)

    auth, _ = make_auth(handler)
    assert await auth.discover_touch_link(None) == "LinkA"
    assert await auth.discover_touch_link("QK2") == "LinkB"
    with pytest.raises(AqualinkAuthError):
        await auth.discover_touch_link("missing")
    assert ("/v2/userId", "Bearer tok1") in seen
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_aqualink_auth.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.aqualink_auth'`

- [ ] **Step 3: Implement auth**

`pool_control/app/aqualink_auth.py`:
```python
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://prod.zodiac-io.com/users/v1/login"
REFRESH_URL = "https://prod.zodiac-io.com/users/v1/refresh"
API_KEY = "EOOEMOW4YR6QNB07"
PORTAL_URL = "https://prm.iaqualink.net/v2"
REFRESH_MARGIN_SECONDS = 300


class AqualinkAuthError(Exception):
    pass


class AqualinkAuth:
    def __init__(self, http: httpx.AsyncClient, email: str, password: str, clock: Callable[[], float] = time.time):
        self._http = http
        self._email = email
        self._password = password
        self._clock = clock
        self.id_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0

    def _apply(self, body: dict) -> None:
        try:
            oauth = body["userPoolOAuth"]
            self.id_token = oauth["IdToken"]
            self.refresh_token = oauth.get("RefreshToken") or self.refresh_token
            self.expires_at = self._clock() + float(oauth.get("ExpiresIn", 3600))
        except (KeyError, TypeError) as exc:
            raise AqualinkAuthError(f"unexpected login response: {exc}") from exc

    async def login(self) -> None:
        payload = {"api_key": API_KEY, "email": self._email, "password": self._password}
        r = await self._http.post(LOGIN_URL, json=payload, timeout=15)
        if r.status_code != 200:
            raise AqualinkAuthError(f"login failed: HTTP {r.status_code}")
        self._apply(r.json())
        LOGGER.info("iAqualink login ok")

    async def _refresh(self) -> None:
        payload = {"email": self._email, "refresh_token": self.refresh_token}
        r = await self._http.post(REFRESH_URL, json=payload, timeout=15)
        if r.status_code != 200:
            raise AqualinkAuthError(f"refresh failed: HTTP {r.status_code}")
        self._apply(r.json())
        LOGGER.info("iAqualink token refreshed")

    async def ensure_token(self) -> str:
        if not self.id_token:
            await self.login()
        elif self.expires_at - self._clock() < REFRESH_MARGIN_SECONDS:
            try:
                await self._refresh()
            except (AqualinkAuthError, httpx.HTTPError) as exc:
                LOGGER.warning("Refresh failed (%s); logging in again", exc)
                await self.login()
        return self.id_token

    async def _portal_get(self, path: str) -> dict:
        token = await self.ensure_token()
        r = await self._http.get(f"{PORTAL_URL}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code != 200:
            raise AqualinkAuthError(f"GET {path} failed: HTTP {r.status_code}")
        return r.json()

    async def discover_touch_link(self, serial: str | None) -> str:
        user = await self._portal_get("/userId")
        session_user_id = user.get("session_user_id")
        if not session_user_id:
            raise AqualinkAuthError("no session_user_id in /userId response")
        locations = (await self._portal_get(f"/users/{session_user_id}/locations")).get("locations", [])
        candidates = [d for d in locations if d.get("device_type") == "iaqua" and d.get("touchLink")]
        if serial:
            candidates = [d for d in candidates if d.get("serial_number") == serial]
        if not candidates:
            raise AqualinkAuthError("no iAqua device found on this account" + (f" with serial {serial}" if serial else ""))
        device = candidates[0]
        LOGGER.info("Using iAqualink device %s (%s)", device.get("Name"), device.get("serial_number"))
        return device["touchLink"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_aqualink_auth.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/aqualink_auth.py tests/test_aqualink_auth.py
git commit -m "Add iAqualink login, refresh and device discovery

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: WebTouch session client (stream, navigation, commands)

**Files:**
- Create: `pool_control/app/aqualink_client.py`
- Test: `tests/test_aqualink_client.py`

**Interfaces:**
- Consumes: `AqualinkAuth` (Task 5), `StreamParser`, `ScreenModel`, page/code constants, `command_for_button` (Task 4).
- Produces: `AqualinkState` dataclass: `connected: bool = False, rpm: int | None = None, active_preset: str | None = None, presets: list[dict] = []` (each `{"index": int, "label": str, "rpm": int}`), `waterfall_on: bool | None = None, pool_temp: float | None = None, air_temp: float | None = None, spa_temp: float | None = None, error: str | None = None`, plus `to_dict()`.
- Produces: `AqualinkClient(auth, http, touch_link_provider, on_change, refresh_interval=300)` with `async start()`, `async stop()`, `state -> AqualinkState`, `async set_preset(index: int) -> None`, `async set_custom_rpm(rpm: int) -> None`, `async set_waterfall(on: bool) -> None`, `async refresh_vsp() -> None`. Command methods raise `AqualinkCommandError` on failure.
- Constants: `WEBTOUCH_API = "https://prm.iaqualink.net/v2/webtouch"`, `NAV_HOME = 1`, `HOME_OTHER_DEVICES_INDEX = 7`, `DEVICES_VSP_ADJ_LABEL = "VSP1 Spd"`, `HOME_WATERFALL_LABEL = "Water-fall"`, `CUSTOM_RPM_COMMAND = 128`.

Navigation rule: never send a page-button command unless `screen.page_id` equals the expected page. `_goto_vsp()` does Home → Other Devices → VSP, waiting for each page id (5 s timeout, one retry from Home).

- [ ] **Step 1: Write the failing tests**

`tests/test_aqualink_client.py`:
```python
import asyncio
import json

import httpx
import pytest

from app.aqualink_auth import AqualinkAuth
from app.aqualink_client import AqualinkClient, AqualinkCommandError

INIT_BODY = {
    "systemType": 0,
    "serverConnection": "https://webtouch.iaqualink.net/5E/STREAM",
    "masterID": "?actionID=NL_MASTER",
    "masterStart": "?actionID=NL_START",
    "masterSTB": "?actionID=NL_STB",
    "masterReset": "?actionID=NL_RESET",
}


def nl(code, params):
    return f"<script type='text/javascript'>parent.printNL({code}, \"{params}\");</script>"


HOME = nl(23, "1") + nl(24, "0||1||8||Filter||Pump") + nl(24, "4||0||3||Water-||fall") + nl(24, "7||0||0||Other||Devices") + nl(25, "0||82º") + nl(25, "1||63º")
DEVICES = nl(23, "54") + nl(24, "2||0||0||VSP1 Spd||ADJ") + nl(24, "6||0||0||Waterfall||OFF")
VSP = nl(23, "30") + nl(24, "0||1||0||Pool||2950") + nl(24, "6||0||0||Cloudy||2800") + nl(25, "0||2950")


class FakeCloud:
    """Simulates prm.iaqualink.net + the webtouch stream. Commands push new screen chunks."""

    def __init__(self):
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.commands: list[dict] = []
        self.init_calls = 0
        self.page = "1"

    async def handler(self, request: httpx.Request):
        path = request.url.path
        if path.endswith("/users/v1/login"):
            return httpx.Response(200, json={"userPoolOAuth": {"IdToken": "tok", "RefreshToken": "r", "ExpiresIn": 3600}})
        if path.endswith("/webtouch/init"):
            self.init_calls += 1
            assert request.headers["Authorization"] == "tok"
            assert request.url.params["actionID"] == "LINK"
            await self.queue.put(HOME)
            return httpx.Response(200, json=INIT_BODY, headers={"set-cookie": "wt=1; Path=/"})
        if path == "/5E/STREAM":
            return httpx.Response(200, stream=QueueStream(self.queue))
        if path.endswith("/webtouch/command"):
            body = json.loads(request.content)
            self.commands.append(body)
            self.react(body)
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    def react(self, body):
        cmd = int(body["command"])
        if cmd == 1:
            self.page = "1"; self.queue.put_nowait(HOME)
        elif self.page == "1" and cmd == 24:
            self.page = "54"; self.queue.put_nowait(DEVICES)
        elif self.page == "1" and cmd == 21:
            self.queue.put_nowait(nl(24, "4||1||3||Water-||fall"))
        elif self.page == "54" and cmd == 19:
            self.page = "30"; self.queue.put_nowait(VSP)
        elif self.page == "30" and cmd == 23:
            self.queue.put_nowait(nl(24, "0||0||0||Pool||2950") + nl(24, "6||1||0||Cloudy||2800") + nl(25, "0||2800"))
        elif self.page == "30" and cmd == 128:
            self.queue.put_nowait(nl(25, "0||" + body["text"]))


class QueueStream(httpx.AsyncByteStream):
    def __init__(self, queue):
        self.queue = queue

    async def __aiter__(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item.encode()


@pytest.fixture
async def client_and_cloud():
    cloud = FakeCloud()
    http = httpx.AsyncClient(transport=httpx.MockTransport(cloud.handler))
    auth = AqualinkAuth(http, "e", "p")
    changes = []

    async def touch_link():
        return "LINK"

    client = AqualinkClient(auth, http, touch_link, on_change=lambda: changes.append(1), refresh_interval=3600)
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    yield client, cloud, changes
    await client.stop()
    await http.aclose()


async def test_connects_and_reads_home_page(client_and_cloud):
    client, cloud, changes = client_and_cloud
    s = client.state
    assert s.connected and s.pool_temp == 82 and s.air_temp == 63 and s.waterfall_on is False
    assert cloud.init_calls == 1 and changes


async def test_set_preset_navigates_and_confirms(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_preset(6)
    assert [int(c["command"]) for c in cloud.commands] == [1, 24, 19, 23, 1]
    assert all(c["actionID"] == "NL_MASTER" for c in cloud.commands)
    assert client.state.rpm == 2800 and client.state.active_preset == "Cloudy"
    assert [p["label"] for p in client.state.presets] == ["Pool", "Cloudy"]
    assert client.state.presets[0]["rpm"] == 2950


async def test_set_custom_rpm_uses_stb_action(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_custom_rpm(1500)
    last = cloud.commands[-2]
    assert last == {"actionID": "NL_STB", "command": "128", "text": "1500", "dt": last["dt"]}
    assert client.state.rpm == 1500


async def test_set_waterfall_from_home(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_waterfall(True)
    assert int(cloud.commands[-1]["command"]) == 21
    assert client.state.waterfall_on is True


async def test_set_waterfall_noop_when_already_in_state(client_and_cloud):
    client, cloud, _ = client_and_cloud
    await client.set_waterfall(False)
    assert cloud.commands == []


async def test_command_fails_when_page_never_arrives(client_and_cloud):
    client, cloud, _ = client_and_cloud
    cloud.react = lambda body: None  # cloud stops answering
    client.page_timeout = 0.2
    with pytest.raises(AqualinkCommandError):
        await client.set_preset(0)


async def test_stream_end_reconnects(client_and_cloud):
    client, cloud, _ = client_and_cloud
    client.reconnect_delay = 0.05
    await cloud.queue.put(None)  # server closes stream
    await asyncio.sleep(0.5)
    assert cloud.init_calls == 2 and client.state.connected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_aqualink_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.aqualink_client'`

- [ ] **Step 3: Implement the client**

`pool_control/app/aqualink_client.py`:
```python
"""iAqualink WebTouch session: stream reader, page navigation, pump and waterfall commands."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

import httpx

from app.aqualink_auth import AqualinkAuth, AqualinkAuthError
from app.webtouch_parser import (
    PAGE_DEVICES, PAGE_HOME, PAGE_VSP, ScreenModel, StreamParser, command_for_button,
)

LOGGER = logging.getLogger(__name__)

WEBTOUCH_API = "https://prm.iaqualink.net/v2/webtouch"
NAV_HOME = 1
HOME_OTHER_DEVICES_INDEX = 7
DEVICES_VSP_ADJ_LABEL = "VSP1 Spd"
HOME_WATERFALL_LABEL = "Water-fall"
CUSTOM_RPM_COMMAND = 128
MAX_RECONNECT_DELAY = 300
HOME_INFO_POOL_TEMP = 0
HOME_INFO_AIR_TEMP = 1
HOME_INFO_SPA_TEMP = 3


class AqualinkCommandError(Exception):
    pass


@dataclass
class AqualinkState:
    connected: bool = False
    rpm: int | None = None
    active_preset: str | None = None
    presets: list[dict] = field(default_factory=list)
    waterfall_on: bool | None = None
    pool_temp: float | None = None
    air_temp: float | None = None
    spa_temp: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_number(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"-?\d+(\.\d+)?", text)
    return float(match.group()) if match else None


def _action_id(master: str) -> str:
    # masterID looks like "?actionID=NL_XYxCH3nqtqVa"
    return master.split("actionID=", 1)[-1].split("&", 1)[0]


class AqualinkClient:
    def __init__(
        self,
        auth: AqualinkAuth,
        http: httpx.AsyncClient,
        touch_link_provider: Callable[[], Awaitable[str]],
        on_change: Callable[[], None],
        refresh_interval: float = 300,
    ):
        self._auth = auth
        self._http = http
        self._touch_link_provider = touch_link_provider
        self._on_change = on_change
        self.refresh_interval = refresh_interval
        self.page_timeout = 5.0
        self.reconnect_delay = 5.0

        self.state = AqualinkState()
        self.screen = ScreenModel()
        self._master_action = ""
        self._stb_action = ""
        self._stream_url = ""
        self._task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._screen_changed = asyncio.Condition()
        self._stopping = False

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="aqualink-stream")
        self._refresh_task = asyncio.create_task(self._periodic_refresh(), name="aqualink-refresh")

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._task, self._refresh_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def wait_connected(self) -> None:
        await self._connected.wait()

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:  # never let a listener kill the client
            LOGGER.exception("on_change listener failed")

    # ---------- session / stream ----------

    async def _run(self) -> None:
        delay = self.reconnect_delay
        while not self._stopping:
            try:
                await self._open_session()
                delay = self.reconnect_delay  # session opened: reset backoff
                await self._read_stream()
                LOGGER.warning("WebTouch stream ended; reconnecting")
            except asyncio.CancelledError:
                raise
            except (AqualinkAuthError, httpx.HTTPError, ValueError, KeyError) as exc:
                LOGGER.warning("WebTouch session error: %s", exc)
                self.state.error = str(exc)
            self._connected.clear()
            self.state.connected = False
            self._notify()
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _open_session(self) -> None:
        token = await self._auth.ensure_token()
        touch_link = await self._touch_link_provider()
        r = await self._http.get(
            f"{WEBTOUCH_API}/init",
            params={"actionID": touch_link},
            headers={"Authorization": token},
            timeout=20,
        )
        if r.status_code != 200:
            raise AqualinkAuthError(f"webtouch init failed: HTTP {r.status_code}")
        data = r.json()
        self._stream_url = data["serverConnection"]
        self._master_action = _action_id(data["masterID"])
        self._stb_action = _action_id(data["masterSTB"])
        self.screen = ScreenModel()
        LOGGER.info("WebTouch session opened (system type %s)", data.get("systemTypeDisplay", data.get("systemType")))

    async def _read_stream(self) -> None:
        parser = StreamParser()
        async with self._http.stream("GET", self._stream_url, timeout=httpx.Timeout(None, connect=20)) as response:
            if response.status_code != 200:
                raise AqualinkAuthError(f"stream failed: HTTP {response.status_code}")
            async for chunk in response.aiter_text():
                messages = parser.feed(chunk)
                if not messages:
                    continue
                for msg in messages:
                    self.screen.apply(msg)
                self._update_state_from_screen()
                if not self._connected.is_set():
                    self.state.connected = True
                    self.state.error = None
                    self._connected.set()
                async with self._screen_changed:
                    self._screen_changed.notify_all()
                self._notify()

    def _update_state_from_screen(self) -> None:
        screen = self.screen
        if screen.page_id == PAGE_HOME:
            self.state.pool_temp = _parse_number(screen.info.get(HOME_INFO_POOL_TEMP))
            self.state.air_temp = _parse_number(screen.info.get(HOME_INFO_AIR_TEMP))
            self.state.spa_temp = _parse_number(screen.info.get(HOME_INFO_SPA_TEMP))
            waterfall = screen.button_by_label(HOME_WATERFALL_LABEL)
            if waterfall is not None:
                self.state.waterfall_on = waterfall.state == 1
        elif screen.page_id == PAGE_VSP:
            presets = []
            active = None
            for index in sorted(screen.buttons):
                button = screen.buttons[index]
                rpm = _parse_number(button.value)
                if rpm is None or not button.label:
                    continue
                presets.append({"index": index, "label": button.label, "rpm": int(rpm)})
                if button.state == 1:
                    active = button.label
            if presets:
                self.state.presets = presets
                self.state.active_preset = active
            rpm = _parse_number(screen.info.get(0))
            if rpm is not None:
                self.state.rpm = int(rpm)

    async def _periodic_refresh(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.refresh_interval)
            try:
                await self.refresh_vsp()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Periodic VSP refresh failed: %s", exc)

    # ---------- commands ----------

    async def _send(self, command: int, action_id: str | None = None, text: str | None = None) -> None:
        token = await self._auth.ensure_token()
        body = {
            "actionID": action_id or self._master_action,
            "command": str(command),
            "dt": str(int(time.time() * 1000)),
        }
        if text is not None:
            body["text"] = text
        r = await self._http.post(
            f"{WEBTOUCH_API}/command",
            json=body,
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            raise AqualinkCommandError(f"command {command} failed: HTTP {r.status_code}")

    async def _wait_for(self, predicate: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + self.page_timeout
        async with self._screen_changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AqualinkCommandError(f"timed out waiting for {what}")
                try:
                    await asyncio.wait_for(self._screen_changed.wait(), remaining)
                except asyncio.TimeoutError:
                    raise AqualinkCommandError(f"timed out waiting for {what}")

    async def _go_home(self) -> None:
        await self._send(NAV_HOME)
        await self._wait_for(lambda: self.screen.page_id == PAGE_HOME, "Home page")

    async def _goto_vsp(self) -> None:
        for attempt in (1, 2):
            try:
                await self._go_home()
                await self._send(command_for_button(HOME_OTHER_DEVICES_INDEX))
                await self._wait_for(lambda: self.screen.page_id == PAGE_DEVICES, "Devices page")
                adj = self.screen.button_by_label(DEVICES_VSP_ADJ_LABEL)
                if adj is None:
                    raise AqualinkCommandError("VSP1 Spd button not found on Devices page")
                await self._send(command_for_button(adj.index))
                await self._wait_for(lambda: self.screen.page_id == PAGE_VSP and bool(self.screen.buttons), "VSP page")
                return
            except AqualinkCommandError as exc:
                if attempt == 2:
                    raise
                LOGGER.warning("VSP navigation failed (%s); retrying from Home", exc)

    def _require_connected(self) -> None:
        if not self.state.connected:
            raise AqualinkCommandError("iAqualink not connected")

    async def set_preset(self, index: int) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            if index not in self.screen.buttons:
                raise AqualinkCommandError(f"no preset at index {index}")
            await self._send(command_for_button(index))
            await self._wait_for(lambda: self.screen.buttons.get(index) is not None and self.screen.buttons[index].state == 1, "preset confirmation")
            await self._go_home()

    async def set_custom_rpm(self, rpm: int) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            await self._send(CUSTOM_RPM_COMMAND, action_id=self._stb_action, text=str(int(rpm)))
            await self._wait_for(lambda: self.state.rpm == int(rpm), "custom RPM confirmation")
            await self._go_home()

    async def refresh_vsp(self) -> None:
        async with self._lock:
            self._require_connected()
            await self._goto_vsp()
            await self._go_home()

    async def set_waterfall(self, on: bool) -> None:
        async with self._lock:
            self._require_connected()
            if self.screen.page_id != PAGE_HOME:
                await self._go_home()
            button = self.screen.button_by_label(HOME_WATERFALL_LABEL)
            if button is None:
                raise AqualinkCommandError("Waterfall button not found on Home page")
            if (button.state == 1) == on:
                return
            await self._send(command_for_button(button.index))
            await self._wait_for(lambda: (self.screen.button_by_label(HOME_WATERFALL_LABEL) or button).state == (1 if on else 0), "waterfall confirmation")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_aqualink_client.py -q`
Expected: `7 passed`. If `test_stream_end_reconnects` is flaky, raise the sleep to 1.0 s; the reconnect delay in the test is 0.05 s.

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/aqualink_client.py tests/test_aqualink_client.py
git commit -m "Add WebTouch session client with navigation and pump commands

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Home Assistant websocket client

**Files:**
- Create: `pool_control/app/ha_client.py`
- Test: `tests/test_ha_client.py`

**Interfaces:**
- Consumes: `entities.ALL_ENTITY_IDS` (Task 1).
- Produces: `HAClient(ws_url: str, token: str, entity_ids: list[str], on_change: Callable[[], None])` with `async start()`, `async stop()`, `async wait_connected()`, `connected: bool`, `states: dict[str, str]` (entity_id → state string), `async call_service(domain: str, service: str, entity_id: str) -> None` (raises `HAError` on failure or when disconnected), `is_on(entity_id) -> bool`, `number(entity_id) -> float | None`.
- Produces: `HAError(Exception)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ha_client.py`:
```python
import asyncio
import json

import pytest
import websockets

from app.ha_client import HAClient, HAError


class FakeHA:
    def __init__(self):
        self.calls = []
        self.conn = None
        self.server = None
        self.url = ""

    async def start(self):
        self.server = await websockets.serve(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/api/websocket"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handle(self, ws):
        self.conn = ws
        await ws.send(json.dumps({"type": "auth_required"}))
        msg = json.loads(await ws.recv())
        if msg.get("access_token") != "T":
            await ws.send(json.dumps({"type": "auth_invalid"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "get_states":
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": [
                    {"entity_id": "switch.spa_heater", "state": "off"},
                    {"entity_id": "sensor.spa_temp", "state": "unknown"},
                    {"entity_id": "sensor.other", "state": "1"},
                ]}))
            elif msg["type"] == "subscribe_events":
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": None}))
            elif msg["type"] == "call_service":
                self.calls.append(msg)
                ok = msg["service_data"]["entity_id"] != "switch.broken"
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": ok, "error": None if ok else {"message": "nope"}}))

    async def push_state(self, entity_id, state):
        await self.conn.send(json.dumps({"type": "event", "event": {"event_type": "state_changed", "data": {
            "entity_id": entity_id, "new_state": {"entity_id": entity_id, "state": state}}}}))


@pytest.fixture
async def ha():
    fake = FakeHA()
    await fake.start()
    yield fake
    await fake.stop()


async def test_connects_loads_states_and_tracks_changes(ha):
    changes = []
    client = HAClient(ha.url, "T", ["switch.spa_heater", "sensor.spa_temp"], on_change=lambda: changes.append(1))
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    assert client.states == {"switch.spa_heater": "off", "sensor.spa_temp": "unknown"}
    assert client.is_on("switch.spa_heater") is False and client.number("sensor.spa_temp") is None
    await ha.push_state("sensor.spa_temp", "95.5")
    await ha.push_state("sensor.other", "2")
    await asyncio.sleep(0.2)
    assert client.number("sensor.spa_temp") == 95.5 and "sensor.other" not in client.states
    assert len(changes) >= 2
    await client.stop()


async def test_call_service_round_trip_and_error(ha):
    client = HAClient(ha.url, "T", ["switch.spa_heater"], on_change=lambda: None)
    await client.start()
    await asyncio.wait_for(client.wait_connected(), 5)
    await client.call_service("switch", "turn_on", "switch.spa_heater")
    assert ha.calls[-1]["domain"] == "switch" and ha.calls[-1]["service"] == "turn_on"
    assert ha.calls[-1]["service_data"] == {"entity_id": "switch.spa_heater"}
    with pytest.raises(HAError):
        await client.call_service("switch", "turn_on", "switch.broken")
    await client.stop()


async def test_call_service_when_disconnected_raises():
    client = HAClient("ws://127.0.0.1:1/api/websocket", "T", [], on_change=lambda: None)
    with pytest.raises(HAError):
        await client.call_service("switch", "turn_on", "switch.x")


async def test_bad_token_does_not_connect(ha):
    client = HAClient(ha.url, "WRONG", [], on_change=lambda: None)
    client.reconnect_delay = 0.05
    await client.start()
    await asyncio.sleep(0.3)
    assert client.connected is False
    await client.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ha_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.ha_client'`

- [ ] **Step 3: Implement the client**

`pool_control/app/ha_client.py`:
```python
"""Minimal Home Assistant websocket client: state cache + service calls."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

LOGGER = logging.getLogger(__name__)
MAX_RECONNECT_DELAY = 300


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, ws_url: str, token: str, entity_ids: list[str], on_change: Callable[[], None]):
        self._ws_url = ws_url
        self._token = token
        self._entity_ids = set(entity_ids)
        self._on_change = on_change
        self.reconnect_delay = 5.0
        self.states: dict[str, str] = {}
        self.connected = False
        self._ws = None
        self._task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._connected_event = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="ha-websocket")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    async def wait_connected(self) -> None:
        await self._connected_event.wait()

    def is_on(self, entity_id: str) -> bool:
        return self.states.get(entity_id) == "on"

    def number(self, entity_id: str) -> float | None:
        try:
            return float(self.states[entity_id])
        except (KeyError, ValueError, TypeError):
            return None

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:
            LOGGER.exception("on_change listener failed")

    async def _run(self) -> None:
        delay = self.reconnect_delay
        while not self._stopping:
            try:
                async with websockets.connect(self._ws_url, max_size=16 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    await self._load_states()
                    await self._request({"type": "subscribe_events", "event_type": "state_changed"})
                    delay = self.reconnect_delay  # connected: reset backoff
                    self.connected = True
                    self._connected_event.set()
                    self._notify()
                    LOGGER.info("Home Assistant websocket connected")
                    await self._listen(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Home Assistant websocket error: %s", exc)
            self.connected = False
            self._connected_event.clear()
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("disconnected"))
            self._pending.clear()
            self._notify()
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _authenticate(self, ws) -> None:
        first = json.loads(await ws.recv())
        if first.get("type") != "auth_required":
            raise HAError(f"unexpected first message: {first}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(await ws.recv())
        if reply.get("type") != "auth_ok":
            raise HAError(f"authentication failed: {reply}")

    async def _listen(self, ws) -> None:
        # the first messages (get_states / subscribe results) are handled via _request futures,
        # so this loop must run concurrently with them: start it before awaiting requests.
        async for raw in ws:
            msg = json.loads(raw)
            if "id" in msg and msg.get("type") == "result":
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("type") == "event":
                data = msg["event"].get("data", {})
                entity_id = data.get("entity_id")
                new_state = data.get("new_state") or {}
                if entity_id in self._entity_ids:
                    self.states[entity_id] = new_state.get("state", "unavailable")
                    self._notify()

    async def _request(self, payload: dict) -> dict:
        if self._ws is None:
            raise HAError("not connected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({"id": msg_id, **payload}))
        # pump messages until our reply arrives (the listen loop may not be running yet)
        while not fut.done():
            raw = await self._ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "result" and msg.get("id") in self._pending:
                pending = self._pending.pop(msg["id"])
                if not pending.done():
                    pending.set_result(msg)
            elif msg.get("type") == "event":
                data = msg["event"].get("data", {})
                if data.get("entity_id") in self._entity_ids:
                    self.states[data["entity_id"]] = (data.get("new_state") or {}).get("state", "unavailable")
                    self._notify()
        result = fut.result()
        if not result.get("success", False):
            raise HAError((result.get("error") or {}).get("message", "request failed"))
        return result

    async def _load_states(self) -> None:
        result = await self._request({"type": "get_states"})
        self.states = {s["entity_id"]: s["state"] for s in result["result"] if s["entity_id"] in self._entity_ids}

    async def call_service(self, domain: str, service: str, entity_id: str) -> None:
        if not self.connected or self._ws is None:
            raise HAError("Home Assistant not connected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({
            "id": msg_id, "type": "call_service", "domain": domain, "service": service,
            "service_data": {"entity_id": entity_id},
        }))
        try:
            result = await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise HAError("service call timed out") from exc
        if not result.get("success", False):
            raise HAError((result.get("error") or {}).get("message", "service call failed"))
```

Note: `_request` is only used during connection setup (before `_listen` runs) and pumps the socket itself; `call_service` is used after setup and relies on `_listen` to resolve its future. Keep that split; do not call `_request` after `_listen` has started.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_ha_client.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/ha_client.py tests/test_ha_client.py
git commit -m "Add Home Assistant websocket client

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Spa session helper and thermostat runner

**Files:**
- Create: `pool_control/app/notifier.py`, `pool_control/app/spa.py`, `pool_control/app/thermostat_runner.py`
- Test: `tests/test_spa.py`, `tests/test_thermostat_runner.py`

**Interfaces:**
- Consumes: `HAClient` protocol (`is_on`, `number`, `call_service`, `connected`), `entities.SWITCHES/SENSORS`, `evaluate/ThermostatInput` (Task 3), `ThermostatSettings/SettingsStore` (Task 2).
- Produces: `Notifier` with `notify() -> None` and `async wait(timeout: float | None) -> bool` (returns True if notified, False on timeout). Used by SSE and by the runner.
- Produces: `ThermostatRunner(ha, store, clock=time.time)` with `settings: ThermostatSettings`, `status: str`, `last_action: dict | None` (`{"time": iso str, "action": "on"|"off", "temp": float, "reason": str}`), `last_switch_at: float | None`, `async update_settings(**changes) -> ThermostatSettings`, `async evaluate_and_act() -> Decision`, `async run(interval: float = 60)` (loop: evaluate every interval or when `wake()` is called), `wake()`, `to_dict()`.
- Produces: `SpaSession(ha, runner, clock=time.time)` with `async start() -> None` (Spa on, Spa Heat on, thermostat enabled), `async end() -> None` (thermostat disabled, Spa Heat off, Filter Pump off, remembers `end_pressed_at`), `label() -> str` (one of `"Off"`, `"Heating to {target:g}°"`, `"Ready {temp:g}°"`, `"Cooling down"`, `"On"`), `cooling_down -> bool` (end pressed within 360 s and filter pump still on).

- [ ] **Step 1: Write the failing tests**

`tests/test_spa.py`:
```python
from app.settings import SettingsStore
from app.spa import SpaSession
from app.thermostat_runner import ThermostatRunner


class FakeHA:
    def __init__(self, **states):
        self.states = states
        self.calls = []
        self.connected = True

    def is_on(self, e):
        return self.states.get(e) == "on"

    def number(self, e):
        try:
            return float(self.states[e])
        except (KeyError, ValueError):
            return None

    async def call_service(self, domain, service, entity_id):
        self.calls.append((domain, service, entity_id))
        self.states[entity_id] = "on" if service == "turn_on" else "off"


def make(tmp_path, **states):
    ha = FakeHA(**states)
    clock = {"now": 5000.0}
    runner = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"), clock=lambda: clock["now"])
    return ha, runner, SpaSession(ha, runner, clock=lambda: clock["now"]), clock


async def test_start_turns_on_spa_and_heat_and_enables_thermostat(tmp_path):
    ha, runner, spa, _ = make(tmp_path)
    await spa.start()
    assert ha.calls == [("switch", "turn_on", "switch.spa_pump"), ("switch", "turn_on", "switch.spa_heater")]
    assert runner.settings.enabled is True


async def test_end_disables_thermostat_heater_off_pump_off_spa_left_on(tmp_path):
    ha, runner, spa, clock = make(tmp_path, **{"switch.spa_pump": "on", "switch.spa_heater": "on", "switch.pool_pump": "on"})
    await runner.update_settings(enabled=True)
    await spa.end()
    assert ha.calls == [("switch", "turn_off", "switch.spa_heater"), ("switch", "turn_off", "switch.pool_pump")]
    assert runner.settings.enabled is False
    assert ha.states["switch.spa_pump"] == "on"


async def test_labels(tmp_path):
    ha, runner, spa, clock = make(tmp_path, **{"switch.spa_pump": "off"})
    assert spa.label() == "Off"
    ha.states["switch.spa_pump"] = "on"
    ha.states["switch.spa_heater"] = "on"
    await runner.update_settings(target=98)
    assert spa.label() == "Heating to 98°"
    ha.states["switch.spa_heater"] = "off"
    ha.states["sensor.spa_temp"] = "97"
    await runner.update_settings(enabled=True)
    assert spa.label() == "Ready 97°"
    await runner.update_settings(enabled=False)
    assert spa.label() == "On"


async def test_cooling_down_window(tmp_path):
    ha, runner, spa, clock = make(tmp_path, **{"switch.spa_pump": "on", "switch.spa_heater": "on", "switch.pool_pump": "on"})
    await spa.end()
    ha.states["switch.pool_pump"] = "on"  # panel keeps it running during cooldown
    assert spa.cooling_down is True and spa.label() == "Cooling down"
    clock["now"] += 361
    assert spa.cooling_down is False
    clock["now"] -= 300
    ha.states["switch.pool_pump"] = "off"
    assert spa.cooling_down is False
```

`tests/test_thermostat_runner.py`:
```python
from app.settings import SettingsStore
from app.thermostat_runner import ThermostatRunner


class FakeHA:
    def __init__(self, **states):
        self.states = states
        self.calls = []
        self.fail = False
        self.connected = True

    def is_on(self, e):
        return self.states.get(e) == "on"

    def number(self, e):
        try:
            return float(self.states[e])
        except (KeyError, ValueError):
            return None

    async def call_service(self, domain, service, entity_id):
        if self.fail:
            from app.ha_client import HAError
            raise HAError("boom")
        self.calls.append((domain, service, entity_id))
        self.states[entity_id] = "on" if service == "turn_on" else "off"


def make(tmp_path, **states):
    ha = FakeHA(**states)
    clock = {"now": 5000.0}
    runner = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"), clock=lambda: clock["now"])
    return ha, runner, clock


async def test_disabled_by_default_does_nothing(tmp_path):
    ha, runner, _ = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    d = await runner.evaluate_and_act()
    assert d.action == "hold" and ha.calls == [] and runner.status == "Thermostat off"


async def test_turns_heater_on_and_records_action(tmp_path):
    ha, runner, clock = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    await runner.update_settings(enabled=True)
    d = await runner.evaluate_and_act()
    assert d.action == "on" and ha.calls == [("switch", "turn_on", "switch.spa_heater")]
    assert runner.last_switch_at == 5000.0
    assert runner.last_action["action"] == "on" and runner.last_action["temp"] == 90.0 and runner.last_action["reason"] == "Dropped to 90°"
    # reaching target 100 within 5 minutes: blocked by min cycle time
    ha.states["sensor.spa_temp"] = "100"
    clock["now"] += 100
    d = await runner.evaluate_and_act()
    assert d.action == "hold" and runner.status == "Waiting (min. cycle time)"
    clock["now"] += 300
    d = await runner.evaluate_and_act()
    assert d.action == "off" and ha.calls[-1] == ("switch", "turn_off", "switch.spa_heater")


async def test_failed_call_is_logged_and_does_not_update_last_switch(tmp_path):
    ha, runner, _ = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    await runner.update_settings(enabled=True)
    ha.fail = True
    d = await runner.evaluate_and_act()
    assert d.action == "on" and runner.last_switch_at is None and "failed" in runner.status.lower()


async def test_settings_persist(tmp_path):
    ha, runner, _ = make(tmp_path)
    await runner.update_settings(target=96, buffer=2)
    again = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"))
    assert again.settings.target == 96 and again.settings.buffer == 2


async def test_to_dict_shape(tmp_path):
    ha, runner, _ = make(tmp_path)
    d = runner.to_dict()
    assert set(d) == {"settings", "status", "last_action"}
    assert d["settings"]["target"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_spa.py tests/test_thermostat_runner.py -q`
Expected: FAIL with `ModuleNotFoundError` for `app.spa` / `app.thermostat_runner`

- [ ] **Step 3: Implement notifier, runner, and spa session**

`pool_control/app/notifier.py`:
```python
from __future__ import annotations

import asyncio


class Notifier:
    """Wakes every waiter once per notify(). Cheap broadcast for SSE and loops."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        self._event.set()

    async def wait(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            # allow the next notify() to wake waiters again
            self._event.clear()
        return True
```

`pool_control/app/thermostat_runner.py`:
```python
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

from app.entities import SENSORS, SWITCHES
from app.notifier import Notifier
from app.settings import SettingsStore, ThermostatSettings
from app.thermostat import Decision, ThermostatInput, evaluate

LOGGER = logging.getLogger(__name__)


class ThermostatRunner:
    def __init__(self, ha, store: SettingsStore, clock: Callable[[], float] = time.time):
        self._ha = ha
        self._store = store
        self._clock = clock
        self.settings: ThermostatSettings = store.load()
        self.status = "Thermostat off"
        self.last_action: dict | None = None
        self.last_switch_at: float | None = None
        self._wake = Notifier()
        self._lock = asyncio.Lock()

    def wake(self) -> None:
        self._wake.notify()

    async def update_settings(self, **changes) -> ThermostatSettings:
        self.settings = self.settings.with_changes(**changes)
        self._store.save(self.settings)
        self.wake()
        return self.settings

    def _input(self) -> ThermostatInput:
        return ThermostatInput(
            enabled=self.settings.enabled,
            spa_on=self._ha.is_on(SWITCHES["spa"]),
            spa_temp=self._ha.number(SENSORS["spa_temp"]),
            heater_on=self._ha.is_on(SWITCHES["spa_heat"]),
            target=self.settings.target,
            buffer=self.settings.buffer,
            off_early=self.settings.off_early,
            now=self._clock(),
            last_switch_at=self.last_switch_at,
        )

    async def evaluate_and_act(self) -> Decision:
        async with self._lock:
            inp = self._input()
            decision = evaluate(inp)
            self.status = decision.status
            if decision.action in ("on", "off"):
                service = "turn_on" if decision.action == "on" else "turn_off"
                try:
                    await self._ha.call_service("switch", service, SWITCHES["spa_heat"])
                except Exception as exc:
                    LOGGER.error("Thermostat: heater %s failed: %s", decision.action, exc)
                    self.status = f"Heater {decision.action} failed, will retry"
                    return decision
                self.last_switch_at = inp.now
                self.last_action = {
                    "time": datetime.now().isoformat(timespec="minutes"),
                    "action": decision.action,
                    "temp": inp.spa_temp,
                    "reason": decision.reason,
                }
                LOGGER.info("Thermostat: heater %s at %s° (%s)", decision.action, inp.spa_temp, decision.reason)
            return decision

    async def run(self, interval: float = 60) -> None:
        while True:
            try:
                await self.evaluate_and_act()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Thermostat evaluation failed")
            await self._wake.wait(interval)

    def to_dict(self) -> dict:
        return {"settings": self.settings.to_dict(), "status": self.status, "last_action": self.last_action}
```

`pool_control/app/spa.py`:
```python
from __future__ import annotations

import time
from typing import Callable

from app.entities import SENSORS, SWITCHES

COOLDOWN_WINDOW_SECONDS = 360


class SpaSession:
    def __init__(self, ha, runner, clock: Callable[[], float] = time.time):
        self._ha = ha
        self._runner = runner
        self._clock = clock
        self.end_pressed_at: float | None = None

    async def start(self) -> None:
        await self._ha.call_service("switch", "turn_on", SWITCHES["spa"])
        await self._ha.call_service("switch", "turn_on", SWITCHES["spa_heat"])
        await self._runner.update_settings(enabled=True)
        self.end_pressed_at = None

    async def end(self) -> None:
        await self._runner.update_settings(enabled=False)
        await self._ha.call_service("switch", "turn_off", SWITCHES["spa_heat"])
        await self._ha.call_service("switch", "turn_off", SWITCHES["filter_pump"])
        self.end_pressed_at = self._clock()

    @property
    def cooling_down(self) -> bool:
        if self.end_pressed_at is None:
            return False
        if self._clock() - self.end_pressed_at > COOLDOWN_WINDOW_SECONDS:
            return False
        return self._ha.is_on(SWITCHES["filter_pump"])

    def label(self) -> str:
        if self.cooling_down:
            return "Cooling down"
        if not self._ha.is_on(SWITCHES["spa"]):
            return "Off"
        if self._ha.is_on(SWITCHES["spa_heat"]):
            return f"Heating to {self._runner.settings.target:g}°"
        temp = self._ha.number(SENSORS["spa_temp"])
        if self._runner.settings.enabled and temp is not None:
            return f"Ready {temp:g}°"
        return "On"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_spa.py tests/test_thermostat_runner.py -q`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/notifier.py pool_control/app/spa.py pool_control/app/thermostat_runner.py tests/test_spa.py tests/test_thermostat_runner.py
git commit -m "Add thermostat runner and spa session helper

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Web app: state snapshot, JSON API, SSE

**Files:**
- Modify: `pool_control/app/main.py` (replace the Task 1 stub entirely)
- Test: `tests/test_api.py` (replace the Task 1 file entirely)

**Interfaces:**
- Consumes: everything above.
- Produces: `build_app(ha, aqualink, runner, spa, notifier) -> FastAPI` plus module-level `app = build_app(...)` created by a lifespan that constructs the real clients unless `Config.testing`.
- Endpoints (all under the Ingress root, relative paths):
  - `GET /api/health -> {"ok": true}`
  - `GET /api/state -> snapshot`
  - `GET /api/events` text/event-stream; sends `data: <snapshot json>\n\n` on every change and at least every 15 s (keepalive).
  - `POST /api/switch/{name}` body `{"on": bool}`; names: keys of `SWITCHES`, keys of `LIGHTS`, `lights_all`, `waterfall`. 404 unknown name, 503 with `{"error": ...}` on failure.
  - `POST /api/pump/preset/{index}`; `POST /api/pump/rpm` body `{"rpm": int}` (600–3450 else 400).
  - `POST /api/thermostat` body with any of `enabled/target/buffer/off_early` (400 on ValueError). Returns thermostat dict.
  - `POST /api/spa/start`, `POST /api/spa/end`.
  - `GET /` serves `static/index.html`; `/static/*` serves files.
- Snapshot shape:
```json
{
  "ha": {"connected": true, "switches": {"filter_pump": true, "spa": false, "spa_heat": false, "jet_pump": false, "pool_heat": false},
         "lights": {"light_shallow": true, "light_middle": true, "light_deep": true},
         "pool_temp": 82.0, "spa_temp": null},
  "aqualink": {"connected": true, "rpm": 2950, "active_preset": "Pool", "presets": [{"index":0,"label":"Pool","rpm":2950}],
               "waterfall_on": false, "pool_temp": 82.0, "air_temp": 63.0, "spa_temp": null, "error": null},
  "thermostat": {"settings": {"enabled": false, "target": 100.0, "buffer": 3.0, "off_early": 0.0}, "status": "Thermostat off", "last_action": null},
  "spa": {"label": "Off", "cooling_down": false}
}
```

- [ ] **Step 1: Write the failing tests**

`tests/test_api.py` (replace whole file):
```python
import json

import pytest
from fastapi.testclient import TestClient

from app.aqualink_client import AqualinkCommandError, AqualinkState
from app.main import build_app
from app.notifier import Notifier
from app.settings import SettingsStore
from app.spa import SpaSession
from app.thermostat_runner import ThermostatRunner


class FakeHA:
    def __init__(self):
        self.connected = True
        self.states = {"switch.pool_pump": "on", "switch.spa_pump": "off", "switch.spa_heater": "off",
                       "switch.jet_pump": "off", "switch.pool_heater": "off",
                       "light.pool_pool_light_shallow_end": "on", "light.pool_pool_light_middle": "off",
                       "light.pool_pool_light_deep_end": "on", "sensor.pool_temp": "82", "sensor.spa_temp": "unknown"}
        self.calls = []

    def is_on(self, e):
        return self.states.get(e) == "on"

    def number(self, e):
        try:
            return float(self.states[e])
        except (KeyError, ValueError):
            return None

    async def call_service(self, domain, service, entity_id):
        self.calls.append((domain, service, entity_id))
        self.states[entity_id] = "on" if service == "turn_on" else "off"


class FakeAqualink:
    def __init__(self):
        self.state = AqualinkState(connected=True, rpm=2950, active_preset="Pool",
                                   presets=[{"index": 0, "label": "Pool", "rpm": 2950}, {"index": 6, "label": "Cloudy", "rpm": 2800}],
                                   waterfall_on=False, pool_temp=82.0, air_temp=63.0)
        self.calls = []
        self.fail = False

    async def set_preset(self, index):
        self._call(("preset", index))
        self.state.active_preset = "Cloudy" if index == 6 else "Pool"

    async def set_custom_rpm(self, rpm):
        self._call(("rpm", rpm))

    async def set_waterfall(self, on):
        self._call(("waterfall", on))
        self.state.waterfall_on = on

    def _call(self, item):
        if self.fail:
            raise AqualinkCommandError("cloud down")
        self.calls.append(item)


@pytest.fixture
def env(tmp_path):
    ha, aq, notifier = FakeHA(), FakeAqualink(), Notifier()
    runner = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"))
    spa = SpaSession(ha, runner)
    app = build_app(ha, aq, runner, spa, notifier)
    with TestClient(app) as client:
        yield client, ha, aq, runner


def test_health(env):
    client, *_ = env
    assert client.get("/api/health").json() == {"ok": True}


def test_state_snapshot(env):
    client, *_ = env
    s = client.get("/api/state").json()
    assert s["ha"]["switches"] == {"filter_pump": True, "spa": False, "spa_heat": False, "jet_pump": False, "pool_heat": False}
    assert s["ha"]["lights"] == {"light_shallow": True, "light_middle": False, "light_deep": True}
    assert s["ha"]["pool_temp"] == 82.0 and s["ha"]["spa_temp"] is None
    assert s["aqualink"]["rpm"] == 2950 and s["aqualink"]["air_temp"] == 63.0
    assert s["thermostat"]["settings"]["target"] == 100.0
    assert s["spa"] == {"label": "Off", "cooling_down": False}


def test_switch_routes_to_ha(env):
    client, ha, *_ = env
    r = client.post("/api/switch/spa_heat", json={"on": True})
    assert r.status_code == 200 and ha.calls == [("switch", "turn_on", "switch.spa_heater")]
    client.post("/api/switch/light_middle", json={"on": True})
    assert ha.calls[-1] == ("light", "turn_on", "light.pool_pool_light_middle")
    client.post("/api/switch/lights_all", json={"on": False})
    assert [c for c in ha.calls if c[0] == "light" and c[1] == "turn_off"] == [
        ("light", "turn_off", "light.pool_pool_light_shallow_end"),
        ("light", "turn_off", "light.pool_pool_light_middle"),
        ("light", "turn_off", "light.pool_pool_light_deep_end"),
    ]
    assert client.post("/api/switch/nope", json={"on": True}).status_code == 404


def test_waterfall_and_pump_route_to_aqualink(env):
    client, ha, aq, _ = env
    client.post("/api/switch/waterfall", json={"on": True})
    client.post("/api/pump/preset/6")
    client.post("/api/pump/rpm", json={"rpm": 1800})
    assert aq.calls == [("waterfall", True), ("preset", 6), ("rpm", 1800)]
    assert client.post("/api/pump/rpm", json={"rpm": 100}).status_code == 400
    aq.fail = True
    r = client.post("/api/pump/preset/0")
    assert r.status_code == 503 and "cloud down" in r.json()["error"]


def test_thermostat_update_and_validation(env):
    client, *_ = env
    r = client.post("/api/thermostat", json={"enabled": True, "target": 98})
    assert r.status_code == 200 and r.json()["settings"]["target"] == 98 and r.json()["settings"]["enabled"] is True
    assert client.post("/api/thermostat", json={"target": 200}).status_code == 400


def test_spa_start_and_end(env):
    client, ha, _, runner = env
    client.post("/api/spa/start")
    assert ha.calls[:2] == [("switch", "turn_on", "switch.spa_pump"), ("switch", "turn_on", "switch.spa_heater")]
    assert runner.settings.enabled is True
    client.post("/api/spa/end")
    assert ha.calls[-2:] == [("switch", "turn_off", "switch.spa_heater"), ("switch", "turn_off", "switch.pool_pump")]
    ha.states["switch.pool_pump"] = "on"  # the panel keeps the pump running through the heater cooldown
    assert client.get("/api/state").json()["spa"]["label"] == "Cooling down"


async def test_sse_stream_yields_snapshot_immediately():
    from app.main import sse_stream
    gen = sse_stream(lambda: {"ha": {"connected": True}}, Notifier())
    first = await gen.__anext__()
    await gen.aclose()
    assert first.startswith("data: ") and first.endswith("\n\n")
    assert json.loads(first[6:])["ha"]["connected"] is True



def test_index_served(env):
    client, *_ = env
    r = client.get("/")
    assert r.status_code == 200 and "Pool" in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_api.py -q`
Expected: FAIL with `ImportError: cannot import name 'build_app'`

- [ ] **Step 3: Implement the app**

Create a placeholder page so the index test can pass now (Task 10 replaces it): `pool_control/app/static/index.html` containing `<!doctype html><title>Pool</title><h1>Pool Control</h1>`.

`pool_control/app/main.py` (replace whole file):
```python
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.aqualink_auth import AqualinkAuth
from app.aqualink_client import AqualinkClient, AqualinkCommandError
from app.config import Config
from app.entities import ALL_ENTITY_IDS, LIGHTS, SENSORS, SWITCHES
from app.ha_client import HAClient, HAError
from app.notifier import Notifier
from app.settings import SettingsStore
from app.spa import SpaSession
from app.thermostat_runner import ThermostatRunner

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
RPM_MIN, RPM_MAX = 600, 3450


def snapshot(ha, aqualink, runner, spa) -> dict:
    return {
        "ha": {
            "connected": ha.connected,
            "switches": {name: ha.is_on(eid) for name, eid in SWITCHES.items()},
            "lights": {name: ha.is_on(eid) for name, eid in LIGHTS.items()},
            "pool_temp": ha.number(SENSORS["pool_temp"]),
            "spa_temp": ha.number(SENSORS["spa_temp"]),
        },
        "aqualink": aqualink.state.to_dict(),
        "thermostat": runner.to_dict(),
        "spa": {"label": spa.label(), "cooling_down": spa.cooling_down},
    }


async def sse_stream(snap, notifier: Notifier):
    """Yields one SSE frame now, then one per change, and at least one every 15 s."""
    while True:
        yield f"data: {json.dumps(snap())}\n\n"
        await notifier.wait(15)


def build_app(ha, aqualink, runner, spa, notifier: Notifier, lifespan=None) -> FastAPI:
    app = FastAPI(title="Pool Control", lifespan=lifespan)

    def snap() -> dict:
        return snapshot(ha, aqualink, runner, spa)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/state")
    async def state():
        return snap()

    @app.get("/api/events")
    async def events():
        return StreamingResponse(sse_stream(snap, notifier), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def ha_switch(domain: str, entity_id: str, on: bool) -> None:
        try:
            await ha.call_service(domain, "turn_on" if on else "turn_off", entity_id)
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))

    async def aqualink_call(coro) -> None:
        try:
            await coro
        except AqualinkCommandError as exc:
            raise HTTPException(503, detail=str(exc))

    @app.post("/api/switch/{name}")
    async def switch(name: str, body: dict):
        on = bool(body.get("on"))
        if name in SWITCHES:
            await ha_switch("switch", SWITCHES[name], on)
        elif name in LIGHTS:
            await ha_switch("light", LIGHTS[name], on)
        elif name == "lights_all":
            for entity_id in LIGHTS.values():
                await ha_switch("light", entity_id, on)
        elif name == "waterfall":
            await aqualink_call(aqualink.set_waterfall(on))
        else:
            raise HTTPException(404, detail=f"unknown switch {name}")
        notifier.notify()
        runner.wake()
        return snap()

    @app.post("/api/pump/preset/{index}")
    async def pump_preset(index: int):
        await aqualink_call(aqualink.set_preset(index))
        notifier.notify()
        return snap()

    @app.post("/api/pump/rpm")
    async def pump_rpm(body: dict):
        try:
            rpm = int(body.get("rpm"))
        except (TypeError, ValueError):
            raise HTTPException(400, detail="rpm must be a number")
        if not (RPM_MIN <= rpm <= RPM_MAX):
            raise HTTPException(400, detail=f"rpm must be between {RPM_MIN} and {RPM_MAX}")
        await aqualink_call(aqualink.set_custom_rpm(rpm))
        notifier.notify()
        return snap()

    @app.post("/api/thermostat")
    async def thermostat(body: dict):
        try:
            await runner.update_settings(**body)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        notifier.notify()
        return runner.to_dict()

    @app.post("/api/spa/start")
    async def spa_start():
        try:
            await spa.start()
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))
        notifier.notify()
        return snap()

    @app.post("/api/spa/end")
    async def spa_end():
        try:
            await spa.end()
        except HAError as exc:
            raise HTTPException(503, detail=str(exc))
        notifier.notify()
        return snap()

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _build_production_app() -> FastAPI:
    config = Config.from_env()
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    notifier = Notifier()
    http = httpx.AsyncClient(follow_redirects=True)
    auth = AqualinkAuth(http, config.iaqualink_email, config.iaqualink_password)
    touch_link_cache: dict[str, str] = {}

    async def touch_link() -> str:
        if "link" not in touch_link_cache:
            touch_link_cache["link"] = await auth.discover_touch_link(config.iaqualink_serial)
        return touch_link_cache["link"]

    runner_holder: list[ThermostatRunner] = []

    def on_ha_change() -> None:
        notifier.notify()
        if runner_holder:
            runner_holder[0].wake()  # re-evaluate the thermostat on any state change

    ha = HAClient(config.ha_ws_url, config.ha_token, ALL_ENTITY_IDS, on_change=on_ha_change)
    aqualink = AqualinkClient(auth, http, touch_link, on_change=notifier.notify)
    runner = ThermostatRunner(ha, SettingsStore(config.data_dir / "settings.json"))
    runner_holder.append(runner)
    spa = SpaSession(ha, runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ha.start()
        if config.iaqualink_email and config.iaqualink_password:
            await aqualink.start()
        else:
            LOGGER.warning("iAqualink email/password not set; pump speed and waterfall disabled")
        thermostat_task = asyncio.create_task(runner.run(60), name="thermostat")
        try:
            yield
        finally:
            thermostat_task.cancel()
            await aqualink.stop()
            await ha.stop()
            await http.aclose()

    return build_app(ha, aqualink, runner, spa, notifier, lifespan=lifespan)


if Config.from_env().testing:
    app = FastAPI()  # tests build their own app via build_app()
else:
    app = _build_production_app()
```

- [ ] **Step 4: Run all tests**

Run: `python3 -m pytest -q`
Expected: all passing (`8` in test_api plus earlier tasks). Do not call `/api/events` through `TestClient`; it never ends and the test client would hang.

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/main.py pool_control/app/static/index.html tests/test_api.py
git commit -m "Add JSON API, SSE state stream and app wiring

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: The page (HTML, CSS, JS)

**Files:**
- Create/replace: `pool_control/app/static/index.html`, `pool_control/app/static/style.css`, `pool_control/app/static/app.js`
- Test: add `test_static_assets_served` to `tests/test_api.py`

**Interfaces:**
- Consumes: the snapshot shape and endpoints from Task 9. All URLs relative (`api/state`, `static/app.js`), never leading `/`.

Behaviour:
- Connect to `api/events` with `EventSource`; fall back to polling `api/state` every 5 s if it errors.
- Render from the snapshot. Buttons carry a `pending` class from tap until the next snapshot or 10 s; on error show a toast with the `error` text.
- Pump preset and waterfall buttons are disabled with a note when `aqualink.connected` is false.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:
```python
def test_static_assets_served(env):
    client, *_ = env
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    html = client.get("/").text
    assert 'static/app.js' in html and 'static/style.css' in html and "/static/" not in html
```

Run: `python3 -m pytest tests/test_api.py::test_static_assets_served -q` → Expected: FAIL (404 for app.js).

- [ ] **Step 2: Write the page**

`pool_control/app/static/index.html`:
```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Pool</title>
<link rel="stylesheet" href="static/style.css">
</head>
<body>
<main id="app">
  <section class="readings">
    <div class="reading"><span class="label">Pool</span><span class="value" id="pool-temp">--</span></div>
    <div class="reading"><span class="label">Air</span><span class="value" id="air-temp">--</span></div>
    <div class="reading" id="spa-reading" hidden><span class="label">Spa</span><span class="value" id="spa-temp">--</span></div>
  </section>
  <p class="pumpline" id="pumpline">Pump --</p>

  <section class="card" id="spa-card">
    <header><h2>Spa</h2><span class="session" id="spa-label">--</span></header>
    <div class="row two">
      <button class="big primary" data-action="spa-start">Start spa</button>
      <button class="big" data-action="spa-end">End spa</button>
    </div>
    <div class="row three">
      <button class="toggle" data-switch="spa">Spa</button>
      <button class="toggle" data-switch="spa_heat">Spa Heat</button>
      <button class="toggle" data-switch="jet_pump">Jet Pump</button>
    </div>
    <div class="thermo">
      <label class="enable"><input type="checkbox" id="thermo-enabled"> Thermostat</label>
      <div class="stepper">
        <button data-thermo="target" data-delta="-1">−</button>
        <span id="thermo-target">--°</span>
        <button data-thermo="target" data-delta="1">+</button>
      </div>
      <details>
        <summary>Settings</summary>
        <div class="stepper small"><span>Buffer</span>
          <button data-thermo="buffer" data-delta="-1">−</button><span id="thermo-buffer">--</span><button data-thermo="buffer" data-delta="1">+</button></div>
        <div class="stepper small"><span>Off early</span>
          <button data-thermo="off_early" data-delta="-1">−</button><span id="thermo-off-early">--</span><button data-thermo="off_early" data-delta="1">+</button></div>
      </details>
      <p class="status" id="thermo-status">--</p>
    </div>
  </section>

  <section class="card" id="pump-card">
    <header><h2>Pump</h2><button class="toggle" data-switch="filter_pump">Filter Pump</button></header>
    <p class="note" id="aq-note" hidden>iAqualink not connected</p>
    <div class="grid" id="presets"></div>
    <form class="row custom" id="rpm-form">
      <input type="number" id="rpm-input" min="600" max="3450" step="50" placeholder="Custom RPM" inputmode="numeric">
      <button type="submit">Set</button>
    </form>
  </section>

  <section class="card" id="equipment-card">
    <header><h2>Equipment</h2></header>
    <div class="row two">
      <button class="toggle" data-switch="pool_heat">Pool Heat</button>
      <button class="toggle aq" data-switch="waterfall">Waterfall</button>
    </div>
    <div class="row">
      <button class="toggle" data-switch="lights_all">All pool lights</button>
    </div>
    <div class="row three">
      <button class="toggle" data-switch="light_shallow">Shallow</button>
      <button class="toggle" data-switch="light_middle">Middle</button>
      <button class="toggle" data-switch="light_deep">Deep</button>
    </div>
  </section>

  <footer>
    <span id="ha-health">HA: --</span> · <span id="aq-health">iAqualink: --</span>
    <p id="last-action"></p>
  </footer>
</main>
<div id="toast" hidden></div>
<script src="static/app.js"></script>
</body>
</html>
```

`pool_control/app/static/style.css`:
```css
:root { --bg:#0f172a; --card:#1e293b; --text:#f1f5f9; --muted:#94a3b8; --accent:#38bdf8; --on:#22c55e; --warn:#f59e0b; --off:#334155; }
@media (prefers-color-scheme: light) { :root { --bg:#f1f5f9; --card:#ffffff; --text:#0f172a; --muted:#64748b; --off:#e2e8f0; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font: 17px/1.3 -apple-system, system-ui, sans-serif; padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }
main { max-width: 520px; margin: 0 auto; padding: 12px 12px 32px; }
.readings { display:flex; gap:12px; justify-content:space-around; padding: 8px 0; }
.reading { text-align:center; }
.reading .label { display:block; color:var(--muted); font-size:14px; text-transform:uppercase; letter-spacing:.06em; }
.reading .value { font-size:44px; font-weight:600; }
.pumpline { text-align:center; color:var(--muted); margin: 0 0 12px; }
.card { background:var(--card); border-radius:16px; padding:14px; margin-bottom:14px; box-shadow: 0 1px 3px rgba(0,0,0,.2); }
.card header { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
.card h2 { margin:0; font-size:20px; }
.session { color:var(--accent); font-weight:600; }
.row { display:grid; gap:10px; margin-bottom:10px; }
.row.two { grid-template-columns: 1fr 1fr; }
.row.three { grid-template-columns: 1fr 1fr 1fr; }
button { font: inherit; color:var(--text); background:var(--off); border:0; border-radius:12px; min-height:48px; padding:10px 12px; cursor:pointer; touch-action: manipulation; }
button.big { min-height:56px; font-size:18px; font-weight:600; }
button.primary { background:var(--accent); color:#0f172a; }
button.toggle.on { background:var(--on); color:#052e16; }
button.pending { opacity:.55; }
button:disabled { opacity:.35; cursor:not-allowed; }
.grid { display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-bottom:10px; }
.grid button { display:flex; flex-direction:column; align-items:center; min-height:64px; }
.grid button small { color:var(--muted); font-size:14px; }
.grid button.active { background:var(--accent); color:#0f172a; }
.grid button.active small { color:#0f172a; }
.custom { grid-template-columns: 1fr auto; }
input[type=number] { font:inherit; padding:10px 12px; border-radius:12px; border:1px solid var(--off); background:var(--bg); color:var(--text); min-height:48px; }
.thermo { border-top:1px solid var(--off); padding-top:10px; }
.enable { display:flex; align-items:center; gap:8px; font-weight:600; margin-bottom:8px; }
.enable input { width:22px; height:22px; }
.stepper { display:flex; align-items:center; justify-content:center; gap:14px; margin:6px 0; }
.stepper span { min-width:64px; text-align:center; font-size:26px; font-weight:600; }
.stepper.small span { font-size:17px; font-weight:500; min-width:56px; }
.stepper.small span:first-child { min-width:90px; text-align:left; color:var(--muted); }
.stepper button { min-width:48px; }
details summary { color:var(--muted); cursor:pointer; margin:6px 0; }
.status, .note { color:var(--muted); margin:6px 0 0; text-align:center; }
.note { color:var(--warn); }
footer { color:var(--muted); font-size:14px; text-align:center; }
#toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#b91c1c; color:#fff; padding:10px 16px; border-radius:10px; max-width:90%; }
```

`pool_control/app/static/app.js`:
```js
(() => {
  const $ = (id) => document.getElementById(id);
  let state = null;
  const pendingUntil = new Map();

  function fmtTemp(v) { return v == null ? "--" : `${Math.round(v)}°`; }

  async function post(path, body) {
    const el = document.activeElement;
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      if (!r.ok) {
        let msg = `Error ${r.status}`;
        try { msg = (await r.json()).error || msg; } catch (_) {}
        toast(msg);
        clearPending();
      }
    } catch (e) {
      toast("Network error");
      clearPending();
    }
  }

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, 4000);
  }

  function setPending(btn) {
    btn.classList.add("pending");
    pendingUntil.set(btn, Date.now() + 10000);
  }
  function clearPending() {
    for (const [btn] of pendingUntil) btn.classList.remove("pending");
    pendingUntil.clear();
  }

  function render(s) {
    state = s;
    clearPending();
    $("pool-temp").textContent = fmtTemp(s.ha.pool_temp ?? s.aqualink.pool_temp);
    $("air-temp").textContent = fmtTemp(s.aqualink.air_temp);
    const spaOn = s.ha.switches.spa;
    $("spa-reading").hidden = !spaOn;
    $("spa-temp").textContent = fmtTemp(s.ha.spa_temp ?? s.aqualink.spa_temp);

    const aq = s.aqualink;
    $("pumpline").textContent = !s.ha.switches.filter_pump ? "Pump off"
      : aq.rpm != null ? `Pump ${aq.active_preset ? aq.active_preset + " · " : ""}${aq.rpm} RPM` : "Pump on";

    $("spa-label").textContent = s.spa.label;
    for (const btn of document.querySelectorAll("[data-switch]")) {
      const name = btn.dataset.switch;
      let on;
      if (name === "waterfall") on = aq.waterfall_on;
      else if (name === "lights_all") on = Object.values(s.ha.lights).every(Boolean);
      else if (name in s.ha.switches) on = s.ha.switches[name];
      else on = s.ha.lights[name];
      btn.classList.toggle("on", !!on);
      btn.disabled = name === "waterfall" ? !aq.connected : !s.ha.connected;
    }

    const t = s.thermostat;
    $("thermo-enabled").checked = t.settings.enabled;
    $("thermo-target").textContent = `${t.settings.target}°`;
    $("thermo-buffer").textContent = `${t.settings.buffer}°`;
    $("thermo-off-early").textContent = `${t.settings.off_early}°`;
    $("thermo-status").textContent = t.status;

    $("aq-note").hidden = aq.connected;
    const grid = $("presets");
    grid.innerHTML = "";
    for (const p of aq.presets) {
      const b = document.createElement("button");
      b.innerHTML = `<span>${p.label}</span><small>${p.rpm} RPM</small>`;
      b.disabled = !aq.connected;
      b.classList.toggle("active", p.label === aq.active_preset);
      b.addEventListener("click", () => { setPending(b); post(`api/pump/preset/${p.index}`); });
      grid.appendChild(b);
    }
    $("rpm-form").querySelector("button").disabled = !aq.connected;

    $("ha-health").textContent = `HA: ${s.ha.connected ? "connected" : "disconnected"}`;
    $("aq-health").textContent = `iAqualink: ${aq.connected ? "connected" : (aq.error ? "error" : "disconnected")}`;
    const la = t.last_action;
    $("last-action").textContent = la ? `Heater ${la.action} at ${Math.round(la.temp)}° (${la.time.slice(11, 16)}) — ${la.reason}` : "";
  }

  document.body.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn || btn.disabled) return;
    if (btn.dataset.switch) {
      const name = btn.dataset.switch;
      setPending(btn);
      post(`api/switch/${name}`, { on: !btn.classList.contains("on") });
    } else if (btn.dataset.action === "spa-start") {
      setPending(btn); post("api/spa/start");
    } else if (btn.dataset.action === "spa-end") {
      setPending(btn); post("api/spa/end");
    } else if (btn.dataset.thermo) {
      const key = btn.dataset.thermo;
      const cur = state.thermostat.settings[key];
      setPending(btn);
      post("api/thermostat", { [key]: cur + Number(btn.dataset.delta) });
    }
  });

  $("thermo-enabled").addEventListener("change", (ev) => post("api/thermostat", { enabled: ev.target.checked }));

  $("rpm-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const rpm = Number($("rpm-input").value);
    if (!rpm) return;
    setPending(ev.submitter || $("rpm-form").querySelector("button"));
    post("api/pump/rpm", { rpm });
  });

  setInterval(() => {
    const now = Date.now();
    for (const [btn, until] of pendingUntil) if (until < now) { btn.classList.remove("pending"); pendingUntil.delete(btn); }
  }, 1000);

  function connect() {
    let es;
    try { es = new EventSource("api/events"); } catch (_) { return poll(); }
    es.onmessage = (ev) => render(JSON.parse(ev.data));
    es.onerror = () => { es.close(); setTimeout(connect, 3000); };
  }
  async function poll() {
    try { render(await (await fetch("api/state")).json()); } catch (_) {}
    setTimeout(poll, 5000);
  }
  fetch("api/state").then((r) => r.json()).then(render).catch(() => {});
  connect();
})();
```

- [ ] **Step 3: Run the tests**

Run: `python3 -m pytest -q`
Expected: all passing.

- [ ] **Step 4: Look at it in a browser**

Run: `. .venv/bin/activate && POOL_CONTROL_TESTING=1 python3 -c "print('use run_local.sh from Task 11 for a live view')"` — the real visual check happens in Task 11 with live data. For now open `pool_control/app/static/index.html` directly in a browser to check layout at phone width (the buttons render with placeholder text). Fix any obvious layout problems.

- [ ] **Step 5: Commit**

```bash
git add pool_control/app/static tests/test_api.py
git commit -m "Add the pool control page

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Local dev runner, live integration test, README

**Files:**
- Create: `run_local.sh`, `tests/test_integration_live.py`, `README.md`

**Interfaces:**
- Consumes: everything.

- [ ] **Step 1: Write the opt-in live integration test**

`tests/test_integration_live.py`:
```python
"""Opt-in test against the real iAqualink cloud. Changes the pump speed for a few seconds.

Run with:
  IAQUALINK_EMAIL=... IAQUALINK_PASSWORD=... POOL_LIVE_TEST=1 python3 -m pytest tests/test_integration_live.py -s
"""
import asyncio
import os

import httpx
import pytest

from app.aqualink_auth import AqualinkAuth
from app.aqualink_client import AqualinkClient

pytestmark = pytest.mark.skipif(os.environ.get("POOL_LIVE_TEST") != "1", reason="set POOL_LIVE_TEST=1 to run")


async def test_live_login_presets_and_round_trip():
    email, password = os.environ["IAQUALINK_EMAIL"], os.environ["IAQUALINK_PASSWORD"]
    async with httpx.AsyncClient(follow_redirects=True) as http:
        auth = AqualinkAuth(http, email, password)
        link = await auth.discover_touch_link(os.environ.get("IAQUALINK_SERIAL") or None)

        async def touch_link():
            return link

        client = AqualinkClient(auth, http, touch_link, on_change=lambda: None, refresh_interval=3600)
        await client.start()
        await asyncio.wait_for(client.wait_connected(), 30)
        await client.refresh_vsp()
        print("presets:", client.state.presets, "rpm:", client.state.rpm, "active:", client.state.active_preset)
        assert client.state.presets and client.state.rpm

        original = next(p for p in client.state.presets if p["label"] == client.state.active_preset)
        other = next(p for p in client.state.presets if p["index"] != original["index"])
        await client.set_preset(other["index"])
        assert client.state.active_preset == other["label"]
        await client.set_preset(original["index"])
        assert client.state.active_preset == original["label"]
        print("air temp:", client.state.air_temp, "pool temp:", client.state.pool_temp, "waterfall:", client.state.waterfall_on)
        await client.stop()
```

- [ ] **Step 2: Run it against the real cloud once**

Ask the user for the iAqualink email and password if not already in a local `.env` (gitignored). Then:

Run: `set -a; . ./.env; set +a; POOL_LIVE_TEST=1 python3 -m pytest tests/test_integration_live.py -s -q`
Expected: PASS, printing the eight presets. If `air_temp` prints `None`, check the printed Home page `info` indices against `docs/iaqualink-webtouch-protocol.md` ("Home page info values") and adjust `HOME_INFO_*` constants in `aqualink_client.py`.

- [ ] **Step 3: Write the local dev runner**

`run_local.sh`:
```bash
#!/usr/bin/env bash
# Run the add-on on this Mac against the real Home Assistant and iAqualink.
# Needs a .env file with IAQUALINK_EMAIL, IAQUALINK_PASSWORD, HA_TOKEN (long-lived token) and optionally HA_URL.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export DATA_DIR="${DATA_DIR:-./data}"
export PYTHONPATH=pool_control
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8099 --reload --reload-dir pool_control
```

Run: `chmod +x run_local.sh && ./run_local.sh` then open `http://127.0.0.1:8099/` in a browser (phone width too). Verify: temps match the iAqualink app, preset grid shows eight presets with the active one highlighted, toggling a light works, and the thermostat status reads "Thermostat off". Stop with Ctrl-C.

- [ ] **Step 4: Write the README**

`README.md`:
```markdown
# Pool Control

A Home Assistant add-on with a simple phone-friendly page for the pool and spa:
pump speed presets (via the iAqualink Web Interface protocol), everyday equipment
toggles, and a spa thermostat that cycles the propane spa heater.

Design: `docs/superpowers/specs/2026-09-14-pool-control-design.md`.
Protocol notes: `docs/iaqualink-webtouch-protocol.md`.

## Install into Home Assistant (local add-on)

1. Enable the Samba share add-on (or SSH) on Home Assistant.
2. Copy the `pool_control/` folder (the one containing `config.yaml`) into the Home Assistant `addons` share, so you have `addons/pool_control/config.yaml`.
3. In Home Assistant: Settings → Add-ons → Add-on store → ⋮ menu → Check for updates. "Pool Control" appears under Local add-ons.
4. Install it. On the Configuration tab set `iaqualink_email` and `iaqualink_password` (the same login as the iAquaLink app). Leave `serial` blank unless you have more than one iAqua device.
5. Start the add-on and turn on "Show in sidebar". Open "Pool" from the sidebar or the Companion app.

## Go-live checklist

- Readings on the page match the iAquaLink app.
- Each toggle changes the real equipment (watch it).
- Pump presets change speed and the RPM line updates.
- Disable (do not delete) the old automation "Turn_Off_Spa_When_Temperature_Too_High"; it fights the thermostat.
- Run one spa session with the thermostat on; note how far the spa overshoots the target after the heater turns off, and set "Off early" to that amount.

## Develop on a Mac

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python3 -m pytest -q
cp .env.example .env   # fill in IAQUALINK_EMAIL, IAQUALINK_PASSWORD, HA_TOKEN
./run_local.sh          # http://127.0.0.1:8099/
```

Live cloud test (moves the pump for a few seconds):
`set -a; . ./.env; set +a; POOL_LIVE_TEST=1 python3 -m pytest tests/test_integration_live.py -s`

## Updating the add-on

Copy the changed files to `addons/pool_control/`, bump `version` in `config.yaml`, then in the add-on page choose Rebuild, then Start.
```

Also create `.env.example`:
```
IAQUALINK_EMAIL=
IAQUALINK_PASSWORD=
HA_URL=http://homeassistant.local:8123
HA_TOKEN=
```

- [ ] **Step 5: Run everything and commit**

Run: `python3 -m pytest -q`
Expected: all passing (live test skipped unless opted in).

```bash
git add run_local.sh tests/test_integration_live.py README.md .env.example
git commit -m "Add local runner, live integration test and README

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Install into Home Assistant and verify with the user

**Files:** none new. This is the deployment and go-live task.

- [ ] **Step 1: Copy the add-on folder to Home Assistant** — via the Samba share (`smb://homeassistant.local/addons`) or SSH: the target is `addons/pool_control/`. Confirm `config.yaml`, `Dockerfile`, `build.yaml`, `run.sh`, `requirements.txt`, and `app/` are present.
- [ ] **Step 2: Install and configure** — Settings → Add-ons → Add-on store → ⋮ → Check for updates → Local add-ons → Pool Control → Install. Set the iAqualink email and password on the Configuration tab. Start. Watch the Log tab for "Home Assistant websocket connected" and "WebTouch session opened".
- [ ] **Step 3: Verify from the sidebar and the Companion app** — readings, one light toggle, one pump preset round trip, waterfall on/off.
- [ ] **Step 4: Disable the old automation** — Settings → Automations → "Turn_Off_Spa_When_Temperature_Too_High" → toggle off.
- [ ] **Step 5: First spa session with the user** — Start spa, watch the thermostat status line, confirm the heater cycles at the thresholds, note the overshoot, set Off-early.
- [ ] **Step 6: Commit any fixes found during go-live** with the usual message format, and update the protocol notes if any index or command differed from the capture.
