import json

import pytest

from app.settings import SettingsStore, ThermostatSettings


def test_defaults():
    s = ThermostatSettings()
    assert s.to_dict() == {"enabled": False, "target": 94.0, "buffer": 3.0, "off_early": 0.0}


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


def test_with_changes_rejects_off_early_at_or_above_buffer():
    s = ThermostatSettings()
    # off_early >= buffer inverts the hysteresis band: the heater would cycle at a fixed temperature
    with pytest.raises(ValueError, match="Off early must be less than Buffer"):
        s.with_changes(buffer=1, off_early=3)
    with pytest.raises(ValueError, match="Off early must be less than Buffer"):
        s.with_changes(buffer=2, off_early=2)
    # raising off_early past the stored buffer is rejected too
    narrow = s.with_changes(buffer=1)
    with pytest.raises(ValueError, match="Off early must be less than Buffer"):
        narrow.with_changes(off_early=1)
    assert narrow.with_changes(off_early=0.5).off_early == 0.5


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
