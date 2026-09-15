from app.settings import SettingsStore
from app.thermostat_runner import STALE_TEMPERATURE_SECONDS, ThermostatRunner


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
    states.setdefault("switch.pool_pump", "on")
    ha = FakeHA(**states)
    clock = {"now": 5000.0}
    runner = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"), clock=lambda: clock["now"])
    return ha, runner, clock


async def test_no_session_by_default_does_not_turn_on(tmp_path):
    ha, runner, _ = make(tmp_path, **{"switch.spa_pump": "on", "switch.pool_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    d = await runner.evaluate_and_act()
    assert d.action == "hold" and ha.calls == [] and runner.status == "Idle 90°"


async def test_safety_cap_applies_without_a_session(tmp_path):
    ha, runner, _ = make(tmp_path, **{"switch.spa_pump": "on", "switch.pool_pump": "on", "sensor.spa_temp": "95", "switch.spa_heater": "on"})
    d = await runner.evaluate_and_act()  # default target is 94: already too hot
    assert d.action == "off" and ha.calls == [("switch", "turn_off", "switch.spa_heater")]


async def test_stale_guard_applies_without_a_session(tmp_path):
    ha, runner, clock = make(tmp_path, **{"switch.spa_pump": "on", "switch.pool_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "on"})
    await runner.evaluate_and_act()
    clock["now"] += STALE_TEMPERATURE_SECONDS
    d = await runner.evaluate_and_act()
    assert d.action == "off" and runner.status == "Spa temperature stale"


async def test_turns_heater_on_and_records_action(tmp_path):
    ha, runner, clock = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    await runner.update_settings(enabled=True)
    d = await runner.evaluate_and_act()
    assert d.action == "on" and ha.calls == [("switch", "turn_on", "switch.spa_heater")]
    assert runner.last_switch_at == 5000.0
    assert runner.last_action["action"] == "on" and runner.last_action["temp"] == 90.0 and runner.last_action["reason"] == "Dropped to 90°"
    # reaching the 94 target turns the heater off even inside the 5-minute window
    ha.states["sensor.spa_temp"] = "94"
    clock["now"] += 100
    d = await runner.evaluate_and_act()
    assert d.action == "off" and ha.calls[-1] == ("switch", "turn_off", "switch.spa_heater")
    # dropping below target - buffer right away: turning back on waits for the cycle time
    ha.states["sensor.spa_temp"] = "90"
    clock["now"] += 100
    d = await runner.evaluate_and_act()
    assert d.action == "hold" and runner.status == "Waiting (min. cycle time)"
    clock["now"] += 300
    d = await runner.evaluate_and_act()
    assert d.action == "on"


async def test_failed_call_is_logged_and_does_not_update_last_switch(tmp_path):
    ha, runner, _ = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    await runner.update_settings(enabled=True)
    ha.fail = True
    d = await runner.evaluate_and_act()
    assert d.action == "on" and runner.last_switch_at is None and "failed" in runner.status.lower()


async def test_holds_when_home_assistant_is_disconnected(tmp_path):
    ha, runner, _ = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    await runner.update_settings(enabled=True)
    ha.connected = False
    d = await runner.evaluate_and_act()
    assert d.action == "hold" and ha.calls == [] and runner.status == "Home Assistant disconnected"


async def test_stale_spa_temperature_turns_the_heater_off_then_holds(tmp_path):
    ha, runner, clock = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "95", "switch.spa_heater": "on"})
    await runner.update_settings(enabled=True, target=100)  # 95 is below target: heating normally
    await runner.evaluate_and_act()  # first look: records the reading
    assert ha.calls == []
    clock["now"] += STALE_TEMPERATURE_SECONDS
    d = await runner.evaluate_and_act()
    assert d.action == "off" and ha.calls == [("switch", "turn_off", "switch.spa_heater")]
    assert runner.status == "Spa temperature stale"
    # still stale: do not switch back on, even though 95 is below the on threshold
    clock["now"] += 600
    d = await runner.evaluate_and_act()
    assert d.action == "hold" and runner.status == "Spa temperature stale" and len(ha.calls) == 1
    # the sensor moves again: normal rules resume
    ha.states["sensor.spa_temp"] = "94"
    d = await runner.evaluate_and_act()
    assert d.action == "on" and ha.calls[-1] == ("switch", "turn_on", "switch.spa_heater")


async def test_a_changing_spa_temperature_never_trips_the_stale_guard(tmp_path):
    ha, runner, clock = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "99", "switch.spa_heater": "on"})
    await runner.update_settings(enabled=True, target=104)
    for step in range(6):  # 3000 s of heating, well past the stale window
        ha.states["sensor.spa_temp"] = str(99 + step * 0.5)
        clock["now"] += 600
        d = await runner.evaluate_and_act()
        assert d.action == "hold" and runner.status.startswith("Heating")
    assert ha.calls == []


async def test_last_switch_at_survives_a_restart(tmp_path):
    ha, runner, clock = make(tmp_path, **{"switch.spa_pump": "on", "sensor.spa_temp": "90", "switch.spa_heater": "off"})
    await runner.update_settings(enabled=True)
    await runner.evaluate_and_act()
    assert runner.last_switch_at == 5000.0
    # a fresh runner on the same data dir, as after an add-on restart; the heater was
    # switched off by hand meanwhile and the water is below target - buffer again
    ha.states["switch.spa_heater"] = "off"
    again = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"), clock=lambda: clock["now"])
    assert again.last_switch_at == 5000.0
    clock["now"] += 100
    d = await again.evaluate_and_act()
    assert d.action == "hold" and again.status == "Waiting (min. cycle time)"
    clock["now"] += 300
    assert (await again.evaluate_and_act()).action == "on"


async def test_settings_persist(tmp_path):
    ha, runner, _ = make(tmp_path)
    await runner.update_settings(target=96, buffer=2)
    again = ThermostatRunner(ha, SettingsStore(tmp_path / "s.json"))
    assert again.settings.target == 96 and again.settings.buffer == 2


async def test_to_dict_shape(tmp_path):
    ha, runner, _ = make(tmp_path)
    d = runner.to_dict()
    assert set(d) == {"settings", "status", "last_action"}
    assert d["settings"]["target"] == 94
