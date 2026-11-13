"""Set up the Oil Monitor integration."""

import hashlib
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import (
    CONF_CALIBRATION,
    CONF_DEPTH_ENTITY,
    CONF_EMPTY_DISTANCE,
    CONF_FULL_DISTANCE,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_LEVEL_TYPE,
    CONF_OUTDOOR_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_TANK_SHAPE,
    CONF_TIMESTAMP_ENTITY,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import OilPredictCoordinator
from .model import DEPTH_TO_PERCENT, LEVEL_DISTANCE, SHAPE_CUSTOM, format_calibration

_LOGGER = logging.getLogger(__name__)

PANEL_PATH = "oil-monitor"
PANEL_COMPONENT = "oil-monitor-panel"
PANEL_MODULE_URL = "/oil_predict/oil-monitor-panel.js"
PANEL_FILE = Path(__file__).parent / "www" / "oil-monitor-panel.js"


def _panel_build_id() -> str:
    """Return a short hash of the panel file, used to bust browser caches."""
    return hashlib.sha256(PANEL_FILE.read_bytes()).hexdigest()[:8]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the bundled Oil Monitor dashboard module with Home Assistant."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_MODULE_URL, str(PANEL_FILE), cache_headers=False)]
    )
    websocket_api.async_register_command(hass, websocket_data_check)
    return True


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/data_check"})
@callback
def websocket_data_check(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return the data behind the dashboard's Data check page."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if isinstance(coordinator, OilPredictCoordinator):
            connection.send_result(msg["id"], coordinator.data_check())
            return
    connection.send_error(
        msg["id"], "not_loaded", "Oil Monitor is not set up or has not finished loading."
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade entries created by older versions."""
    if entry.version == 1:
        # Version 1 only supported a distance sensor on the prototype tank.
        data = {
            **entry.data,
            CONF_LEVEL_TYPE: LEVEL_DISTANCE,
            CONF_TANK_SHAPE: SHAPE_CUSTOM,
            CONF_FULL_DISTANCE: DEPTH_TO_PERCENT[0][0],
            CONF_EMPTY_DISTANCE: DEPTH_TO_PERCENT[-1][0],
            CONF_CALIBRATION: format_calibration(DEPTH_TO_PERCENT),
        }
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Migrated Oil Monitor settings to version 2")
    return True


async def _async_reload_on_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed settings by reloading the integration."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Oil Monitor from a config entry."""
    coordinator = OilPredictCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for unsubscribe in coordinator.async_start_listeners():
        entry.async_on_unload(unsubscribe)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_change))

    # Look up the actual entity IDs, which depend on the device name and on
    # any renames the user has made, rather than guessing them.
    registry = er.async_get(hass)

    def entity_id(platform: Platform, key: str) -> str | None:
        return registry.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{key}"
        )

    # A new URL for every panel version, so browsers and the companion app
    # cannot keep running an old cached copy after an update.
    build_id = await hass.async_add_executor_job(_panel_build_id)
    integration = await async_get_integration(hass, DOMAIN)
    tank = coordinator.tank
    config = entry.data

    if frontend.async_panel_exists(hass, PANEL_PATH):
        frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
    await async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=f"{PANEL_COMPONENT}-{build_id}",
        sidebar_title="Oil Monitor",
        sidebar_icon="mdi:oil",
        module_url=f"{PANEL_MODULE_URL}?v={build_id}",
        config={
            "build": build_id,
            "version": str(integration.version or ""),
            "depth_entity": config[CONF_DEPTH_ENTITY],
            "timestamp_entity": config.get(CONF_TIMESTAMP_ENTITY),
            "outdoor_entity": config[CONF_OUTDOOR_ENTITY],
            "indoor_entity": config[CONF_INDOOR_ENTITY],
            "runtime_entity": config.get(CONF_RUNTIME_ENTITY),
            "target_entity": config[CONF_INDOOR_TARGET_ENTITY],
            "weather_entity": config[CONF_WEATHER_ENTITY],
            "tank_capacity": tank.capacity_litres,
            "tank": {
                "level_type": tank.level_type,
                "shape": tank.shape,
                "full_distance_cm": tank.full_distance_cm,
                "empty_distance_cm": tank.empty_distance_cm,
                "calibration": [list(pair) for pair in tank.calibration],
            },
            "litres_entity": entity_id(Platform.SENSOR, "litres_remaining"),
            "weekly_entity": entity_id(Platform.SENSOR, "weekly_oil_use_total"),
            "lower_entity": entity_id(Platform.SENSOR, "weekly_oil_use_lower"),
            "upper_entity": entity_id(Platform.SENSOR, "weekly_oil_use_upper"),
            "stale_entity": entity_id(Platform.BINARY_SENSOR, "tank_sensor_stale"),
            "offset_entity": entity_id(Platform.NUMBER, "temperature_adjustment"),
            "reorder_entity": entity_id(Platform.SENSOR, "days_until_reorder"),
            "empty_entity": entity_id(Platform.SENSOR, "days_until_empty"),
            "reorder_level_entity": entity_id(Platform.NUMBER, "reorder_level"),
            "empty_level_entity": entity_id(Platform.NUMBER, "empty_level"),
        },
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Oil Monitor platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)
    return unloaded
