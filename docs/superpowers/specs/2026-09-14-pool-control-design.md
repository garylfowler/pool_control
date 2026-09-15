# Pool Control — Design Spec

Date: 2026-09-14
Status: approved in discussion, pending written review

## 1. Goal

A simple, phone-friendly pool control page, delivered as a Home Assistant add-on, that:

1. Controls the variable speed pool pump (VSP) presets, which the Home Assistant iAqualink integration does not expose.
2. Gives one-tap control of the everyday pool and spa equipment.
3. Runs a spa thermostat that holds the spa at a target temperature by cycling the propane spa heater, because the panel's own spa set point does not stop the heater.

Out of scope: replacing the iAqualink integration, editing existing Home Assistant automations, schedules, chemistry, or anything not listed in section 6.

## 2. Environment (as found on 2026-09-14)

- Home Assistant OS 2026.6.4 with Supervisor, running in VirtualBox on a Windows PC. Reachable at `http://homeassistant.local:8123` (192.168.4.201).
- Accessed from an iPhone through the Home Assistant Companion app, including away from home.
- iAqualink integration (`iaqualink`) exposes:
  - Switches: `switch.pool_pump` (Filter Pump), `switch.spa_pump` (Spa mode), `switch.spa_heater`, `switch.pool_heater`, `switch.solar_heater`, `switch.jet_pump`.
  - Sensors: `sensor.pool_temp`, `sensor.spa_temp` (reads `unknown` unless Spa mode is on), `climate.pool`, `climate.spa`.
  - No pump speed entity, no waterfall, no air temp.
- Pool lights are three Bond lights: `light.pool_pool_light_shallow_end`, `light.pool_pool_light_middle`, `light.pool_pool_light_deep_end`.
- Panel: Jandy AquaLink RS ("iaqua" device, serial `<serial redacted>`, named "Fowler Pool"). Pool heat is a heat pump; spa heat is a propane heater. The Spa Heat thermostat on the panel is effectively tied to the pool set point, so the spa keeps heating.
- Existing automation `automation.turn_off_spa_when_temperature_too_high` shuts off the spa and jet pumps when `sensor.spa_temp` exceeds `input_number.temperature` minus 1. It conflicts with the new thermostat and must be disabled (not deleted) at go-live.

## 3. iAqualink WebTouch protocol

Full capture is in `docs/iaqualink-webtouch-protocol.md`. Summary:

- Login: `POST https://prod.zodiac-io.com/users/v1/login` with email and password. Yields an `idToken` JWT (about 1 hour) and a refresh token. The exact request body (including the public `apiKey` field) was not captured because the browser was already signed in; verify it against the open-source `iaqualink` Python library during implementation, and confirm with one real login before building on it.
- Device list: `https://prm.iaqualink.net/v2` device API gives each device's `touchLink`.
- Session: `GET https://prm.iaqualink.net/v2/webtouch/init?actionID=<touchLink>` with header `Authorization: <idToken>`. Returns JSON with `serverConnection` (stream URL), `masterID`, `masterStart`, `masterSTB`, `masterReset`, `systemType`.
- Stream: long-lived `GET <serverConnection>` (cookies from init required). Body is a sequence of `<script type='text/javascript'>parent.printNL(code, "params")</script>` chunks.
  - code 23: params = current page id.
  - code 24: `index||state||image||label||value` — one button. `state` 1 = selected/on.
  - code 25: `0||<rpm>` — current pump RPM.
  - code 28: date/time.
- Command: `POST https://prm.iaqualink.net/v2/webtouch/command`, JSON body `{"actionID":"<masterID actionID>","command":"<n>","dt":"<ms>"}`, header `Authorization: <idToken>`. Response body is empty; the new screen arrives on the stream.
- Command numbers: nav bar 1 Home, 2 Menu, 3 OneTouch, 4 Help, 5 Back, 6 Status. Any on-screen button = 17 + its code-24 index.
- Pages: 1 Home, 15 Menu, 54 Devices ("Other Devices On/Off"), 30 "VSP Adjust / Set Speed".
- VSP presets on page 30 (index → command): 0 Pool 2950 (17), 1 Spa 2700 (18), 2 FAST CLEAN 3450 (19), 3 HIGH SUN 3200 (20), 4 Pool Heat 3000 (21), 5 Spa Heat 2000 (22), 6 Cloudy 2800 (23), 7 At Night 900 (24). Custom RPM: `masterSTB` actionID with `command=128&text=<rpm>`.
- Waterfall: Devices page (54) index 6, command 23. Also on Home page index 4, command 21.
- Path to set a preset: Home (1) → Other Devices (24) → VSP1 Spd ADJ (19) → preset (17..24). Verified live on 2026-09-14: round trip about 1 second, stream confirms selection and RPM.

Labels and RPMs are read from the stream, never hard-coded, so keypad changes to presets are reflected automatically.

## 4. Architecture

One Home Assistant local add-on (Docker container managed by Supervisor), Python 3.12, with Ingress so it appears in the Home Assistant sidebar and Companion app using Home Assistant's own authentication and remote access.

