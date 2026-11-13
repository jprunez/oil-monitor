"""Checks on the entities Oil Monitor is set up with.

Shared by the running integration (the dashboard's Data check) and the
setup screens (the final "Check your sensors" step), so both give the same
verdicts in the same words.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_DEPTH_ENTITY,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_OUTDOOR_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_TANK_TEMP_ENTITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_WEATHER_ENTITY,
)
from .model import LEVEL_DISTANCE, LEVEL_LITRES, LEVEL_PERCENT, parse_sample_timestamp

# Roles whose daily long-term statistics are read.
STATISTIC_ROLES = ("level", "outdoor", "indoor", "runtime", "tank_temp")

LEVEL_UNITS = {
    LEVEL_DISTANCE: ("cm",),
    LEVEL_PERCENT: ("%",),
    LEVEL_LITRES: ("L", "l"),
}
LEVEL_NAMES = {
    LEVEL_DISTANCE: "a distance in cm",
    LEVEL_PERCENT: "percent full",
    LEVEL_LITRES: "litres",
}


def entity_roles(config: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Return (role, label, entity_id) for every entity in the settings."""
    items: list[tuple[str, str, str]] = [
        ("level", "Tank sensor", config[CONF_DEPTH_ENTITY]),
        ("outdoor", "Outdoor temperature", config[CONF_OUTDOOR_ENTITY]),
        ("indoor", "Indoor temperature", config[CONF_INDOOR_ENTITY]),
        ("target", "Thermostat", config[CONF_INDOOR_TARGET_ENTITY]),
        ("weather", "Weather forecast", config[CONF_WEATHER_ENTITY]),
    ]
    if config.get(CONF_RUNTIME_ENTITY):
        items.append(("runtime", "Heating runtime", config[CONF_RUNTIME_ENTITY]))
    if config.get(CONF_TANK_TEMP_ENTITY):
        items.append(("tank_temp", "Tank temperature", config[CONF_TANK_TEMP_ENTITY]))
    if config.get(CONF_TIMESTAMP_ENTITY):
        items.append(("timestamp", "Measurement time", config[CONF_TIMESTAMP_ENTITY]))
    return items


def read_temperature_c(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a temperature in Celsius from a sensor or a climate entity's target."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return temperature_c_from_state(hass, state)


def temperature_c_from_state(hass: HomeAssistant, state: State) -> float | None:
    """Temperature in Celsius from one state (live or from history)."""
    if state.entity_id.split(".", 1)[0] == "climate":
        raw = state.attributes.get("temperature")
        unit = hass.config.units.temperature_unit
    else:
        if state.state in ("unknown", "unavailable"):
            return None
        raw = state.state
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if unit == UnitOfTemperature.FAHRENHEIT:
        return TemperatureConverter.convert(
            value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
        )
    if unit == UnitOfTemperature.CELSIUS:
        return value
    return None


def check_entity(
    hass: HomeAssistant,
    role: str,
    label: str,
    entity_id: str,
    *,
    level_type: str,
    history_days: int | None,
    indoor_fallback_c: float | None = None,
    weather_error: str | None = None,
) -> dict[str, Any]:
    """Check one entity and explain anything that will cause problems.

    ``history_days`` is how many days of daily statistics were found for it
    (None when not looked up or not applicable).
    """
    problems: list[str] = []
    notes: list[str] = []
    state = hass.states.get(entity_id)
    days = history_days
    if state is None:
        problems.append(
            "Not found. It may have been renamed or removed; choose it again under Configure."
        )
    elif state.state in ("unknown", "unavailable"):
        problems.append(f"Currently {state.state}.")
    else:
        unit = state.attributes.get("unit_of_measurement")
        if role in STATISTIC_ROLES:
            try:
                float(state.state)
            except ValueError:
                problems.append(f'Its value "{state.state}" is not a number.')
        if role == "level" and unit and unit not in LEVEL_UNITS[level_type]:
            problems.append(
                f"Its unit is {unit}, but Oil Monitor is set up for "
                f"{LEVEL_NAMES[level_type]}. Check the sensor type under Configure."
            )
        if role in ("outdoor", "indoor", "tank_temp") and unit not in (
            UnitOfTemperature.CELSIUS,
            UnitOfTemperature.FAHRENHEIT,
        ):
            problems.append(f"Its unit is {unit or 'missing'}; it should be °C or °F.")
        if role == "target" and read_temperature_c(hass, entity_id) is None:
            problems.append("No target temperature could be read from it.")
        if role == "weather" and weather_error:
            problems.append(weather_error)
        if role == "timestamp" and parse_sample_timestamp(state.state) is None:
            problems.append(f'Its value "{state.state}" is not a recognisable time.')
        if role in STATISTIC_ROLES:
            no_class = state.attributes.get("state_class") is None
            fallback = role == "indoor" and indoor_fallback_c is not None
            if no_class or days == 0:
                reason = (
                    "It has no state class, so Home Assistant keeps no long-term "
                    "statistics (daily history) for it."
                    if no_class
                    else "No daily history has been recorded for it yet."
                )
                if fallback:
                    notes.append(
                        f"{reason} Past weeks are assumed to have been "
                        f"{indoor_fallback_c:.1f} °C indoors; choose a "
                        "sensor with history under Configure for better estimates."
                    )
                elif role == "tank_temp":
                    notes.append(
                        f"{reason} The outdoor temperature is used for the "
                        "tank reading correction instead."
                    )
                elif role == "outdoor":
                    notes.append(f"{reason} Charts and estimates need it.")
                else:
                    problems.append(f"{reason} Charts and estimates need it.")
    status = "problem" if problems else "warning" if notes else "ok"
    if status == "ok":
        message = f"OK · {days} days of history" if days else "OK"
    else:
        message = " ".join(problems + notes)
    return {
        "role": role,
        "label": label,
        "entity_id": entity_id,
        "status": status,
        "message": message,
        "history_days": days,
    }
