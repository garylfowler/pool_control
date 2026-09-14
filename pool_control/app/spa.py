from __future__ import annotations

import time
from typing import Callable

from app.entities import SENSORS, SWITCHES

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
        await self._runner.update_settings(enabled=False)
        await self._ha.call_service("switch", "turn_off", SWITCHES["spa_heat"])
        await self._ha.call_service("switch", "turn_off", SWITCHES["filter_pump"])
        self.end_pressed_at = self._clock()

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
