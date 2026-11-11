"""Sensor platform for the Oil Monitor integration."""

from typing import Any

from homeassistant.components.sensor import SensorEntity
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
    """Set up oil sensors from a config entry."""
    coordinator: OilPredictCoordinator = entry.runtime_data
    async_add_entities(
        [
            OilLitresRemainingSensor(coordinator, entry),
            WeeklyOilUsageSensor(coordinator, entry, "total"),
            WeeklyOilUsageSensor(coordinator, entry, "lower"),
            WeeklyOilUsageSensor(coordinator, entry, "upper"),
        ]
    )


class OilLitresRemainingSensor(
    CoordinatorEntity[OilPredictCoordinator], SensorEntity
):
    """Report estimated litres remaining in the tank."""

    _attr_has_entity_name = True
    _attr_name = "Litres remaining"
    _attr_native_unit_of_measurement = "L"
    _attr_state_class = "measurement"

    def __init__(
        self, coordinator: OilPredictCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_litres_remaining"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Only report a volume when a valid depth reading exists."""
        return super().available and self.coordinator.data["litres_remaining"] is not None

    @property
    def native_value(self) -> float | None:
        """Return the interpolated volume in litres."""
        value: Any = self.coordinator.data["litres_remaining"]
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the depth and the sensor's own sample time for troubleshooting."""
        data = self.coordinator.data
        return {
            "depth_cm": data["depth_cm"],
            "sample_time": data["sample_time"],
            "reading_age_minutes": data["age_minutes"],
        }


class WeeklyOilUsageSensor(CoordinatorEntity[OilPredictCoordinator], SensorEntity):
    """Report the weather-based oil use estimate for the next seven days."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "L"

    def __init__(
        self,
        coordinator: OilPredictCoordinator,
        entry: ConfigEntry,
        estimate: str,
    ) -> None:
        """Initialize a weekly estimate or uncertainty-bound sensor."""
        super().__init__(coordinator)
        self._estimate = estimate
        names = {
            "total": "Estimated oil use next 7 days",
            "lower": "Oil use forecast lower estimate",
            "upper": "Oil use forecast upper estimate",
        }
        self._attr_name = names[estimate]
        self._attr_unique_id = f"{entry.entry_id}_weekly_oil_use_{estimate}"
        self._attr_device_info = coordinator.device_info

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the indoor-temperature adjustment the estimate was made with."""
        return {"temperature_offset_c": self.coordinator.data["temperature_offset_c"]}

    @property
    def available(self) -> bool:
        """Only expose an estimate when training and forecast data are valid."""
        return (
            super().available
            and self.coordinator.data["weekly_forecast"] is not None
        )

    @property
    def native_value(self) -> float | None:
        """Return the weekly total or its lower/upper uncertainty bound."""
        forecast = self.coordinator.data["weekly_forecast"]
        if forecast is None:
            return None
        if self._estimate == "lower":
            return forecast.lower_litres
        if self._estimate == "upper":
            return forecast.upper_litres
        return forecast.total_litres
