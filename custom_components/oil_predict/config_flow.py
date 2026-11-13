"""Config and options flows for the Oil Monitor integration."""

from __future__ import annotations

from datetime import timedelta
from functools import partial
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder.statistics import statistics_during_period

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .checks import STATISTIC_ROLES, check_entity, entity_roles, read_temperature_c

from .const import (
    CONF_CALIBRATION,
    CONF_DEPTH_ENTITY,
    CONF_EMPTY_DISTANCE,
    CONF_FULL_DISTANCE,
    CONF_HISTORY_LOCATION,
    CONF_INDOOR_ENTITY,
    CONF_INDOOR_TARGET_ENTITY,
    CONF_LEVEL_TYPE,
    CONF_OUTDOOR_ENTITY,
    CONF_RUNTIME_ENTITY,
    CONF_STALE_AFTER_MINUTES,
    CONF_TANK_CAPACITY,
    CONF_TANK_SHAPE,
    CONF_TANK_TEMP_ENTITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_WEATHER_HISTORY,
    DEFAULT_EMPTY_DISTANCE,
    DEFAULT_FULL_DISTANCE,
    DEFAULT_STALE_AFTER_MINUTES,
    DEFAULT_TANK_CAPACITY,
    DOMAIN,
)
from .model import (
    DEPTH_TO_PERCENT,
    LEVEL_DISTANCE,
    LEVEL_LITRES,
    LEVEL_PERCENT,
    SHAPE_CUSTOM,
    SHAPE_HORIZONTAL_CYLINDER,
    SHAPE_VERTICAL,
    format_calibration,
    parse_calibration,
)

_LOGGER = logging.getLogger(__name__)

STATUS_ICONS = {"ok": "✅", "warning": "⚠️", "problem": "❌"}

OPTIONAL_ENTITIES = (
    CONF_RUNTIME_ENTITY,
    CONF_TIMESTAMP_ENTITY,
    CONF_TANK_TEMP_ENTITY,
    CONF_HISTORY_LOCATION,
)


def _distance_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=1000, step=0.5, unit_of_measurement="cm",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _sensor(domain: str | list[str] = "sensor") -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


TANK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEPTH_ENTITY): _sensor(),
        vol.Required(CONF_LEVEL_TYPE, default=LEVEL_DISTANCE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[LEVEL_DISTANCE, LEVEL_PERCENT, LEVEL_LITRES],
                translation_key="level_type",
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_TANK_CAPACITY, default=DEFAULT_TANK_CAPACITY): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=100000, step=1, unit_of_measurement="L",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }
)

SHAPE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TANK_SHAPE, default=SHAPE_VERTICAL): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[SHAPE_VERTICAL, SHAPE_HORIZONTAL_CYLINDER, SHAPE_CUSTOM],
                translation_key="tank_shape",
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_FULL_DISTANCE, default=DEFAULT_FULL_DISTANCE): _distance_selector(),
        vol.Required(CONF_EMPTY_DISTANCE, default=DEFAULT_EMPTY_DISTANCE): _distance_selector(),
        vol.Optional(
            CONF_CALIBRATION, default=format_calibration(DEPTH_TO_PERCENT)
        ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
    }
)

SENSORS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OUTDOOR_ENTITY): _sensor(),
        vol.Required(CONF_INDOOR_ENTITY): _sensor(),
        vol.Required(CONF_INDOOR_TARGET_ENTITY): _sensor(["climate", "sensor"]),
        vol.Required(CONF_WEATHER_ENTITY): _sensor("weather"),
        vol.Optional(CONF_RUNTIME_ENTITY): _sensor(),
        vol.Optional(CONF_TIMESTAMP_ENTITY): _sensor(),
        vol.Optional(CONF_TANK_TEMP_ENTITY): _sensor(),
        vol.Required(CONF_WEATHER_HISTORY, default=True): selector.BooleanSelector(),
        vol.Optional(CONF_HISTORY_LOCATION): selector.TextSelector(),
        vol.Required(
            CONF_STALE_AFTER_MINUTES, default=DEFAULT_STALE_AFTER_MINUTES
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=10080, step=1, unit_of_measurement="min",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }
)


def _validate_shape(user_input: dict[str, Any]) -> dict[str, str]:
    """Check the tank-shape step and return form errors, if any."""
    errors: dict[str, str] = {}
    if user_input[CONF_TANK_SHAPE] == SHAPE_CUSTOM:
        try:
            parse_calibration(user_input.get(CONF_CALIBRATION, ""))
        except ValueError:
            errors[CONF_CALIBRATION] = "invalid_calibration"
    elif float(user_input[CONF_EMPTY_DISTANCE]) <= float(user_input[CONF_FULL_DISTANCE]):
        errors[CONF_EMPTY_DISTANCE] = "empty_not_below_full"
    return errors


