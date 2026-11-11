"""Binary sensor platform for the Oil Monitor integration."""

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OilPredictCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up oil binary sensors from a config entry."""
    coordinator: OilPredictCoordinator = entry.runtime_data
    async_add_entities([OilTankSensorStaleBinarySensor(coordinator, entry)])


class OilTankSensorStaleBinarySensor(
    CoordinatorEntity[OilPredictCoordinator], BinarySensorEntity
):
    """Indicate whether the tank sensor reading is missing or stale."""

    _attr_has_entity_name = True
    _attr_name = "Tank sensor stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _unrecorded_attributes = frozenset({"setup_check", "history_days", "temp_correction"})

    def __init__(
        self, coordinator: OilPredictCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tank_sensor_stale"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        """Return true when the source reading is not safe to use."""
        return bool(self.coordinator.data["is_stale"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the last source reading time and age."""
        data = self.coordinator.data
        return {
            "sample_time": data["sample_time"],
            "reading_age_minutes": data["age_minutes"],
            "sensor_reading": data["level_reading"],
            "training_weeks": data["training_weeks"],
            "training_weeks_needed": data["training_weeks_needed"],
            "history_days": data["history_days"],
            "setup_check": data["setup_check"],
            "temp_correction": data["temp_correction"],
            "scenario_info": data["scenario_info"],
            "forecast_warning": data["forecast_warning"],
        }
