"""Read Home Assistant tank entities and prepare integration data."""

from collections import defaultdict
from collections.abc import Callable, Mapping
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
    CONF_CALIBRATION,
    CONF_DEPTH_ENTITY,
    CONF_EMPTY_DISTANCE,
    CONF_FULL_DISTANCE,
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
    DOMAIN,
    EMPTY_FRACTION,
    REORDER_FRACTION,
)
from .model import (
    DEPTH_TO_PERCENT,
    LEVEL_DISTANCE,
    LEVEL_LITRES,
    LEVEL_PERCENT,
    MIN_TRAINING_WEEKS,
    SHAPE_CUSTOM,
    DailyHistoryPoint,
    DailyTrainingSample,
    TankConfig,
    UsageModel,
    WeeklyForecast,
    build_training_samples,
    days_until_level,
    estimate_temperature_drift,
    fit_usage_model,
    parse_calibration,
    reading_to_litres,
    forecast_week,
    parse_sample_timestamp,
    predict_day,
    seasonal_outdoor_c,
    temperature_corrected_distance,
)

_LOGGER = logging.getLogger(__name__)

# How far ahead the run-out projection looks.
PROJECTION_DAYS = 365

# The dashboard chart shows this many days, one point every few days.
PROJECTION_CHART_DAYS = 182
PROJECTION_CHART_STEP = 3

# How often the stale flag is re-evaluated when no new reading arrives.
STALE_CHECK_INTERVAL = timedelta(minutes=5)


