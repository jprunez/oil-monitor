"""Constants for the Oil Monitor integration."""

from homeassistant.const import Platform

DOMAIN = "oil_predict"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER]

# The tank sensor (stored as "depth_entity" for compatibility with v1 entries).
CONF_DEPTH_ENTITY = "depth_entity"
CONF_LEVEL_TYPE = "level_type"
CONF_TANK_CAPACITY = "tank_capacity"
CONF_TANK_SHAPE = "tank_shape"
CONF_FULL_DISTANCE = "full_distance_cm"
CONF_EMPTY_DISTANCE = "empty_distance_cm"
CONF_CALIBRATION = "calibration"

CONF_TIMESTAMP_ENTITY = "timestamp_entity"  # optional
CONF_OUTDOOR_ENTITY = "outdoor_entity"
CONF_INDOOR_ENTITY = "indoor_entity"
CONF_RUNTIME_ENTITY = "runtime_entity"  # optional
CONF_TANK_TEMP_ENTITY = "tank_temp_entity"  # optional
CONF_INDOOR_TARGET_ENTITY = "indoor_target_entity"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_STALE_AFTER_MINUTES = "stale_after_minutes"
CONF_WEATHER_HISTORY = "weather_history"  # use long-term weather history
CONF_HISTORY_LOCATION = "history_location"  # optional town or postcode

DEFAULT_TANK_CAPACITY = 1000
DEFAULT_STALE_AFTER_MINUTES = 90
DEFAULT_FULL_DISTANCE = 10
DEFAULT_EMPTY_DISTANCE = 120

# Default projection levels as fractions of capacity: order more oil at the
# first, treat the second as effectively empty (the pump/sludge line).
REORDER_FRACTION = 0.25
EMPTY_FRACTION = 0.05
