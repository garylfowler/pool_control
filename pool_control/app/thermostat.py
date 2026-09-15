"""Pure spa thermostat rules. No I/O here; the runner does the switching."""
from __future__ import annotations

from dataclasses import dataclass

MIN_CYCLE_SECONDS = 300


@dataclass(frozen=True)
class ThermostatInput:
    enabled: bool  # a spa session is active (Start spa pressed): maintain the target, not just cap it
    spa_on: bool
    pump_on: bool  # filter pump running: the heater is never turned on without flow
    spa_temp: float | None
    heater_on: bool
    target: float
    buffer: float
    off_early: float
    now: float
    last_switch_at: float | None


@dataclass(frozen=True)
class Decision:
    action: str  # "on" | "off" | "hold"
    status: str
    reason: str = ""


def _fmt(temp: float) -> str:
    return f"{temp:g}°"


def evaluate(inp: ThermostatInput) -> Decision:
    """The protective half (turn OFF at the target) always runs while the spa is on.
    The convenience half (turn back ON below target - buffer) runs only during a session,
    and only with the pump running. Turning off is never delayed; turning on honours the
    minimum cycle time."""
    if not inp.spa_on:
        return Decision("hold", "Spa is off")
    if inp.spa_temp is None:
        return Decision("hold", "No spa temperature")

    temp = inp.spa_temp
    off_at = inp.target - inp.off_early
    on_at = inp.target - inp.buffer

    if inp.heater_on:
        if temp >= off_at:
            return Decision("off", "Heater off", f"Reached {_fmt(temp)}")
        return Decision("hold", f"Heating {_fmt(temp)}")

    if not inp.enabled:
        return Decision("hold", f"Idle {_fmt(temp)}")
    if off_at <= on_at:
        # inverted band: switching on here would be switched off again at once, forever
        return Decision("hold", "Invalid settings (off-early ≥ buffer)")
    if temp > on_at:
        return Decision("hold", f"Holding {_fmt(temp)}")
    if not inp.pump_on:
        return Decision("hold", "Pump is off")
    if inp.last_switch_at is not None and inp.now - inp.last_switch_at < MIN_CYCLE_SECONDS:
        return Decision("hold", "Waiting (min. cycle time)", f"Dropped to {_fmt(temp)}")
    return Decision("on", "Heater on", f"Dropped to {_fmt(temp)}")
