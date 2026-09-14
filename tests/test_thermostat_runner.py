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
