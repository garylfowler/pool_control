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
