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
