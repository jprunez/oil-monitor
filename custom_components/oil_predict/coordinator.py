"""Read Home Assistant tank entities and prepare integration data."""

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.components.recorder.history import state_changes_during_period
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
    DOMAIN,
    EMPTY_FRACTION,
    REORDER_FRACTION,
)
from .checks import (
    STATISTIC_ROLES,
    check_entity,
    entity_roles,
    read_temperature_c,
    temperature_c_from_state,
)
from .climate_history import ClimateHistoryManager
from .model import (
    DEPTH_TO_PERCENT,
    FALLBACK_SCENARIO_SHIFT_C,
    REFILL_RISE_FRACTION,
    active_setpoints,
    time_weighted_average,
    LEVEL_DISTANCE,
    LEVEL_LITRES,
    LEVEL_PERCENT,
    MIN_TRAINING_WEEKS,
    MIN_WEATHER_WEEKS,
    SHAPE_CUSTOM,
    DailyHistoryPoint,
    DailyTrainingSample,
    BacktestResult,
    TankConfig,
    UsageModel,
    WEEK_IMPLAUSIBLE,
    WEEK_MISSING_DAYS,
    WEEK_REFILL,
    WEEK_USED,
    WeatherTerms,
    WeekReport,
    WeeklyForecast,
    backtest_weekly,
    classify_weeks,
    climate_offset,
    days_until_level,
    estimate_temperature_drift,
    fit_usage_model,
    fit_weather_terms,
    historical_years,
    parse_calibration,
    pick_scenarios,
    reading_to_litres,
    same_day_in_year,
    forecast_week,
    parse_sample_timestamp,
    predict_day,
    seasonal_outdoor_c,
    temperature_corrected_distance,
    weather_terms_help,
)

_LOGGER = logging.getLogger(__name__)

# How far ahead the run-out projection looks.
PROJECTION_DAYS = 365