Three components in one process:

### 4.1 Home Assistant client (`ha_client`)
- Connects to the Supervisor-provided Home Assistant websocket (`ws://supervisor/core/websocket`, token from `SUPERVISOR_TOKEN`). No long-lived token.
- Subscribes to `state_changed` for the entities in section 2 and keeps an in-memory copy.
- Calls `switch.turn_on/off` and `light.turn_on/off`.
- Reconnects with backoff.

### 4.2 iAqualink WebTouch client (`aqualink_client`)
- Logs in with email and password from add-on options; refreshes the token before expiry; re-logs-in on 401.
- Discovers the device `touchLink` from the account (single device expected; if several, the first `iaqua` device, overridable by an optional `serial` option).
- Opens the session, holds the stream open, parses `printNL` chunks into a screen model: `page_id`, `buttons[index] = {label, value, state}`, `rpm`.
- Tracks `page_id` and never sends a page button command unless the stream says it is on the expected page. Navigation helper: `go_home()` then step through pages, waiting for each page-id confirmation (timeout 5 s, one retry).
- Public operations: `set_preset(index)`, `set_custom_rpm(rpm)`, `set_waterfall(on)`, and a `state` property: `{connected, rpm, active_preset, presets[], waterfall_on}`.
- After any command it returns the session to Home so the panel's own state (Home page buttons include Waterfall) keeps refreshing. It re-visits the VSP page on a slow poll (every 5 minutes) to refresh preset labels and RPM, and immediately after a speed command.
- Reconnects the stream with backoff; on reconnect re-reads the current page before acting.

### 4.3 Web app and thermostat (`app`, `thermostat`)
- FastAPI + uvicorn. Serves one static page and a JSON API under the Ingress path.
- `GET /api/state` returns the combined state (HA entities, aqualink state, thermostat settings and status, connection health, last action).
- `GET /api/events` is a server-sent-events stream pushing the same state object whenever anything changes (page updates within about a second).
- `POST /api/switch/{name}` with `{on: bool}` for: `filter_pump`, `spa`, `spa_heat`, `jet_pump`, `pool_heat`, `waterfall`, `light_shallow`, `light_middle`, `light_deep`, `lights_all`.
- `POST /api/pump/preset/{index}` and `POST /api/pump/rpm` with `{rpm}`.
- `POST /api/thermostat` with any of `{enabled, target, buffer, off_early}`.
- `POST /api/spa/start` and `POST /api/spa/end`.
- Thermostat settings persist as JSON in `/data/settings.json`.

## 5. Spa thermostat and session logic

Settings (defaults): `enabled=false` (a *session* flag set by Start spa / cleared by End spa, not a user switch), `target=94`, `buffer=3`, `off_early=0`. Target range 80–104 °F. Buffer 1–10. Off-early 0–5. In addition, `off_early < buffer` must always hold: the off threshold (`target - off_early`) has to stay strictly above the on threshold (`target - buffer`), otherwise the band inverts and the heater cycles on and off at a constant temperature. A settings change that would violate it is rejected with "Off early must be less than Buffer", and if inverted thresholds ever reach the loop it holds with status "Invalid settings (off-early ≥ buffer)" rather than switching.

Loop, evaluated on every `sensor.spa_temp` change and every 60 s:

1. The thermostat is always on (revised 2026-09-15). If `switch.spa_pump` is off, or `sensor.spa_temp` is not a number: do nothing; status = reason ("Spa is off", "No spa temperature").
2. Let `T` = spa temp, `H` = `switch.spa_heater` state.
3. Protective half, always: if `H` is on and `T >= target - off_early`: turn Spa Heat off, immediately (never delayed by the minimum cycle time). Reason "Reached {T}°". This applies whether the heater was turned on by the page, by hand, or by the panel.
4. Convenience half, only during a session (`enabled`) and only while `switch.pool_pump` is on: if `H` is off and `T <= target - buffer`: turn Spa Heat on. Reason "Dropped to {T}°". Outside a session the status reads "Idle {T}°"; with the pump off, "Pump is off".
5. Otherwise hold.
6. Minimum 5 minutes between a thermostat-initiated switch and the next turn-ON. If turning on is due but too soon, status shows "Waiting (min. cycle time)". Turning off is never delayed.
7. Manual changes are respected: the loop only compares current state to thresholds, so a heater the user turned on stays on until `T` reaches the off threshold, and one they turned off stays off until `T` drops to the on threshold.
8. Every action is logged: time, action, temperature, reason. The last one is shown on the page.
9. A failed service call is logged and retried on the next evaluation (subject to rule 6).
10. Stale-reading guard: the loop records the spa temperature and the time it last changed. If the thermostat is enabled, the spa is on, the heater is on and `sensor.spa_temp` has not changed value for 15 minutes, the reading is treated as stuck: Spa Heat is turned off immediately (not subject to rule 6) and status shows "Spa temperature stale". While stale the loop holds — it will not switch the heater back on — and normal rules resume as soon as the reading changes.
11. If the Home Assistant websocket is disconnected the loop does not evaluate at all; status shows "Home Assistant disconnected".

