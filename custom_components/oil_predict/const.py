"""Constants for the Oil Monitor integration."""

from homeassistant.const import Platform

DOMAIN = "oil_predict"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER]

CONF_DEPTH_ENTITY = "depth_entity"
CONF_TIMESTAMP_ENTITY = "timestamp_entity"
CONF_OUTDOOR_ENTITY = "outdoor_entity"
CONF_INDOOR_ENTITY = "indoor_entity"
CONF_RUNTIME_ENTITY = "runtime_entity"
CONF_INDOOR_TARGET_ENTITY = "indoor_target_entity"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_TANK_CAPACITY = "tank_capacity"
CONF_STALE_AFTER_MINUTES = "stale_after_minutes"

DEFAULT_TANK_CAPACITY = 1000
DEFAULT_STALE_AFTER_MINUTES = 90

# Projection levels (litres): order more oil at the first, treat the second as
# effectively empty (the pump/sludge line).
REORDER_LITRES = 250
EMPTY_LITRES = 50
