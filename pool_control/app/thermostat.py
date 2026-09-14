"""Pure spa thermostat rules. No I/O here; the runner does the switching."""
from __future__ import annotations

from dataclasses import dataclass

MIN_CYCLE_SECONDS = 300


@dataclass(frozen=True)
class ThermostatInput:
    enabled: bool
    spa_on: bool
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
    if not inp.enabled:
        return Decision("hold", "Thermostat off")
    if not inp.spa_on:
        return Decision("hold", "Spa is off")
    if inp.spa_temp is None:
        return Decision("hold", "No spa temperature")

    temp = inp.spa_temp
    off_at = inp.target - inp.off_early
    on_at = inp.target - inp.buffer
    if off_at <= on_at:
        # inverted band: acting here would switch the heater on and off forever
        return Decision("hold", "Invalid settings (off-early ≥ buffer)")

    wanted = None
    if inp.heater_on and temp >= off_at:
        wanted = ("off", f"Reached {_fmt(temp)}")
    elif not inp.heater_on and temp <= on_at:
        wanted = ("on", f"Dropped to {_fmt(temp)}")

    if wanted is None:
        status = f"Heating {_fmt(temp)}" if inp.heater_on else f"Holding {_fmt(temp)}"
        return Decision("hold", status)

    if inp.last_switch_at is not None and inp.now - inp.last_switch_at < MIN_CYCLE_SECONDS:
        return Decision("hold", "Waiting (min. cycle time)", wanted[1])

    return Decision(wanted[0], f"Heater {wanted[0]}", wanted[1])