Start spa (`/api/spa/start`): Spa on, then Spa Heat on, then `enabled=true`. Jet Pump untouched.

End spa (`/api/spa/end`): `enabled=false`, Spa Heat off, Filter Pump off. Spa mode is left on so valves do not move. The AquaLink RS keeps the pump running for five minutes after the heater fired ("PUMP WILL TURN OFF AFTER COOL DOWN CYCLE"), so no timer is needed. Page shows "Pump stopping after heater cooldown" while HA still reports the pump on after End spa was pressed (cleared when the pump reads off or after 6 minutes).

Session state shown on the page: "Off" (spa off), "Heating to {target}°" (spa on, heater on), "Ready {T}°" (spa on, heater off, thermostat holding), "Cooling down" (End spa pressed, pump still on).

## 6. Page

One screen that fits an iPhone in portrait without scrolling (revised 2026-09-15), also fine on desktop. Large tap targets. Controls show a pending state until the confirming state change arrives (timeout 10 s, then revert and show a brief error).

1. Readings: Pool temp, Air temp (from the WebTouch Home page), Spa temp (only when Spa mode is on). Pump line: "{preset} · {rpm} RPM" or "Off".
2. Spa card: Start spa / End spa buttons and session state; toggles Spa, Spa Heat, Jet Pump; Target ± stepper and status line (no on/off switch: the thermostat always runs); disclosure for Buffer and Off-early.
3. Pump card: Filter Pump toggle; one line with the current preset and RPM, and a "Change" disclosure that opens the 2-column preset grid (label + RPM, active highlighted) and the Custom RPM input.
4. Equipment card: Pool Heat, Waterfall. Lights card: an "All" toggle in the header and Shallow, Middle, Deep below.
5. Status footer: Home Assistant and iAqualink connection health; last thermostat action with time and reason. When iAqualink is disconnected, pump-speed and waterfall controls are disabled with a note; HA controls keep working.

Style: clean, high contrast, dark-mode aware, large type. Vanilla HTML/CSS/JS, no build step.

## 7. Configuration and deployment

- Add-on options: `iaqualink_email`, `iaqualink_password` (password type), optional `serial`, `log_level`.
- Everything else discovered at runtime (entity IDs are fixed constants in one module; device and presets read live).
- Install as a local add-on: copy the `pool_control` add-on folder to Home Assistant's `/addons/` share (Samba or SSH add-on), refresh the add-on store, install, set options, start, open from the sidebar.
- Data: `/data/settings.json` for thermostat settings, `/data/runtime.json` for the last thermostat switch time.

## 8. Error handling

- iAqualink login fails or cloud unreachable: retry with exponential backoff (max 5 min); page shows disconnected; HA controls unaffected.
- Stream drops: reconnect with backoff; re-read page before any command.
- Command on wrong page: navigation always verifies page id from the stream; on mismatch it goes Home and retries once, then reports failure.
- Home Assistant websocket drops: reconnect with backoff; thermostat does not act until fresh state arrives.
- Thermostat never acts on `unknown`/`unavailable` temperature.
- Thermostat never acts while the Home Assistant websocket is down: the cached state may be stale, so it holds with status "Home Assistant disconnected" until the connection is back.
- A spa temperature that stops changing for 15 minutes while the heater runs is treated as a failed sensor: the heater is turned off and the thermostat holds until the reading moves again (see section 5, rule 10).
- The time of the last thermostat switch is persisted to `/data/runtime.json`, so a restart cannot bypass the 5-minute minimum cycle time.
- Supervisor restart: the add-on restarts with HA (`startup: application`, `watchdog` on the health endpoint).

## 9. Testing

- Unit: thermostat rules with fake temperatures (buffer, off-early, min cycle time, manual override, unknown temp, spa off).
- Unit: WebTouch stream parser using the captured `printNL` messages; command-number derivation; page navigation state machine with a fake stream.
- Unit: API handlers with fake HA and aqualink clients.
- Integration (manual, opt-in via env vars): real login, session open, read presets, Cloudy→Pool round trip.
- Go-live checklist: readings match the iAqualink app; each toggle verified with the equipment in view; disable the old "Turn_Off_Spa_When_Temperature_Too_High" automation; run one spa session with the thermostat and note the overshoot to set Off-early.

## 10. Repository layout

```
pool_control/
  pool_control/            # the add-on (config.yaml, Dockerfile, run.sh, app/)
    app/
      main.py              # FastAPI app, API routes, SSE
      ha_client.py
      aqualink_client.py   # login, session, stream parser, navigation, commands
      thermostat.py
      settings.py          # persisted settings
      entities.py          # HA entity id constants
      static/index.html, app.js, style.css
  tests/
  docs/
    iaqualink-webtouch-protocol.md
    superpowers/specs/2026-09-14-pool-control-design.md
  README.md
```
