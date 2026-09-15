from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

from app.entities import SENSORS, SWITCHES
from app.notifier import Notifier
from app.settings import RuntimeStore, SettingsStore, ThermostatSettings
from app.thermostat import Decision, ThermostatInput, evaluate

LOGGER = logging.getLogger(__name__)

# a spa temperature that has not moved for this long while the heater runs means the
# reading is stuck: stop heating rather than trust it
STALE_TEMPERATURE_SECONDS = 900


class ThermostatRunner:
    def __init__(self, ha, store: SettingsStore, clock: Callable[[], float] = time.time,
                 runtime: RuntimeStore | None = None):
        self._ha = ha
        self._store = store
        self._runtime = runtime or RuntimeStore(store.path.parent / "runtime.json")
        self._clock = clock
        self.settings: ThermostatSettings = store.load()
        self.status = "Starting"
        self.last_action: dict | None = None
        self.last_switch_at: float | None = self._runtime.load_last_switch_at()
        self._temp_seen: tuple[float | None, float] | None = None
        self._temp_stale = False
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
            pump_on=self._ha.is_on(SWITCHES["filter_pump"]),
            spa_temp=self._ha.number(SENSORS["spa_temp"]),
            heater_on=self._ha.is_on(SWITCHES["spa_heat"]),
            target=self.settings.target,
            buffer=self.settings.buffer,
            off_early=self.settings.off_early,
            now=self._clock(),
            last_switch_at=self.last_switch_at,
        )

    def _temperature_is_stale(self, inp: ThermostatInput) -> bool:
        """True while `sensor.spa_temp` has been stuck at one value with the heater running."""
        last = self._temp_seen
        if last is None or last[0] != inp.spa_temp:
            self._temp_seen = (inp.spa_temp, inp.now)
            self._temp_stale = False
            return False
        if not self._temp_stale and (
            inp.spa_on and inp.heater_on and inp.spa_temp is not None
            and inp.now - last[1] >= STALE_TEMPERATURE_SECONDS
        ):
            LOGGER.warning("Thermostat: spa temperature stuck at %s° for %.0f s", inp.spa_temp, inp.now - last[1])
            self._temp_stale = True
        return self._temp_stale

    def _decide(self, inp: ThermostatInput) -> Decision:
        if self._temperature_is_stale(inp):
            if inp.heater_on:
                return Decision("off", "Spa temperature stale", "Spa temperature stopped changing")
            return Decision("hold", "Spa temperature stale")
        return evaluate(inp)

    async def evaluate_and_act(self) -> Decision:
        async with self._lock:
            if not self._ha.connected:
                # no fresh state to act on; wait for the websocket to come back
                self.status = "Home Assistant disconnected"
                return Decision("hold", self.status)
            inp = self._input()
            decision = self._decide(inp)
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
                try:
                    self._runtime.save_last_switch_at(self.last_switch_at)
                except OSError as exc:  # persistence is best effort; never block control
                    LOGGER.warning("Could not persist last switch time: %s", exc)
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