class _OilMonitorSteps:
    """Steps shared by the setup flow and the options (settings) flow."""

    _values: dict[str, Any]

    def _form(self, step_id: str, schema: vol.Schema, errors: dict[str, str] | None = None):
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, self._values),  # type: ignore[attr-defined]
            errors=errors or {},
        )

    async def _tank_step(self, user_input: dict[str, Any] | None, step_id: str):
        if user_input is not None:
            self._values.update(user_input)
            if user_input[CONF_LEVEL_TYPE] == LEVEL_DISTANCE:
                return await self.async_step_shape()  # type: ignore[attr-defined]
            return await self.async_step_sensors()  # type: ignore[attr-defined]
        return self._form(step_id, TANK_SCHEMA)

    async def async_step_shape(self, user_input: dict[str, Any] | None = None):
        """Describe the tank so distance readings can be turned into litres."""
        if user_input is not None:
            errors = _validate_shape(user_input)
            if not errors:
                self._values.update(user_input)
                return await self.async_step_sensors()  # type: ignore[attr-defined]
            self._values.update(user_input)
            return self._form("shape", SHAPE_SCHEMA, errors)
        return self._form("shape", SHAPE_SCHEMA)

    async def async_step_check(self, user_input: dict[str, Any] | None = None):
        """Show a verdict on each chosen sensor before saving."""
        if user_input is not None:
            return self._finish()  # type: ignore[attr-defined]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="check",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": await self._async_check_summary()},
        )

    async def _async_history_days(self, roles: list[tuple[str, str, str]]) -> dict[str, int]:
        """Count days of daily statistics per role over the last year."""
        hass = self.hass  # type: ignore[attr-defined]
        wanted = {entity_id: role for role, _, entity_id in roles if role in STATISTIC_ROLES}
        if not wanted:
            return {}
        now = dt_util.utcnow()
        try:
            result = await get_instance(hass).async_add_executor_job(
                partial(
                    statistics_during_period,
                    hass,
                    now - timedelta(days=370),
                    now,
                    set(wanted),
                    "day",
                    None,
                    {"mean", "max", "change"},
                )
            )
        except Exception as err:  # noqa: BLE001 - the check must never block setup
            _LOGGER.debug("Could not read statistics for the setup check: %s", err)
            return {}
        days: dict[str, int] = {}
        for entity_id, role in wanted.items():
            keys = ("max", "change") if role == "runtime" else ("mean",)
            days[role] = sum(
                1
                for row in result.get(entity_id, [])
                if any(row.get(key) is not None for key in keys)
            )
        return days

    async def _async_check_summary(self) -> str:
        """One line per chosen entity: an icon, its name and what was found."""
        hass = self.hass  # type: ignore[attr-defined]
        roles = entity_roles(self._values)
        history_days = await self._async_history_days(roles)
        fallback = read_temperature_c(hass, self._values[CONF_INDOOR_TARGET_ENTITY])
        lines = []
        for role, label, entity_id in roles:
            row = check_entity(
                hass,
                role,
                label,
                entity_id,
                level_type=self._values.get(CONF_LEVEL_TYPE, LEVEL_DISTANCE),
                history_days=history_days.get(role),
                indoor_fallback_c=fallback,
            )
            lines.append(
                f"{STATUS_ICONS[row['status']]} **{label}** ({entity_id}): {row['message']}"
            )
        return "\n\n".join(lines)

    def _sensors_input(self, user_input: dict[str, Any]) -> None:
        # Optional entities left empty must be removed, not kept from before.
        for key in OPTIONAL_ENTITIES:
            if not user_input.get(key):
                self._values.pop(key, None)
        self._values.update({k: v for k, v in user_input.items() if v not in (None, "")})


class OilPredictConfigFlow(_OilMonitorSteps, ConfigFlow, domain=DOMAIN):
    """Set up Oil Monitor from the Home Assistant UI."""

    VERSION = 2

    def __init__(self) -> None:
        """Start with empty values."""
        self._values = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the settings flow for an existing entry."""
        return OilPredictOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the tank sensor and what it reports."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._tank_step(user_input, "user")

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the temperature, weather and optional sensors."""
        if user_input is not None:
            self._sensors_input(user_input)
            return await self.async_step_check()
        return self._form("sensors", SENSORS_SCHEMA)

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title="Oil Monitor", data=self._values)


class OilPredictOptionsFlow(_OilMonitorSteps, OptionsFlow):
    """Change Oil Monitor settings after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start from the current settings."""
        if not hasattr(self, "_values"):
            self._values = {**self.config_entry.data, **self.config_entry.options}
        return await self._tank_step(user_input, "init")

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the temperature, weather and optional sensors."""
        if user_input is not None:
            self._sensors_input(user_input)
            return await self.async_step_check()
        return self._form("sensors", SENSORS_SCHEMA)

    def _finish(self) -> ConfigFlowResult:
        # Store settings in the entry's data (cleared optional sensors must
        # disappear); the update listener reloads the integration.
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self._values, options={}
        )
        return self.async_create_entry(data={})
