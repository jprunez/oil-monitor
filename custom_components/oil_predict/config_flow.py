"""Config flow for the Oil Monitor integration."""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DEPTH_ENTITY,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_OUTDOOR_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_STALE_AFTER_MINUTES,
    CONF_TANK_CAPACITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_WEATHER_ENTITY,
    DEFAULT_STALE_AFTER_MINUTES,
    DEFAULT_TANK_CAPACITY,
    DOMAIN,
)


class OilPredictConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Oil Monitor from the Home Assistant UI."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the tank sensors and basic tank settings."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Oil Monitor", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_DEPTH_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_TIMESTAMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_OUTDOOR_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_INDOOR_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_RUNTIME_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    CONF_INDOOR_TARGET_ENTITY
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["climate", "sensor"])
                ),
                vol.Required(CONF_WEATHER_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="weather")
                ),
                vol.Required(
                    CONF_TANK_CAPACITY, default=DEFAULT_TANK_CAPACITY
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=10000,
                        step=1,
                        unit_of_measurement="L",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_STALE_AFTER_MINUTES,
                    default=DEFAULT_STALE_AFTER_MINUTES,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=1440,
                        step=1,
                        unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
