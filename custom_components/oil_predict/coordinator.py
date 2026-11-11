"""Read Home Assistant tank entities and prepare integration data."""

from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

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
    DOMAIN,
    EMPTY_LITRES,
    REORDER_LITRES,
)
from .model import (
    DailyHistoryPoint,
    DailyTrainingSample,
    WeeklyForecast,
    build_training_samples,
    days_until_level,
    depth_to_litres,
    fit_usage_model,
    forecast_week,
    parse_sample_timestamp,
    predict_day,
    seasonal_outdoor_c,
)

_LOGGER = logging.getLogger(__name__)

# How far ahead the run-out projection looks.
PROJECTION_DAYS = 365

# How often the stale flag is re-evaluated when no new reading arrives.
STALE_CHECK_INTERVAL = timedelta(minutes=5)


class OilPredictCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate tank data and expose validated values to entities.

    The weather-based forecast is recalculated on the coordinator's interval
    (or on request). Live tank values are recalculated whenever the depth or
    timestamp sensor changes, and the stale flag is re-checked periodically.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(hours=3),
        )
        self.entry = entry
        self.temperature_offset_c = 0.0
        self._history_loaded_at: datetime | None = None
        self._training_samples: list[DailyTrainingSample] = []
        self._weekly_forecast: WeeklyForecast | None = None
        self._forecast_warning: str | None = None
        self._forecast_needs_retry = False
        self._target_source: str | None = None
        self.reorder_level_litres = float(REORDER_LITRES)
        self.empty_level_litres = float(EMPTY_LITRES)
        self._outdoor_history: dict[date, float] = {}
        self._daily_usage_projection: list[float] | None = None
        self._upper_scale = 1.0

    @property
    def device_info(self) -> DeviceInfo:
        """Group all Oil Monitor entities under one service device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="Oil Monitor",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def async_start_listeners(self) -> list[Callable[[], None]]:
        """Track source sensors so live values do not wait for the next poll."""
        live_entities = [
            self.entry.data[CONF_DEPTH_ENTITY],
            self.entry.data[CONF_TIMESTAMP_ENTITY],
        ]
        return [
            async_track_state_change_event(
                self.hass, live_entities, self._async_handle_live_change
            ),
            async_track_state_change_event(
                self.hass,
                [self.entry.data[CONF_INDOOR_TARGET_ENTITY]],
                self._async_handle_target_change,
            ),
            async_track_time_interval(
                self.hass, self._async_handle_stale_check, STALE_CHECK_INTERVAL
            ),
        ]

    @callback
    def async_push_live_data(self) -> None:
        """Recalculate live values (e.g. after a level setting changes)."""
        self._async_push_live_data()

    @callback
    def _async_push_live_data(self) -> None:
        """Rebuild live values and notify entities without resetting the poll timer."""
        if self.data is None:
            return
        self.data = self._build_data()
        self.async_update_listeners()

    @callback
    def _async_handle_live_change(self, _event: Event[EventStateChangedData]) -> None:
        self._async_push_live_data()

    @callback
    def _async_handle_stale_check(self, _now: datetime) -> None:
        self._async_push_live_data()
        if self._forecast_needs_retry:
            # A missing input (weather or temperature) is usually transient;
            # retry soon instead of waiting for the 3-hour refresh.
            self.hass.async_create_task(self.async_request_refresh())

    async def _async_handle_target_change(
        self, _event: Event[EventStateChangedData]
    ) -> None:
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict[str, Any]:
        """Recalculate the forecast, then combine it with live tank values."""
        await self._async_update_forecast()
        return self._build_data()

    async def _async_update_forecast(self) -> None:
        """Refresh training history when due and recalculate the weekly forecast."""
        self._forecast_needs_retry = False
        self._daily_usage_projection = None
        try:
            if (
                self._history_loaded_at is None
                or dt_util.utcnow() - self._history_loaded_at > timedelta(hours=24)
            ):
                self._training_samples = await self._async_load_training_history()
                self._history_loaded_at = dt_util.utcnow()

            if len(self._training_samples) < 14:
                self._weekly_forecast = None
                self._forecast_warning = (
                    "Need at least 14 complete, clean weeks of history"
                )
                return

            temperature_target, self._target_source = self._forecast_indoor_target()
            if temperature_target is None:
                # Source sensors are often still loading just after a restart.
                self._forecast_needs_retry = True
                self._weekly_forecast = None
                self._forecast_warning = (
                    "Could not read the thermostat target or indoor temperature"
                )
                return

            model = fit_usage_model(self._training_samples)
            forecast_temperatures = await self._async_get_daily_forecast_c()
            indoor_target = temperature_target + self.temperature_offset_c
            self._weekly_forecast = forecast_week(
                model, forecast_temperatures, indoor_target
            )
            self._daily_usage_projection = self._project_daily_usage(
                model, forecast_temperatures, indoor_target
            )
            total = self._weekly_forecast.total_litres
            self._upper_scale = (
                self._weekly_forecast.upper_litres / total if total > 0 else 1.0
            )
            self._forecast_warning = None
        except (HomeAssistantError, ValueError, TypeError, KeyError) as err:
            _LOGGER.warning("Could not calculate the oil forecast: %s", err)
            self._forecast_needs_retry = True
            self._weekly_forecast = None
            self._forecast_warning = str(err)

    def _project_daily_usage(
        self,
        model: Any,
        forecast_temperatures: list[float],
        indoor_target_c: float,
    ) -> list[float]:
        """Project daily litres for the coming year.

        The first week uses the weather forecast; later days use the outdoor
        temperature recorded around the same date last year. Where no history
        exists, the forecast week's average temperature is assumed.
        """
        fallback = sum(forecast_temperatures) / len(forecast_temperatures)
        today = dt_util.now().date()
        usage: list[float] = []
        for offset in range(PROJECTION_DAYS):
            if offset < len(forecast_temperatures):
                outdoor = forecast_temperatures[offset]
            else:
                outdoor = seasonal_outdoor_c(
                    self._outdoor_history, today + timedelta(days=offset)
                )
                if outdoor is None:
                    outdoor = fallback
            usage.append(predict_day(model, outdoor, indoor_target_c)[1])
        return usage

    def _run_out(self, litres: float | None, level: float) -> dict[str, Any]:
        """Days and dates until the tank reaches ``level`` (central and earliest)."""
        result: dict[str, Any] = {
            "days": None,
            "date": None,
            "earliest_days": None,
            "earliest_date": None,
            "level_litres": level,
            "beyond_horizon": False,
        }
        if litres is None or self._daily_usage_projection is None:
            return result
        today = dt_util.now().date()
        central = days_until_level(litres, level, self._daily_usage_projection)
        earliest = days_until_level(
            litres,
            level,
            [use * self._upper_scale for use in self._daily_usage_projection],
        )
        if central is None:
            result["beyond_horizon"] = True
        else:
            result["days"] = central
            result["date"] = (today + timedelta(days=int(central))).isoformat()
        if earliest is not None:
            result["earliest_days"] = earliest
            result["earliest_date"] = (
                today + timedelta(days=int(earliest))
            ).isoformat()
        return result

    def _forecast_indoor_target(self) -> tuple[float | None, str | None]:
        """Pick the indoor temperature the forecast should assume.

        Prefer the thermostat target; fall back to the current indoor
        temperature, then to the recent average indoor temperature in history.
        """
        target = self._read_temperature_c(self.entry.data[CONF_INDOOR_TARGET_ENTITY])
        if target is not None:
            return target, "thermostat target"
        indoor = self._read_temperature_c(self.entry.data[CONF_INDOOR_ENTITY])
        if indoor is not None:
            return indoor, "current indoor temperature"
        if self._training_samples:
            recent = self._training_samples[-4:]
            return (
                sum(sample.indoor_mean_c for sample in recent) / len(recent),
                "recent average indoor temperature",
            )
        return None, None

    def _build_data(self) -> dict[str, Any]:
        """Read current tank states, using the sensor's measurement timestamp."""
        depth_state = self.hass.states.get(self.entry.data[CONF_DEPTH_ENTITY])
        timestamp_state = self.hass.states.get(self.entry.data[CONF_TIMESTAMP_ENTITY])

        depth_cm: float | None = None
        if depth_state is not None and depth_state.state not in ("unknown", "unavailable"):
            try:
                depth_cm = float(depth_state.state)
            except (TypeError, ValueError):
                _LOGGER.debug("Tank depth is not numeric: %s", depth_state.state)

        sample_time = None
        if timestamp_state is not None:
            sample_time = parse_sample_timestamp(timestamp_state.state)

        age_minutes = None
        if sample_time is not None:
            age_seconds = (dt_util.utcnow() - sample_time).total_seconds()
            if age_seconds >= 0:
                age_minutes = age_seconds / 60

        stale_after = int(self.entry.data[CONF_STALE_AFTER_MINUTES])
        is_fresh = age_minutes is not None and age_minutes <= stale_after

        litres_remaining = None
        if depth_cm is not None and depth_cm > 0:
            try:
                litres_remaining = depth_to_litres(
                    depth_cm, float(self.entry.data[CONF_TANK_CAPACITY])
                )
            except ValueError:
                _LOGGER.debug("Ignoring invalid tank depth: %s", depth_cm)

        return {
            "depth_cm": depth_cm,
            "litres_remaining": litres_remaining,
            "sample_time": sample_time.isoformat() if sample_time else None,
            "age_minutes": age_minutes,
            "is_stale": not is_fresh or litres_remaining is None,
            "weekly_forecast": self._weekly_forecast,
            "training_weeks": len(self._training_samples),
            "forecast_warning": self._forecast_warning,
            "temperature_offset_c": self.temperature_offset_c,
            "indoor_target_source": self._target_source,
            "reorder": self._run_out(litres_remaining, self.reorder_level_litres),
            "empty": self._run_out(litres_remaining, self.empty_level_litres),
        }

    def _read_temperature_c(self, entity_id: str) -> float | None:
        """Read a temperature in Celsius from a sensor or a climate entity's target."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        if state.domain == "climate":
            raw = state.attributes.get("temperature")
            unit = self.hass.config.units.temperature_unit
        else:
            if state.state in ("unknown", "unavailable"):
                return None
            raw = state.state
            unit = state.attributes.get(
                "unit_of_measurement", UnitOfTemperature.CELSIUS
            )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if unit == UnitOfTemperature.FAHRENHEIT:
            return TemperatureConverter.convert(
                value, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
            )
        if unit == UnitOfTemperature.CELSIUS:
            return value
        return None

    async def _async_load_training_history(self) -> list[DailyTrainingSample]:
        """Read one year of daily long-term statistics and prepare clean weeks."""
        now = dt_util.utcnow()
        start = now - timedelta(days=370)
        entity_ids = {
            self.entry.data[CONF_DEPTH_ENTITY],
            self.entry.data[CONF_OUTDOOR_ENTITY],
            self.entry.data[CONF_INDOOR_ENTITY],
            self.entry.data[CONF_RUNTIME_ENTITY],
        }
        result = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                start,
                now,
                entity_ids,
                "day",
                None,
                {"mean", "max"},
            )
        )

        daily: dict[date, dict[str, float]] = defaultdict(dict)
        role_to_entity = {
            "depth": self.entry.data[CONF_DEPTH_ENTITY],
            "outdoor": self.entry.data[CONF_OUTDOOR_ENTITY],
            "indoor": self.entry.data[CONF_INDOOR_ENTITY],
            "runtime": self.entry.data[CONF_RUNTIME_ENTITY],
        }
        for role, entity_id in role_to_entity.items():
            for row in result.get(entity_id, []):
                timestamp = dt_util.utc_from_timestamp(row["start"])
                local_day = dt_util.as_local(timestamp).date()
                value = row.get("max") if role == "runtime" else row.get("mean")
                if value is not None:
                    daily[local_day][role] = float(value)

        history: list[DailyHistoryPoint] = []
        outdoor_unit = self._entity_unit(self.entry.data[CONF_OUTDOOR_ENTITY])
        indoor_unit = self._entity_unit(self.entry.data[CONF_INDOOR_ENTITY])

        outdoor_history: dict[date, float] = {}
        for day, values in daily.items():
            if "outdoor" not in values:
                continue
            outdoor = values["outdoor"]
            if outdoor_unit == UnitOfTemperature.FAHRENHEIT:
                outdoor = TemperatureConverter.convert(
                    outdoor, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
                )
            outdoor_history[day] = outdoor
        self._outdoor_history = outdoor_history
        for day, values in sorted(daily.items()):
            if not all(key in values for key in role_to_entity):
                continue
            if outdoor_unit == UnitOfTemperature.FAHRENHEIT:
                values["outdoor"] = TemperatureConverter.convert(
                    values["outdoor"],
                    UnitOfTemperature.FAHRENHEIT,
                    UnitOfTemperature.CELSIUS,
                )
            if indoor_unit == UnitOfTemperature.FAHRENHEIT:
                values["indoor"] = TemperatureConverter.convert(
                    values["indoor"],
                    UnitOfTemperature.FAHRENHEIT,
                    UnitOfTemperature.CELSIUS,
                )
            history.append(
                DailyHistoryPoint(
                    day=day,
                    depth_mean_cm=values["depth"],
                    outdoor_mean_c=values["outdoor"],
                    indoor_mean_c=values["indoor"],
                    runtime_max_hours=values["runtime"],
                )
            )

        return build_training_samples(
            history, float(self.entry.data[CONF_TANK_CAPACITY])
        )

    def _entity_unit(self, entity_id: str) -> str | None:
        """Return the current unit of a configured Home Assistant entity."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return self.hass.config.units.temperature_unit
        return state.attributes.get("unit_of_measurement")

    async def _async_get_daily_forecast_c(self) -> list[float]:
        """Fetch seven daily temperatures from the configured weather entity."""
        response = await self.hass.services.async_call(
            "weather",
            "get_forecasts",
            {
                "entity_id": self.entry.data[CONF_WEATHER_ENTITY],
                "type": "daily",
            },
            blocking=True,
            return_response=True,
        )
        forecast = response.get(self.entry.data[CONF_WEATHER_ENTITY], {}).get(
            "forecast", []
        )
        temperatures: list[float] = []
        unit = self.hass.config.units.temperature_unit
        for day in forecast:
            high = day.get("temperature")
            low = day.get("templow", high)
            if high is None or low is None:
                continue
            daily_mean = (float(high) + float(low)) / 2
            if unit == UnitOfTemperature.FAHRENHEIT:
                daily_mean = TemperatureConverter.convert(
                    daily_mean,
                    UnitOfTemperature.FAHRENHEIT,
                    UnitOfTemperature.CELSIUS,
                )
            temperatures.append(daily_mean)
            if len(temperatures) == 7:
                break
        if len(temperatures) != 7:
            raise ValueError("The selected weather entity did not return seven daily forecasts")
        return temperatures