def tank_from_config(config: Mapping[str, Any]) -> TankConfig:
    """Build the tank description from the entry's settings."""
    calibration = DEPTH_TO_PERCENT
    if config.get(CONF_CALIBRATION):
        try:
            calibration = parse_calibration(config[CONF_CALIBRATION])
        except ValueError as err:
            _LOGGER.warning("Ignoring invalid tank calibration table: %s", err)
    return TankConfig(
        capacity_litres=float(config[CONF_TANK_CAPACITY]),
        level_type=config.get(CONF_LEVEL_TYPE, LEVEL_DISTANCE),
        shape=config.get(CONF_TANK_SHAPE, SHAPE_CUSTOM),
        full_distance_cm=float(config.get(CONF_FULL_DISTANCE, DEPTH_TO_PERCENT[0][0])),
        empty_distance_cm=float(config.get(CONF_EMPTY_DISTANCE, DEPTH_TO_PERCENT[-1][0])),
        calibration=calibration,
    )


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
        self._history_days: dict[str, int] = {}
        self._indoor_fallback_c: float | None = None
        # Ultrasonic temperature correction, decided from the history.
        self.temp_correction: dict[str, Any] = {"active": False, "checked": False}
        self._weather_error: str | None = None
        self.model: UsageModel | None = None
        self._history_entities: dict[str, str] = {}
        self.tank = tank_from_config(entry.data)
        self.reorder_level_litres = round(self.tank.capacity_litres * REORDER_FRACTION)
        self.empty_level_litres = round(self.tank.capacity_litres * EMPTY_FRACTION)
        self._outdoor_history: dict[date, float] = {}
        self._daily_usage_projection: list[float] | None = None
        self._upper_scale = 1.0

    @property
    def training_samples(self) -> list[DailyTrainingSample]:
        """The clean weekly samples the model was fitted to."""
        return list(self._training_samples)

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
        live_entities = [self.entry.data[CONF_DEPTH_ENTITY]]
        if self.entry.data.get(CONF_TIMESTAMP_ENTITY):
            live_entities.append(self.entry.data[CONF_TIMESTAMP_ENTITY])
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

            if len(self._training_samples) < MIN_TRAINING_WEEKS:
                self._weekly_forecast = None
                self._forecast_warning = self._history_warning()
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
            self.model = model
            try:
                forecast_temperatures = await self._async_get_daily_forecast_c()
            except (HomeAssistantError, ValueError, TypeError, KeyError) as err:
                self._weather_error = f"Could not get a 7-day daily forecast: {err}"
                raise
            self._weather_error = None
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

    def _history_warning(self) -> str:
        """Explain why there are not enough clean weeks to estimate from."""
        labels = {
            "level": "tank",
            "outdoor": "outdoor temperature",
            "indoor": "indoor temperature",
            "runtime": "heating runtime",
            "tank_temp": "tank temperature",
        }
        missing = [
            f"{labels[role]} ({self._history_entities[role]})"
            for role, days in self._history_days.items()
            if days == 0
            and role != "tank_temp"
            and not (role == "indoor" and self._indoor_fallback_c is not None)
        ]
        if missing:
            return (
                "No daily history was found for "
                + ", ".join(missing)
                + ". That sensor needs long-term statistics (state class "
                "\"measurement\", or \"total\" for a runtime counter); choose another "
                "sensor under Configure, or leave the optional runtime sensor empty."
            )
        return (
            f"Needs at least {MIN_TRAINING_WEEKS} complete, clean weeks of history; "
            "estimates start automatically once they are recorded"
        )

    def setup_check(self) -> list[dict[str, Any]]:
        """Check each configured entity and explain anything that will cause problems."""
        config = self.entry.data
        items: list[tuple[str, str, str]] = [
            ("level", "Tank sensor", config[CONF_DEPTH_ENTITY]),
            ("outdoor", "Outdoor temperature", config[CONF_OUTDOOR_ENTITY]),
            ("indoor", "Indoor temperature", config[CONF_INDOOR_ENTITY]),
            ("target", "Thermostat", config[CONF_INDOOR_TARGET_ENTITY]),
            ("weather", "Weather forecast", config[CONF_WEATHER_ENTITY]),
        ]
        if config.get(CONF_RUNTIME_ENTITY):
            items.append(("runtime", "Heating runtime", config[CONF_RUNTIME_ENTITY]))
        if config.get(CONF_TANK_TEMP_ENTITY):
            items.append(("tank_temp", "Tank temperature", config[CONF_TANK_TEMP_ENTITY]))
        if config.get(CONF_TIMESTAMP_ENTITY):
            items.append(("timestamp", "Measurement time", config[CONF_TIMESTAMP_ENTITY]))

        level_units = {
            LEVEL_DISTANCE: ("cm",),
            LEVEL_PERCENT: ("%",),
            LEVEL_LITRES: ("L", "l"),
        }
        level_names = {
            LEVEL_DISTANCE: "a distance in cm",
            LEVEL_PERCENT: "percent full",
            LEVEL_LITRES: "litres",
        }
        rows: list[dict[str, Any]] = []
        for role, label, entity_id in items:
            problems: list[str] = []
            notes: list[str] = []
            state = self.hass.states.get(entity_id)
            days = self._history_days.get(role)
            if state is None:
                problems.append(
                    "Not found. It may have been renamed or removed; choose it again under Configure."
                )
            elif state.state in ("unknown", "unavailable"):
                problems.append(f"Currently {state.state}.")
            else:
                unit = state.attributes.get("unit_of_measurement")
                if role in ("level", "outdoor", "indoor", "runtime", "tank_temp"):
                    try:
                        float(state.state)
                    except ValueError:
                        problems.append(f"Its value \"{state.state}\" is not a number.")
                if role == "level" and unit and unit not in level_units[self.tank.level_type]:
                    problems.append(
                        f"Its unit is {unit}, but Oil Monitor is set up for "
                        f"{level_names[self.tank.level_type]}. Check the sensor type under Configure."
                    )
                if role in ("outdoor", "indoor", "tank_temp") and unit not in (
                    UnitOfTemperature.CELSIUS,
                    UnitOfTemperature.FAHRENHEIT,
                ):
                    problems.append(f"Its unit is {unit or 'missing'}; it should be °C or °F.")
                if role == "target" and self._read_temperature_c(entity_id) is None:
                    problems.append("No target temperature could be read from it.")
                if role == "weather" and self._weather_error:
                    problems.append(self._weather_error)
                if role == "timestamp" and parse_sample_timestamp(state.state) is None:
                    problems.append(f"Its value \"{state.state}\" is not a recognisable time.")
                if role in ("level", "outdoor", "indoor", "runtime", "tank_temp"):
                    no_class = state.attributes.get("state_class") is None
                    fallback = role == "indoor" and self._indoor_fallback_c is not None
                    if no_class or days == 0:
                        reason = (
                            "It has no state class, so Home Assistant keeps no long-term "
                            "statistics (daily history) for it."
                            if no_class
                            else "No daily history has been recorded for it yet."
                        )
                        if fallback:
                            notes.append(
                                f"{reason} Past weeks are assumed to have been "
                                f"{self._indoor_fallback_c:.1f} °C indoors; choose a "
                                "sensor with history under Configure for better estimates."
                            )
                        elif role == "tank_temp":
                            notes.append(
                                f"{reason} The outdoor temperature is used for the "
                                "tank reading correction instead."
                            )
                        elif role == "outdoor":
                            notes.append(f"{reason} Charts and estimates need it.")
                        else:
                            problems.append(f"{reason} Charts and estimates need it.")
            status = "problem" if problems else "warning" if notes else "ok"
            if status == "ok":
                message = f"OK · {days} days of history" if days else "OK"
            else:
                message = " ".join(problems + notes)
            rows.append(
                {
                    "role": role,
                    "label": label,
                    "entity_id": entity_id,
                    "status": status,
                    "message": message,
                    "history_days": days,
                }
            )
        if self.tank.level_type == LEVEL_DISTANCE:
            correction = self.temp_correction
            if correction["active"]:
                message = (
                    f"On. Your tank readings drift about "
                    f"{correction['coefficient_per_c'] * 100:.2f} % per °C (found in "
                    f"{correction['day_pairs']} days of history), so they are "
                    f"corrected to {correction['reference_c']:.0f} °C."
                )
            elif correction["checked"]:
                message = (
                    "Not needed. Your tank readings don't drift with temperature, "
                    "so the sensor already compensates."
                )
            else:
                message = "Not enough history yet to check for temperature drift."
            rows.append(
                {
                    "role": "temp_correction",
                    "label": "Temperature correction",
                    "entity_id": correction.get("entity_id") or "",
                    "status": "ok",
                    "message": message,
                    "history_days": None,
                }
            )
        return rows

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

    def _level_projection(self, litres: float | None) -> list[dict[str, Any]] | None:
        """Projected litres every few days for about six months, for charting.

        ``litres`` is the central estimate; ``cautious`` uses the upper end of
        the usage range (the tank empties sooner).
        """
        if litres is None or self._daily_usage_projection is None:
            return None
        today = dt_util.now().date()
        points: list[dict[str, Any]] = []
        central = cautious = litres
        for offset, use in enumerate(self._daily_usage_projection[:PROJECTION_CHART_DAYS + 1]):
            if offset % PROJECTION_CHART_STEP == 0:
                points.append(
                    {
                        "date": (today + timedelta(days=offset)).isoformat(),
                        "litres": round(max(0.0, central), 1),
                        "cautious": round(max(0.0, cautious), 1),
                    }
                )
            central -= use
            cautious -= use * self._upper_scale
        return points

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
        """Read the current tank state and work out litres and freshness."""
        level_state = self.hass.states.get(self.entry.data[CONF_DEPTH_ENTITY])
        level_ok = level_state is not None and level_state.state not in (
            "unknown",
            "unavailable",
        )

        reading: float | None = None
        if level_ok:
            try:
                reading = float(level_state.state)
            except (TypeError, ValueError):
                _LOGGER.debug("Tank reading is not numeric: %s", level_state.state)

        # Prefer the sensor's own measurement time; otherwise use when Home
        # Assistant last received a report from the tank sensor.
        sample_time = None
        timestamp_entity = self.entry.data.get(CONF_TIMESTAMP_ENTITY)
        if timestamp_entity:
            timestamp_state = self.hass.states.get(timestamp_entity)
            if timestamp_state is not None:
                sample_time = parse_sample_timestamp(timestamp_state.state)
        elif level_ok:
            sample_time = getattr(level_state, "last_reported", None) or level_state.last_updated

        age_minutes = None
        if sample_time is not None:
            age_seconds = (dt_util.utcnow() - sample_time).total_seconds()
            age_minutes = max(0.0, age_seconds) / 60

        stale_after = int(self.entry.data[CONF_STALE_AFTER_MINUTES])
        is_fresh = age_minutes is not None and age_minutes <= stale_after

        litres_remaining = None
        if reading is not None:
            try:
                litres_remaining = reading_to_litres(
                    self._corrected_reading(reading), self.tank
                )
            except ValueError:
                _LOGGER.debug("Ignoring invalid tank reading: %s", reading)

        forecast = self._weekly_forecast
        return {
            "level_reading": reading,
            "level_unit": level_state.attributes.get("unit_of_measurement") if level_ok else None,
            "litres_remaining": litres_remaining,
            "sample_time": sample_time.isoformat() if sample_time else None,
            "age_minutes": age_minutes,
            "is_stale": not is_fresh or litres_remaining is None,
            "weekly_forecast": forecast,
            "early_estimate": bool(forecast and forecast.early),
            "training_weeks": len(self._training_samples),
            "training_weeks_needed": MIN_TRAINING_WEEKS,
            "history_days": dict(self._history_days),
            "indoor_history_fallback_c": self._indoor_fallback_c,
            "temp_correction": dict(self.temp_correction),
            "setup_check": self.setup_check(),
            "forecast_warning": self._forecast_warning,
            "temperature_offset_c": self.temperature_offset_c,
            "indoor_target_source": self._target_source,
            "reorder": self._run_out(litres_remaining, self.reorder_level_litres),
            "empty": self._run_out(litres_remaining, self.empty_level_litres),
            "level_projection": self._level_projection(litres_remaining),
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
        role_to_entity = {
            "level": self.entry.data[CONF_DEPTH_ENTITY],
            "outdoor": self.entry.data[CONF_OUTDOOR_ENTITY],
            "indoor": self.entry.data[CONF_INDOOR_ENTITY],
        }
        if self.entry.data.get(CONF_RUNTIME_ENTITY):
            role_to_entity["runtime"] = self.entry.data[CONF_RUNTIME_ENTITY]
        if self.entry.data.get(CONF_TANK_TEMP_ENTITY):
            role_to_entity["tank_temp"] = self.entry.data[CONF_TANK_TEMP_ENTITY]
        entity_ids = set(role_to_entity.values())
        result = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                start,
                now,
                entity_ids,
                "day",
                None,
                {"mean", "max", "change"},
            )
        )

        daily: dict[date, dict[str, float]] = defaultdict(dict)
        for role, entity_id in role_to_entity.items():
            for row in result.get(entity_id, []):
                timestamp = dt_util.utc_from_timestamp(row["start"])
                local_day = dt_util.as_local(timestamp).date()
                if role == "runtime":
                    # A runtime that resets daily has a daily maximum; a
                    # counter that keeps adding up has a daily increase.
                    value = row.get("max")
                    if value is None:
                        value = row.get("change")
                else:
                    value = row.get("mean")
                if value is not None:
                    daily[local_day][role] = float(value)

        self._history_days = {
            role: sum(1 for values in daily.values() if role in values)
            for role in role_to_entity
        }
        self._history_entities = role_to_entity

        # Without any indoor history, assume the house was kept at today's
        # thermostat setting (or current indoor temperature) in past weeks.
        required = set(role_to_entity) - {"tank_temp"}
        self._indoor_fallback_c = None
        if self._history_days.get("indoor") == 0:
            fallback = self._read_temperature_c(
                self.entry.data[CONF_INDOOR_TARGET_ENTITY]
            )
            if fallback is None:
                fallback = self._read_temperature_c(self.entry.data[CONF_INDOOR_ENTITY])
            if fallback is not None:
                self._indoor_fallback_c = fallback
                required.discard("indoor")

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
        air_temps = self._update_temp_correction(daily, outdoor_history)
        for day, values in sorted(daily.items()):
            if not all(key in values for key in required):
                continue
            if "indoor" not in values:
                values["indoor"] = self._indoor_fallback_c
                indoor_is_celsius = True
            else:
                indoor_is_celsius = False
            if outdoor_unit == UnitOfTemperature.FAHRENHEIT:
                values["outdoor"] = TemperatureConverter.convert(
                    values["outdoor"],
                    UnitOfTemperature.FAHRENHEIT,
                    UnitOfTemperature.CELSIUS,
                )
            if indoor_unit == UnitOfTemperature.FAHRENHEIT and not indoor_is_celsius:
                values["indoor"] = TemperatureConverter.convert(
                    values["indoor"],
                    UnitOfTemperature.FAHRENHEIT,
                    UnitOfTemperature.CELSIUS,
                )
            try:
                level = values["level"]
                if self.temp_correction["active"] and day in air_temps:
                    level = temperature_corrected_distance(
                        level, air_temps[day], self.temp_correction["reference_c"]
                    )
                litres = reading_to_litres(level, self.tank)
            except ValueError:
                continue
            history.append(
                DailyHistoryPoint(
                    day=day,
                    litres_mean=litres,
                    outdoor_mean_c=values["outdoor"],
                    indoor_mean_c=values["indoor"],
                    runtime_max_hours=values.get("runtime"),
                )
            )

        return build_training_samples(history, self.tank.capacity_litres)

    def _update_temp_correction(
        self,
        daily: Mapping[date, Mapping[str, float]],
        outdoor_history: Mapping[date, float],
    ) -> dict[date, float]:
        """Decide from history whether distance readings need temperature correction.

        Returns the air temperature (°C) to use for each day: the tank
        temperature sensor when it has history, otherwise outdoor temperature.
        """
        entity_id = self.entry.data[CONF_OUTDOOR_ENTITY]
        air_temps: dict[date, float] = dict(outdoor_history)
        tank_temp_entity = self.entry.data.get(CONF_TANK_TEMP_ENTITY)
        if tank_temp_entity and self._history_days.get("tank_temp"):
            fahrenheit = self._entity_unit(tank_temp_entity) == UnitOfTemperature.FAHRENHEIT
            air_temps = {
                day: (
                    TemperatureConverter.convert(
                        values["tank_temp"],
                        UnitOfTemperature.FAHRENHEIT,
                        UnitOfTemperature.CELSIUS,
                    )
                    if fahrenheit
                    else values["tank_temp"]
                )
                for day, values in daily.items()
                if "tank_temp" in values
            }
            entity_id = tank_temp_entity

        self.temp_correction = {"active": False, "checked": False, "entity_id": entity_id}
        if self.tank.level_type != LEVEL_DISTANCE:
            return air_temps
        samples = [
            (day, values["level"], air_temps[day])
            for day, values in daily.items()
            if "level" in values and day in air_temps
        ]
        estimate = estimate_temperature_drift(samples)
        if estimate is None:
            return air_temps
        self.temp_correction.update(
            {
                "checked": True,
                "active": estimate.needs_correction,
                "coefficient_per_c": round(estimate.coefficient_per_c, 5),
                "t_statistic": round(estimate.t_statistic, 1),
                "day_pairs": estimate.day_pairs,
                # Correct to the average temperature, so typical readings
                # (and the tank calibration) stay where they were.
                "reference_c": float(round(sum(t for _, _, t in samples) / len(samples))),
            }
        )
        return air_temps

    def _corrected_reading(self, reading: float) -> float:
        """Apply the temperature correction to a live distance reading."""
        correction = self.temp_correction
        if not correction.get("active"):
            return reading
        air_temp = self._read_temperature_c(correction["entity_id"])
        if air_temp is None:
            return reading
        return temperature_corrected_distance(reading, air_temp, correction["reference_c"])

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
