"""Sensor platform for the Oil Monitor integration."""

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FLOW_ENTITY
from .coordinator import OilPredictCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up oil sensors from a config entry."""
    coordinator: OilPredictCoordinator = entry.runtime_data
    extra: list[SensorEntity] = []
    if entry.data.get(CONF_FLOW_ENTITY):
        extra.append(HeatingEfficiencySensor(coordinator, entry))
    async_add_entities(
        [
            *extra,
            OilLitresRemainingSensor(coordinator, entry),
            WeeklyOilUsageSensor(coordinator, entry, "total"),
            WeeklyOilUsageSensor(coordinator, entry, "lower"),
            WeeklyOilUsageSensor(coordinator, entry, "upper"),
            OilRunOutSensor(coordinator, entry, "reorder"),
            OilRunOutSensor(coordinator, entry, "empty"),
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
            "sensor_reading": data["level_reading"],
            "sensor_unit": data["level_unit"],
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
        data = self.coordinator.data
        return {
            "temperature_offset_c": data["temperature_offset_c"],
            "indoor_target_source": data["indoor_target_source"],
            "early_estimate": data["early_estimate"],
            "typical_weekly_error_litres": (
                round(data["backtest"]["typical_error_litres"], 1) if data["backtest"] else None
            ),
            "training_weeks": data["training_weeks"],
        }

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


class OilRunOutSensor(CoordinatorEntity[OilPredictCoordinator], SensorEntity):
    """Report how many days until the tank reaches the reorder or empty level."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "d"
    _attr_suggested_display_precision = 0
    # The chart series changes every update; keep it out of the recorder.
    _unrecorded_attributes = frozenset({"projection"})

    def __init__(
        self,
        coordinator: OilPredictCoordinator,
        entry: ConfigEntry,
        level: str,
    ) -> None:
        """Initialize a run-out sensor for the ``reorder`` or ``empty`` level."""
        super().__init__(coordinator)
        self._level = level
        self._attr_name = {
            "reorder": "Days until reorder level",
            "empty": "Days until empty",
        }[level]
        self._attr_icon = "mdi:calendar-clock" if level == "reorder" else "mdi:oil-level"
        self._attr_unique_id = f"{entry.entry_id}_days_until_{level}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Only report when a forecast and a tank volume are available."""
        projection = self.coordinator.data[self._level]
        return super().available and (
            projection["days"] is not None or projection["beyond_horizon"]
        )

    @property
    def native_value(self) -> float | None:
        """Return the central estimate in days (unknown when over a year away)."""
        return self.coordinator.data[self._level]["days"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the expected date, a cautious earliest date, and the level."""
        projection = self.coordinator.data[self._level]
        attributes: dict[str, Any] = {
            "date": projection["date"],
            "earliest_days": projection["earliest_days"],
            "earliest_date": projection["earliest_date"],
            "latest_days": projection["latest_days"],
            "latest_date": projection["latest_date"],
            "level_litres": projection["level_litres"],
            "more_than_a_year": projection["beyond_horizon"],
        }
        if self._level == "empty":
            attributes["projection"] = self.coordinator.data["level_projection"]
        return attributes


class HeatingEfficiencySensor(CoordinatorEntity[OilPredictCoordinator], SensorEntity):
    """Relative boiler efficiency: heat delivered per litre, 100 % = usual.

    Falls when the boiler needs a service (soot, a worn nozzle) and rises
    after one. Worked out from the flow (and return) temperatures.
    """

    _attr_has_entity_name = True
    _attr_name = "Heating efficiency"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = "measurement"
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:fire-circle"

    def __init__(self, coordinator: OilPredictCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_heating_efficiency"
        self._attr_device_info = coordinator.device_info

    @property
    def _trend(self) -> dict[str, Any] | None:
        return self.coordinator.boiler.get("efficiency")

    @property
    def available(self) -> bool:
        """Only report once there are enough heating weeks to compare."""
        return super().available and self._trend is not None

    @property
    def native_value(self) -> float | None:
        """Average of the last four weeks, relative to this house's usual."""
        trend = self._trend
        return trend["latest_percent"] if trend else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Trend, service flag and burner figures."""
        boiler = self.coordinator.boiler
        trend = self._trend or {}
        burn = boiler.get("burn") or {}
        summary = boiler.get("summary") or {}
        return {
            "change_per_year_percent": trend.get("change_per_year_percent"),
            "weeks": trend.get("weeks"),
            "service_suggested": boiler.get("efficiency_alert", False),
            "litres_per_heating_hour": burn.get("litres_per_hour"),
            "heating_hours_per_day": summary.get("hours_per_day"),
            "flow_temperature_c": summary.get("flow_c"),
            "return_temperature_c": summary.get("return_c"),
            "condensing_share": summary.get("condensing_share"),
        }
