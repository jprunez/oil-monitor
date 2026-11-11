"""What-if indoor temperature adjustment for the Oil Monitor forecasts."""

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import OilPredictCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the temperature adjustment number."""
    coordinator: OilPredictCoordinator = entry.runtime_data
    async_add_entities([TemperatureAdjustmentNumber(coordinator, entry)])


class TemperatureAdjustmentNumber(RestoreNumber):
    """Shift the indoor temperature used for forecasts ("what if 1 °C warmer?")."""

    _attr_has_entity_name = True
    _attr_name = "Forecast temperature adjustment"
    _attr_icon = "mdi:thermometer-plus"
    _attr_native_min_value = -3.0
    _attr_native_max_value = 3.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: OilPredictCoordinator, entry: ConfigEntry) -> None:
        """Initialize the number."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_temperature_adjustment"
        self._attr_device_info = coordinator.device_info
        self._attr_native_value = 0.0

    async def async_added_to_hass(self) -> None:
        """Restore the last adjustment after a restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value:
            self._attr_native_value = float(last.native_value)
            self._coordinator.temperature_offset_c = self._attr_native_value
            await self._coordinator.async_request_refresh()

    async def async_set_native_value(self, value: float) -> None:
        """Apply a new adjustment and recalculate the forecasts."""
        self._attr_native_value = value
        self._coordinator.temperature_offset_c = value
        self.async_write_ha_state()
        await self._coordinator.async_request_refresh()
