"""Diagnostics download for Oil Monitor.

In Home Assistant: Settings → Devices & services → Oil Monitor → ⋮ →
Download diagnostics. The file describes the setup, each source sensor, the
history found and what the model learned, so problems can be diagnosed
without access to the user's system. It contains entity IDs and readings,
but no location, credentials or tokens.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    CONF_DEPTH_ENTITY,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_OUTDOOR_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_TANK_TEMP_ENTITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_WEATHER_ENTITY,
    DOMAIN,
)
from .coordinator import OilPredictCoordinator
from .model import MIN_TRAINING_WEEKS

SOURCE_KEYS = (
    CONF_DEPTH_ENTITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_OUTDOOR_ENTITY,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_TANK_TEMP_ENTITY,
    CONF_WEATHER_ENTITY,
)
ATTRIBUTES = (
    "unit_of_measurement",
    "state_class",
    "device_class",
    "temperature",
    "current_temperature",
    "hvac_action",
)


def _source(hass: HomeAssistant, entity_id: str | None) -> dict[str, Any] | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return {"entity_id": entity_id, "found": False}
    return {
        "entity_id": entity_id,
        "found": True,
        "state": state.state,
        "attributes": {k: state.attributes[k] for k in ATTRIBUTES if k in state.attributes},
        "last_updated": state.last_updated.isoformat(),
        "last_reported": (
            state.last_reported.isoformat() if getattr(state, "last_reported", None) else None
        ),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: OilPredictCoordinator = entry.runtime_data
    data = dict(coordinator.data or {})
    integration = await async_get_integration(hass, DOMAIN)
    forecast = data.get("weekly_forecast")

    live_keys = (
        "level_reading",
        "level_unit",
        "litres_remaining",
        "sample_time",
        "age_minutes",
        "is_stale",
        "temperature_offset_c",
        "indoor_target_source",
        "reorder",
        "empty",
    )
    return {
        "versions": {
            "oil_monitor": str(integration.version),
            "home_assistant": HA_VERSION,
        },
        "settings": {**entry.data},
        "settings_version": entry.version,
        "tank": asdict(coordinator.tank),
        "levels": {
            "reorder_litres": coordinator.reorder_level_litres,
            "empty_litres": coordinator.empty_level_litres,
        },
        "setup_check": data.get("setup_check"),
        "temp_correction": data.get("temp_correction"),
        "scenarios": data.get("scenario_info"),
        "backtest": data.get("backtest"),
        "sources": {
            key: _source(hass, entry.data.get(key)) for key in SOURCE_KEYS
        },
        "history": {
            "days_per_sensor": data.get("history_days"),
            "clean_weeks": data.get("training_weeks"),
            "weeks_needed": MIN_TRAINING_WEEKS,
            "indoor_history_fallback_c": data.get("indoor_history_fallback_c"),
            "training_samples": [asdict(s) for s in coordinator.training_samples],
        },
        "model": asdict(coordinator.model) if coordinator.model else None,
        "forecast": asdict(forecast) if forecast else None,
        "forecast_warning": data.get("forecast_warning"),
        "live": {key: data.get(key) for key in live_keys},
        "last_update_success": coordinator.last_update_success,
    }