# The dashboard chart shows this many days, one point every few days.
PROJECTION_CHART_DAYS = 182
PROJECTION_CHART_STEP = 3
# The thermostat's active (non-setback) target is averaged over this period,
# so a setback in force right now doesn't set the forecast.
TARGET_AVERAGE_PERIOD = timedelta(days=7)
# Settings this far below the week's highest are setbacks (night, away,
# frost protection): the house cools slowly, so they barely move the daily
# average indoor temperature the model learns from.
SETBACK_MARGIN_C = 3.0

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
        # For the Data check page: per-sensor coverage and per-week outcomes.
        self._coverage: dict[str, dict[str, Any]] = {}
        self._coverage_weeks: list[str] = []
        self._week_reports: list[dict[str, Any]] = []
        self._indoor_fallback_c: float | None = None
        # Ultrasonic temperature correction, decided from the history.
        self.temp_correction: dict[str, Any] = {"active": False, "checked": False}
        self._weather_error: str | None = None
        self.model: UsageModel | None = None
        self.backtest: BacktestResult | None = None
        # Wind and sun are used only if they improved past forecasts.
        self.weather_terms: WeatherTerms | None = None
        self.weather_terms_info: dict[str, Any] = {"status": "off_no_data"}
        self._history_entities: dict[str, str] = {}
        self.tank = tank_from_config(entry.data)
        self.reorder_level_litres = round(self.tank.capacity_litres * REORDER_FRACTION)
        self.empty_level_litres = round(self.tank.capacity_litres * EMPTY_FRACTION)
        self._outdoor_history: dict[date, float] = {}
        # Projected daily litres for a typical, mild and cold winter.
        self._usage: dict[str, list[float]] | None = None
        self._litres_history: dict[date, float] = {}
        self.scenario_info: dict[str, Any] | None = None
        self.climate: ClimateHistoryManager | None = (
            ClimateHistoryManager(hass, entry.data.get(CONF_HISTORY_LOCATION))
            if entry.data.get(CONF_WEATHER_HISTORY, True)
            else None
        )

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
        self._usage = None
        try:
            if (
                self._history_loaded_at is None
                or dt_util.utcnow() - self._history_loaded_at > timedelta(hours=24)
            ):
                self._training_samples = await self._async_load_training_history()
                self._history_loaded_at = dt_util.utcnow()
                # Score past weeks with models that never saw them (many small
                # fits, so off the event loop).
                self.backtest = await self.hass.async_add_executor_job(
                    backtest_weekly, self._training_samples
                )
                await self._async_evaluate_weather_terms()

            if len(self._training_samples) < MIN_TRAINING_WEEKS:
                self._weekly_forecast = None
                self._forecast_warning = self._history_warning()
                return

            temperature_target, self._target_source = await self._async_forecast_indoor_target()
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
            self.weather_terms = None
            week_adjust: list[float] | None = None
            if self.weather_terms_info["status"] == "on" and self.climate is not None:
                self.weather_terms = fit_weather_terms(model, self._training_samples)
            if self.weather_terms is not None:
                recent = await self.climate.async_get_recent()
                today = dt_util.now().date()
                week_adjust = [
                    self.weather_terms.adjustment(
                        model,
                        temp,
                        indoor_target,
                        recent["wind"].get(today + timedelta(days=index)),
                        recent["sun"].get(today + timedelta(days=index)),
                    )
                    for index, temp in enumerate(forecast_temperatures)
                ]
            self._weekly_forecast = forecast_week(
                model,
                forecast_temperatures,
                indoor_target,
                self.backtest.rmse_litres if self.backtest else None,
                week_adjust,
            )
            await self._async_build_scenarios(
                model, forecast_temperatures, indoor_target
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
        rows: list[dict[str, Any]] = [
            check_entity(
                self.hass,
                role,
                label,
                entity_id,
                level_type=self.tank.level_type,
                history_days=self._history_days.get(role),
                indoor_fallback_c=self._indoor_fallback_c,
                weather_error=self._weather_error,
            )
            for role, label, entity_id in entity_roles(self.entry.data)
        ]
        info = self.scenario_info
        if info is not None:
            status = "ok"
            if info["source"] == "weather_history":
                message = (
                    f"Using {info['years']} years of weather history for {info['place']} "
                    "(location rounded to about 10 km)."
                )
            elif info.get("error"):
                status = "warning"
                message = (
                    f"{info['error']} Using last year's temperatures, "
                    f"{info['shift_c']:.0f} °C warmer and colder, instead."
                )
            elif info.get("weather_history_enabled"):
                message = (
                    "Weather history isn't available yet. Using last year's temperatures, "
                    f"{info['shift_c']:.0f} °C warmer and colder."
                )
            else:
                message = (
                    "Weather history is switched off. Using last year's temperatures, "
                    f"{info['shift_c']:.0f} °C warmer and colder."
                )
            rows.append(
                {
                    "role": "scenarios",
                    "label": "Winter scenarios",
                    "entity_id": "",
                    "status": status,
                    "message": message,
                    "history_days": None,
                }
            )
        weather = self.weather_terms_info
        if self.backtest is not None or weather["status"] == "disabled":
            messages = {
                "on": (
                    f"On. Including wind and sun made past weekly forecasts "
                    f"{weather.get('gain_percent', 0):.0f} % more accurate "
                    f"(tested on {weather.get('weeks', 0)} weeks)."
                ),
                "off_no_gain": (
                    "Off. Tested on your history: wind and sun didn't make past "
                    "forecasts more accurate, so they are left out."
                ),
                "off_no_data": (
                    f"Off. Needs weather history and at least {MIN_WEATHER_WEEKS} clean weeks."
                ),
                "disabled": "Off. Weather history is switched off.",
            }
            rows.append(
                {
                    "role": "wind_sun",
                    "label": "Wind and sun",
                    "entity_id": "",
                    "status": "ok",
                    "message": messages[weather["status"]],
                    "history_days": None,
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

    async def _async_build_scenarios(
        self,
        model: UsageModel,
        forecast_temperatures: list[float],
        indoor_target_c: float,
    ) -> None:
        """Project daily litres for a year, for a typical, mild and cold winter.

        The first week follows the weather forecast in every scenario. With
        long-term weather history, later days use each past year's
        temperatures for the same dates: typical is the average across years,
        mild and cold are real years near the 10th and 90th percentile of oil
        use. Without it, last year's temperatures are shifted up and down.
        """
        today = dt_util.now().date()
        week = [day.predicted_litres for day in self._weekly_forecast.days]
        terms = self.weather_terms
        rest_start = today + timedelta(days=len(week))
        rest_days = PROJECTION_DAYS - len(week)

        history = await self.climate.async_get() if self.climate is not None else None
        if history is not None:
            offset = climate_offset(self._outdoor_history, history.daily)
            years = historical_years(history.daily, rest_start, rest_days)
            wind_years = historical_years(history.wind, rest_start, rest_days) if terms else {}
            sun_years = historical_years(history.sun, rest_start, rest_days) if terms else {}

            def year_usage(back: int, temps: list[float]) -> list[float]:
                wind = wind_years.get(back)
                sun = sun_years.get(back)
                usage: list[float] = []
                for index, temp in enumerate(temps):
                    litres = predict_day(model, temp + offset, indoor_target_c)[1]
                    if terms is not None:
                        litres = max(
                            0.0,
                            litres
                            + terms.adjustment(
                                model,
                                temp + offset,
                                indoor_target_c,
                                wind[index] if wind else None,
                                sun[index] if sun else None,
                            ),
                        )
                    usage.append(litres)
                return usage

            picked = pick_scenarios(
                [year_usage(back, temps) for back, temps in years.items()]
            )
            if picked is not None:
                typical, mild, cold = picked
                self._usage = {
                    "typical": week + typical,
                    "mild": week + mild,
                    "cold": week + cold,
                }
                self.scenario_info = {
                    "source": "weather_history",
                    "years": len(years),
                    "place": history.place,
                    "latitude": history.latitude,
                    "longitude": history.longitude,
                    "offset_c": round(offset, 2),
                }
                return

        # Fallback: last year's temperatures (or the forecast week's average
        # where there is no history), shifted for the mild and cold cases.
        fallback = sum(forecast_temperatures) / len(forecast_temperatures)
        temps: list[float] = []
        for offset_days in range(rest_days):
            temp = seasonal_outdoor_c(
                self._outdoor_history, rest_start + timedelta(days=offset_days)
            )
            temps.append(fallback if temp is None else temp)

        def usage(shift: float) -> list[float]:
            return week + [
                predict_day(model, temp + shift, indoor_target_c)[1] for temp in temps
            ]

        self._usage = {
            "typical": usage(0.0),
            "mild": usage(FALLBACK_SCENARIO_SHIFT_C),
            "cold": usage(-FALLBACK_SCENARIO_SHIFT_C),
        }
        self.scenario_info = {
            "source": "last_year_shifted",
            "shift_c": FALLBACK_SCENARIO_SHIFT_C,
            "weather_history_enabled": self.climate is not None,
            "error": self.climate.error if self.climate is not None else None,
        }

    async def _async_evaluate_weather_terms(self) -> None:
        """Attach wind and sun to past weeks and test whether they help.

        Wind and sun are switched on only when a model using them would have
        predicted past weeks measurably better than one without.
        """
        self.weather_terms_info = {"status": "disabled" if self.climate is None else "off_no_data"}
        if self.climate is None:
            return
        history = await self.climate.async_get()
        if history is None or not history.wind or not history.sun:
            return
        recent = await self.climate.async_get_recent()
        wind = {**history.wind, **recent.get("wind", {})}
        sun = {**history.sun, **recent.get("sun", {})}

        samples: list[DailyTrainingSample] = []
        with_weather = 0
        for sample in self._training_samples:
            days = (
                [sample.week_start + timedelta(days=offset) for offset in range(7)]
                if sample.week_start is not None
                else []
            )
            if days and all(day in wind and day in sun for day in days):
                sample = replace(
                    sample,
                    wind_kmh=sum(wind[day] for day in days) / 7,
                    sun_mj=sum(sun[day] for day in days) / 7,
                )
                with_weather += 1
            samples.append(sample)
        self._training_samples = samples
        if self.backtest is None or with_weather < MIN_WEATHER_WEEKS:
            return

        scored = await self.hass.async_add_executor_job(backtest_weekly, samples, True)
        gain = (
            1 - scored.rmse_litres / self.backtest.rmse_litres
            if scored is not None and self.backtest.rmse_litres > 0
            else 0.0
        )
        self.weather_terms_info = {
            "status": "on" if weather_terms_help(self.backtest, scored) else "off_no_gain",
            "weeks": with_weather,
            "gain_percent": round(gain * 100, 1),
        }
        if self.weather_terms_info["status"] == "on":
            self.backtest = scored

    def _level_projection(self, litres: float | None) -> list[dict[str, Any]] | None:
        """Projected litres every few days for about six months, for charting."""
        if litres is None or self._usage is None:
            return None
        today = dt_util.now().date()
        points: list[dict[str, Any]] = self._level_history_since_fill(today)
        level = {name: litres for name in self._usage}
        # What the tank would do from today's level if oil were used exactly
        # as it was over the same dates last year (deliveries ignored).
        last_year: float | None = litres
        refill_rise = REFILL_RISE_FRACTION * self.tank.capacity_litres
        for offset in range(PROJECTION_CHART_DAYS + 1):
            if offset % PROJECTION_CHART_STEP == 0:
                points.append(
                    {
                        "date": (today + timedelta(days=offset)).isoformat(),
                        "actual": round(litres, 1) if offset == 0 else None,
                        "litres": round(max(0.0, level["typical"]), 1),
                        "mild": round(max(0.0, level["mild"]), 1),
                        "cold": round(max(0.0, level["cold"]), 1),
                        "last_year": (
                            None if last_year is None else round(max(0.0, last_year), 1)
                        ),
                    }
                )
            for name, usage in self._usage.items():
                level[name] -= usage[offset]
            if last_year is not None:
                day = same_day_in_year(today + timedelta(days=offset), 1)
                before = self._litres_history.get(day)
                after = self._litres_history.get(day + timedelta(days=1))
                if before is None or after is None:
                    last_year = None  # no reading a year ago: stop the line
                elif after - before <= refill_rise:
                    last_year -= before - after
        return points

    def _level_history_since_fill(self, today: date) -> list[dict[str, Any]]:
        """Recorded litres from the most recent fill up to yesterday.

        Walks back from today until the day after a refill (a day-to-day rise
        above the refill threshold), a gap in the history, or about a year.
        Points use the same spacing as the projection so the two line up.
        """
        history = self._litres_history
        refill_rise = REFILL_RISE_FRACTION * self.tank.capacity_litres
        start = today
        for _ in range(PROJECTION_DAYS):
            previous = start - timedelta(days=1)
            if previous not in history:
                break
            if start in history and history[start] - history[previous] > refill_rise:
                break  # ``start`` is the first day after the fill
            start = previous
        if start >= today:
            return []
        # Align to the projection's step so today stays on the grid.
        step = PROJECTION_CHART_STEP
        first = (today - start).days // step * step
        points = []
        for back in range(first, 0, -step):
            day = today - timedelta(days=back)
            value = history.get(day)
            points.append(
                {
                    "date": day.isoformat(),
                    "actual": None if value is None else round(max(0.0, value), 1),
                    "litres": None,
                    "mild": None,
                    "cold": None,
                    "last_year": None,
                }
            )
        return points

    def _run_out(self, litres: float | None, level: float) -> dict[str, Any]:
        """Days and dates until the tank reaches ``level`` in each scenario.

        ``days``/``date`` are for a typical winter, ``earliest_*`` for a cold
        one and ``latest_*`` for a mild one.
        """
        result: dict[str, Any] = {
            "days": None,
            "date": None,
            "earliest_days": None,
            "earliest_date": None,
            "latest_days": None,
            "latest_date": None,
            "level_litres": level,
            "beyond_horizon": False,
        }
        if litres is None or self._usage is None:
            return result
        today = dt_util.now().date()
        for prefix, scenario in (("", "typical"), ("earliest_", "cold"), ("latest_", "mild")):
            days = days_until_level(litres, level, self._usage[scenario])
            if days is None:
                if scenario == "typical":
                    result["beyond_horizon"] = True
                continue
            result[f"{prefix}days"] = days
            result[f"{prefix}date"] = (today + timedelta(days=int(days))).isoformat()
        return result

    async def _async_forecast_indoor_target(self) -> tuple[float | None, str | None]:
        """Pick the indoor temperature the forecast should assume.

        Prefer the thermostat's active target over the past week, ignoring
        setbacks; then its current value; then the current indoor
        temperature; then the recent average indoor temperature in history.
        """
        entity_id = self.entry.data[CONF_INDOOR_TARGET_ENTITY]
        average = await self._async_active_target_c(entity_id)
        if average is not None:
            return average, "thermostat target when heating (7-day average)"
        target = self._read_temperature_c(entity_id)
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

    async def _async_active_target_c(self, entity_id: str) -> float | None:
        """Time-weighted average of the active thermostat target this week.

        Setback periods (more than ``SETBACK_MARGIN_C`` below the week's
        highest setting) are left out. Returns None with less than six hours
        of usable history, so the caller falls back to the current setting.
        """
        end = dt_util.utcnow()
        start = end - TARGET_AVERAGE_PERIOD
        try:
            changes = await get_instance(self.hass).async_add_executor_job(
                partial(
                    state_changes_during_period,
                    self.hass,
                    start,
                    end,
                    entity_id,
                    include_start_time_state=True,
                )
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Could not read thermostat history: %s", err)
            return None
        samples = [
            (max(state.last_changed, start), temperature_c_from_state(self.hass, state))
            for state in changes.get(entity_id.lower(), [])
        ]
        return time_weighted_average(
            active_setpoints(samples, SETBACK_MARGIN_C), end, min_seconds=6 * 3600
        )

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
            "scenario_info": dict(self.scenario_info) if self.scenario_info else None,
            "backtest": asdict(self.backtest) if self.backtest else None,
            "weather_terms": {
                **self.weather_terms_info,
                **(asdict(self.weather_terms) if self.weather_terms else {}),
            },
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
        return read_temperature_c(self.hass, entity_id)

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
        self._update_coverage(daily, role_to_entity)

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
        bad_level_days: set[date] = set()
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

        # Daily litres for every day with a tank reading, for the dashboard's
        # "last year" comparison.
        litres_history: dict[date, float] = {}
        for day, values in daily.items():
            if "level" not in values:
                continue
            level = values["level"]
            if self.temp_correction["active"] and day in air_temps:
                level = temperature_corrected_distance(
                    level, air_temps[day], self.temp_correction["reference_c"]
                )
            try:
                litres_history[day] = reading_to_litres(level, self.tank)
            except ValueError:
                continue
        self._litres_history = litres_history
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
                bad_level_days.add(day)
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

        classified = classify_weeks(history, self.tank.capacity_litres)
        self._week_reports = [
            self._describe_week(report, daily, required, bad_level_days)
            for report, _ in classified
        ]
        return [sample for _, sample in classified if sample is not None]

    def _update_coverage(
        self, daily: Mapping[date, Mapping[str, float]], roles: Mapping[str, str]
    ) -> None:
        """Summarise how much daily history each sensor has over the last year."""
        today = dt_util.now().date()
        this_monday = today - timedelta(days=today.weekday())
        mondays = [this_monday - timedelta(weeks=back) for back in range(52, -1, -1)]
        self._coverage_weeks = [monday.isoformat() for monday in mondays]
        self._coverage = {}
        for role in roles:
            days = sorted(day for day, values in daily.items() if role in values)
            largest_gap = 0
            for previous, current in zip(days, days[1:]):
                largest_gap = max(largest_gap, (current - previous).days - 1)
            present = set(days)
            self._coverage[role] = {
                "days": len(days),
                "first_day": days[0].isoformat() if days else None,
                "last_day": days[-1].isoformat() if days else None,
                "largest_gap_days": largest_gap,
                "weekly_days": [
                    sum(1 for offset in range(7) if monday + timedelta(days=offset) in present)
                    for monday in mondays
                ],
            }

    def _describe_week(
        self,
        report: WeekReport,
        daily: Mapping[date, Mapping[str, float]],
        required: set[str],
        bad_level_days: set[date],
    ) -> dict[str, Any]:
        """Explain in plain words what happened to one week of history."""
        labels = {
            "level": "tank",
            "outdoor": "outdoor temperature",
            "indoor": "indoor temperature",
            "runtime": "heating runtime",
        }
        litres = report.litres_used
        missing: dict[str, int] = {}
        if report.status == WEEK_USED:
            reason = f"Used {litres:.0f} L"
        elif report.status == WEEK_REFILL:
            reason = "Delivery detected, so the amount used can't be measured"
        elif report.status == WEEK_IMPLAUSIBLE:
            reason = (
                f"The tank level rose by {-litres:.0f} L without a delivery"
                if litres is not None and litres < 0
                else f"A drop of {litres:.0f} L in one week looks like a sensor fault"
            )
        elif report.status == WEEK_MISSING_DAYS:
            # The Sunday before anchors the week's tank change, so it counts too.
            days = [report.week_start + timedelta(days=offset) for offset in range(-1, 7)]
            for role in sorted(required):
                count = sum(1 for day in days if role not in daily.get(day, {}))
                if count:
                    missing[labels.get(role, role)] = count
            bad = sum(1 for day in days if day in bad_level_days)
            if bad:
                missing["tank (reading out of range)"] = bad
            reason = "No data from " + ", ".join(
                f"{name} on {count} day{'s' if count != 1 else ''}"
                for name, count in missing.items()
            ) if missing else "Some days had no data"
        else:
            reason = "A reading was not a valid number"
        return {
            "week_start": report.week_start.isoformat(),
            "status": report.status,
            "litres_used": None if litres is None else round(litres, 1),
            "reason": reason,
            "missing": missing,
        }

    def data_check(self) -> dict[str, Any]:
        """Everything the dashboard's Data check page shows."""
        rows = self.setup_check()
        sensors: list[dict[str, Any]] = []
        for row in rows:
            if not row["entity_id"]:
                continue
            state = self.hass.states.get(row["entity_id"])
            sensors.append(
                {
                    **row,
                    "state": state.state if state else None,
                    "unit": state.attributes.get("unit_of_measurement") if state else None,
                    "state_class": state.attributes.get("state_class") if state else None,
                    "needs_history": row["role"] in STATISTIC_ROLES,
                    "coverage": self._coverage.get(row["role"]),
                }
            )
        first_week = self._coverage_weeks[0] if self._coverage_weeks else ""
        weeks = [week for week in self._week_reports if week["week_start"] >= first_week]
        clean = len(self._training_samples)
        return {
            "requirements": self._requirements(sensors, clean),
            "sensors": sensors,
            "week_starts": list(self._coverage_weeks),
            "weeks": weeks,
            "week_counts": dict(Counter(week["status"] for week in weeks)),
            "clean_weeks": clean,
            "weeks_needed": MIN_TRAINING_WEEKS,
            "extras": [row for row in rows if not row["entity_id"]],
            "history_checked_at": (
                self._history_loaded_at.isoformat() if self._history_loaded_at else None
            ),
            "forecast_warning": self._forecast_warning,
        }

    def _requirements(
        self, sensors: list[dict[str, Any]], clean_weeks: int
    ) -> list[dict[str, Any]]:
        """The setup requirements, each with whether it is met and how to fix it."""
        statistics_fix = (
            "Give the sensor a state class of \"measurement\" so Home Assistant keeps "
            "long-term statistics for it (see the README), or choose a different sensor "
            "under Settings → Devices & services → Oil Monitor → Configure."
        )
        texts = {
            "level": (
                "A tank sensor with daily history",
                "Litres remaining come from its current reading; estimates are learned "
                "from how the level fell over past weeks.",
                statistics_fix,
            ),
            "outdoor": (
                "An outdoor temperature sensor with daily history",
                "Oil use is learned against how cold it was outside.",
                statistics_fix,
            ),
            "indoor": (
                "An indoor temperature sensor with daily history",
                "Heating demand depends on the gap between indoors and outdoors. Choose "
                "a temperature sensor here, not the thermostat itself.",
                statistics_fix,
            ),
            "target": (
                "A thermostat whose target temperature can be read",
                "Estimates assume the house is kept at this temperature.",
                "Choose your thermostat (climate entity) or a sensor holding its target "
                "temperature under Configure.",
            ),
            "weather": (
                "A weather forecast with 7 daily forecasts",
                "The coming week's estimate follows the forecast temperatures.",
                "Choose a weather entity that provides daily forecasts under Configure.",
            ),
            "runtime": (
                "Heating runtime with daily history (optional)",
                "Improves the estimates when available.",
                "It needs a state class of \"measurement\" (hours today) or \"total\" "
                "(a running counter), or leave it empty under Configure.",
            ),
            "tank_temp": (
                "Tank temperature with daily history (optional)",
                "Used to correct ultrasonic readings for cold air.",
                "Without it the outdoor temperature is used; no action is needed.",
            ),
            "timestamp": (
                "Tank measurement time (optional)",
                "Used to warn when the tank reading is out of date.",
                "Its value must be a date and time or a Unix timestamp, or leave it "
                "empty under Configure.",
            ),
        }
        requirements = [
            {
                "title": texts[sensor["role"]][0],
                "why": texts[sensor["role"]][1],
                "fix": texts[sensor["role"]][2] if sensor["status"] != "ok" else "",
                "status": sensor["status"],
                "detail": f"{sensor['entity_id']}: {sensor['message']}",
            }
            for sensor in sensors
            if sensor["role"] in texts
        ]
        enough = clean_weeks >= MIN_TRAINING_WEEKS
        requirements.append(
            {
                "title": f"At least {MIN_TRAINING_WEEKS} complete, clean weeks of history",
                "why": "A clean week has data from every sensor on every day and no "
                "oil delivery. Estimates are learned from these weeks.",
                "fix": ""
                if enough
                else "See \"Weeks used for learning\" below for why weeks were left out. "
                "If the sensors are set up correctly, this builds up on its own.",
                "status": "ok" if enough else "problem",
                "detail": f"{clean_weeks} clean week{'s' if clean_weeks != 1 else ''} found "
                "in the last 12 months.",
            }
        )
        return requirements

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
