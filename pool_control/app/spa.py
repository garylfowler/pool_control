from __future__ import annotations

import logging
import time
from typing import Callable

from app.entities import SENSORS, SWITCHES
from app.ha_client import HAError

LOGGER = logging.getLogger(__name__)

COOLDOWN_WINDOW_SECONDS = 360


class SpaSession:
    def __init__(self, ha, runner, clock: Callable[[], float] = time.time):
        self._ha = ha
        self._runner = runner
        self._clock = clock
        self.end_pressed_at: float | None = None

    async def start(self) -> None:
        await self._ha.call_service("switch", "turn_on", SWITCHES["spa"])
        await self._ha.call_service("switch", "turn_on", SWITCHES["spa_heat"])
        await self._runner.update_settings(enabled=True)
        self.end_pressed_at = None

    async def end(self) -> None:
        """Runs all three shutdown steps even if one fails, then reports what went wrong."""
        failures: list[str] = []

        try:
            await self._runner.update_settings(enabled=False)
        except Exception as exc:
            LOGGER.error("End spa: disabling the thermostat failed: %s", exc)
            failures.append("thermostat off failed")

        heater_off = True
        try:
            await self._ha.call_service("switch", "turn_off", SWITCHES["spa_heat"])
        except Exception as exc:
            heater_off = False
            LOGGER.error("End spa: turning the heater off failed: %s", exc)
            failures.append("heater off failed")

        try:
            await self._ha.call_service("switch", "turn_off", SWITCHES["filter_pump"])
        except Exception as exc:
            LOGGER.error("End spa: turning the filter pump off failed: %s", exc)
            failures.append("pump off failed")

        if heater_off:
            self.end_pressed_at = self._clock()
        else:
            # the heater may still be running: put the thermostat back in charge of it
            try:
                await self._runner.update_settings(enabled=True)
            except Exception as exc:
                LOGGER.error("End spa: re-enabling the thermostat failed: %s", exc)
                failures.append("thermostat could not resume")

        if failures:
            raise HAError("End spa: " + ", ".join(failures))

    @property
    def cooling_down(self) -> bool:
        if self.end_pressed_at is None:
            return False
        if self._clock() - self.end_pressed_at > COOLDOWN_WINDOW_SECONDS:
            return False
        return self._ha.is_on(SWITCHES["filter_pump"])

    def label(self) -> str:
        if self.cooling_down:
            return "Cooling down"
        if not self._ha.is_on(SWITCHES["spa"]):
            return "Off"
        if self._ha.is_on(SWITCHES["spa_heat"]):
            return f"Heating to {self._runner.settings.target:g}°"
        temp = self._ha.number(SENSORS["spa_temp"])
        if self._runner.settings.enabled and temp is not None:
            return f"Ready {temp:g}°"
        return "On"
