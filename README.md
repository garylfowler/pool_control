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
