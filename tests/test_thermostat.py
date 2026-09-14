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
