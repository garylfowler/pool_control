from app.thermostat import MIN_CYCLE_SECONDS, ThermostatInput, evaluate


def make(**overrides):
    base = dict(enabled=True, spa_on=True, pump_on=True, spa_temp=90.0, heater_on=False,
                target=100.0, buffer=3.0, off_early=0.0, now=10_000.0, last_switch_at=None)
    base.update(overrides)
    return ThermostatInput(**base)


def test_no_session_never_turns_on_but_still_caps():
    # outside a session (no Start spa) the thermostat is a safety cap only
    d = evaluate(make(enabled=False, spa_temp=90.0, heater_on=False))
    assert d.action == "hold" and d.status == "Idle 90°"
    d = evaluate(make(enabled=False, spa_temp=100.0, heater_on=True))
    assert d.action == "off" and d.reason == "Reached 100°"
    d = evaluate(make(enabled=False, spa_temp=98.0, heater_on=True))
    assert d.action == "hold" and d.status == "Heating 98°"


def test_never_turns_on_with_the_pump_off():
    d = evaluate(make(pump_on=False, spa_temp=90.0, heater_on=False))
    assert d.action == "hold" and d.status == "Pump is off"
    # but still turns off when hot
    assert evaluate(make(pump_on=False, spa_temp=100.0, heater_on=True)).action == "off"


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


def test_min_cycle_time_blocks_turning_on_but_never_turning_off():
    d = evaluate(make(spa_temp=90.0, heater_on=False, now=10_000.0, last_switch_at=10_000.0 - MIN_CYCLE_SECONDS + 1))
    assert d.action == "hold" and d.status == "Waiting (min. cycle time)"
    d = evaluate(make(spa_temp=90.0, heater_on=False, now=10_000.0, last_switch_at=10_000.0 - MIN_CYCLE_SECONDS))
    assert d.action == "on"
    # turning off is always allowed: a hot spa is never kept hot by the cycle rule
    d = evaluate(make(spa_temp=100.0, heater_on=True, now=10_000.0, last_switch_at=10_000.0 - 10))
    assert d.action == "off"


def test_manual_on_respected_until_off_threshold():
    # user turned heater on while at 98 (between thresholds): hold, do not fight it
    assert evaluate(make(spa_temp=98.0, heater_on=True)).action == "hold"
    # user turned heater off at 98: hold until it drops to 97
    assert evaluate(make(spa_temp=98.0, heater_on=False)).action == "hold"


def test_inverted_thresholds_hold_instead_of_oscillating():
    # off_early >= buffer would put the off threshold at or below the on threshold:
    # at a constant temperature the heater would switch on and off forever.
    d = evaluate(make(spa_temp=98.0, heater_on=False, buffer=1.0, off_early=3.0))
    assert d.action == "hold" and d.status == "Invalid settings (off-early ≥ buffer)"
    d = evaluate(make(spa_temp=98.0, heater_on=False, buffer=2.0, off_early=2.0))
    assert d.action == "hold" and d.status == "Invalid settings (off-early ≥ buffer)"
    # the protective off still works with bad settings
    assert evaluate(make(spa_temp=98.0, heater_on=True, buffer=1.0, off_early=3.0)).action == "off"


def test_reasons_include_temperature():
    assert evaluate(make(spa_temp=100.0, heater_on=True)).reason == "Reached 100°"
    assert evaluate(make(spa_temp=96.0, heater_on=False)).reason == "Dropped to 96°"
