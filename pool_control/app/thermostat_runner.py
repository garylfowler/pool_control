from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

from app.entities import SENSORS, SWITCHES
from app.notifier import Notifier
from app.settings import SettingsStore, ThermostatSettings
from app.thermostat import Decision, ThermostatInput, evaluate

LOGGER = logging.getLogger(__name__)


class ThermostatRunner:
    def __init__(self, ha, store: SettingsStore, clock: Callable[[], float] = time.time):
        self._ha = ha
        self._store = store
        self._clock = clock
        self.settings: ThermostatSettings = store.load()
        self.status = "Thermostat off"
        self.last_action: dict | None = None
        self.last_switch_at: float | None = None
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
            spa_temp=self._ha.number(SENSORS["spa_temp"]),
            heater_on=self._ha.is_on(SWITCHES["spa_heat"]),
            target=self.settings.target,
            buffer=self.settings.buffer,
            off_early=self.settings.off_early,
            now=self._clock(),
            last_switch_at=self.last_switch_at,
        )

    async def evaluate_and_act(self) -> Decision:
        async with self._lock:
            inp = self._input()
            decision = evaluate(inp)
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
