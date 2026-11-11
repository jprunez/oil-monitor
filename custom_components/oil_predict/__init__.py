"""Set up the Oil Monitor integration."""

from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_DEPTH_ENTITY,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_OUTDOOR_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_TANK_CAPACITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import OilPredictCoordinator

PANEL_PATH = "oil-monitor"
PANEL_COMPONENT = "oil-monitor-panel"
PANEL_MODULE_URL = "/oil_predict/oil-monitor-panel.js"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the bundled Oil Monitor dashboard module with Home Assistant."""
    panel_file = Path(__file__).parent / "www" / "oil-monitor-panel.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_MODULE_URL, str(panel_file), cache_headers=False)]
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Oil Monitor from a config entry."""
    coordinator = OilPredictCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for unsubscribe in coordinator.async_start_listeners():
        entry.async_on_unload(unsubscribe)

    # Look up the actual entity IDs, which depend on the device name and on
    # any renames the user has made, rather than guessing them.
    registry = er.async_get(hass)

    def entity_id(platform: Platform, key: str) -> str | None:
        return registry.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{key}"
        )

    if frontend.async_panel_exists(hass, PANEL_PATH):
        frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
    await async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title="Oil Monitor",
        sidebar_icon="mdi:oil",
        module_url=PANEL_MODULE_URL,
        config={
            "depth_entity": entry.data[CONF_DEPTH_ENTITY],
            "timestamp_entity": entry.data[CONF_TIMESTAMP_ENTITY],
            "outdoor_entity": entry.data[CONF_OUTDOOR_ENTITY],
            "indoor_entity": entry.data[CONF_INDOOR_ENTITY],
            "runtime_entity": entry.data[CONF_RUNTIME_ENTITY],
            "target_entity": entry.data[CONF_INDOOR_TARGET_ENTITY],
            "weather_entity": entry.data[CONF_WEATHER_ENTITY],
            "tank_capacity": entry.data[CONF_TANK_CAPACITY],
            "litres_entity": entity_id(Platform.SENSOR, "litres_remaining"),
            "weekly_entity": entity_id(Platform.SENSOR, "weekly_oil_use_total"),
            "lower_entity": entity_id(Platform.SENSOR, "weekly_oil_use_lower"),
            "upper_entity": entity_id(Platform.SENSOR, "weekly_oil_use_upper"),
            "stale_entity": entity_id(Platform.BINARY_SENSOR, "tank_sensor_stale"),
            "offset_entity": entity_id(Platform.NUMBER, "temperature_adjustment"),
        },
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Oil Monitor platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
    return unloaded
